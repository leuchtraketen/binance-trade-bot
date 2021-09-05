
import asyncio
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Dict, Set, Tuple, Optional

import binance.client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from unicorn_binance_websocket_api import BinanceWebSocketApiManager

from .config import Config
from .database import Database
from .logger import Logger

class ThreadSafeAsyncLock:
    def __init__(self):
        self._init_lock = threading.Lock()
        self._async_lock: Optional[asyncio.Lock] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_loop(self):
        with self._init_lock:
            self._async_lock = asyncio.Lock()
            self.loop = asyncio.get_running_loop()

    def acquire(self):
        self.__enter__()

    def release(self):
        self.__exit__(None, None, None)

    def __enter__(self):
        self._init_lock.__enter__()
        if self._async_lock is not None:
            asyncio.run_coroutine_threadsafe(self._async_lock.__aenter__(), self.loop).result()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._async_lock is not None:
            asyncio.run_coroutine_threadsafe(self._async_lock.__aexit__(exc_type, exc_val, exc_tb), self.loop).result()
        self._init_lock.__exit__(exc_type, exc_val, exc_tb)

    async def __aenter__(self):
        await self._async_lock.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._async_lock.__aexit__(exc_type, exc_val, exc_tb)

class BinanceOrder:  # pylint: disable=too-few-public-methods
    def __init__(self, report):
        self.event = report
        self.symbol = report["symbol"]
        self.side = report["side"]
        self.order_type = report["order_type"]
        self.id = report["order_id"]
        self.cumulative_quote_qty = float(report["cumulative_quote_asset_transacted_quantity"])
        self.status = report["current_order_status"]
        self.price = float(report["order_price"])
        self.time = report["transaction_time"]
        self.cumulative_filled_quantity = float(report["cumulative_filled_quantity"])

    def __repr__(self):
        return f"<BinanceOrder {self.event}>"


class BinanceCache:  # pylint: disable=too-few-public-methods
    def __init__(self):
        self.ticker_values: Dict[str, float] = {}
        self.ticker_values_ask: Dict[str, float] = {}
        self.ticker_values_bid: Dict[str, float] = {}
        self._balances: Dict[str, float] = {}
        self._balances_mutex: ThreadSafeAsyncLock = ThreadSafeAsyncLock()
        self.non_existent_tickers: Set[str] = set()
        self.balances_changed_event = threading.Event()
        self.orders: Dict[str, BinanceOrder] = {}

    def attach_loop(self):
        self._balances_mutex.attach_loop()

    @contextmanager
    def open_balances(self):
        with self._balances_mutex:
            yield self._balances

    @asynccontextmanager
    async def open_balances_async(self):
        async with self._balances_mutex:
            yield self._balances


class OrderGuard:
    def __init__(self, pending_orders: Set[Tuple[str, int]], mutex: threading.Lock):
        self.pending_orders = pending_orders
        self.mutex = mutex
        # lock immediately because OrderGuard
        # should be entered and put tag that shouldn't be missed
        self.mutex.acquire()
        self.tag = None

    def set_order(self, origin_symbol: str, target_symbol: str, order_id: int):
        self.tag = (origin_symbol + target_symbol, order_id)

    def __enter__(self):
        try:
            if self.tag is None:
                raise Exception("OrderGuard wasn't properly set")
            self.pending_orders.add(self.tag)
        finally:
            self.mutex.release()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.pending_orders.remove(self.tag)


class BinanceStreamManager:
    def __init__(self, cache: BinanceCache, config: Config, binance_client: binance.client.Client, db: Database, logger: Logger):
        self.cache = cache
        self.db = db
        self.logger = logger
        self.bw_api_manager = BinanceWebSocketApiManager(
            output_default="UnicornFy", enable_stream_signal_buffer=True, exchange=f"binance.{config.BINANCE_TLD}"
        )
        self.bw_api_manager.create_stream(
            ["arr"], ["!miniTicker"], api_key=config.BINANCE_API_KEY, api_secret=config.BINANCE_API_SECRET_KEY
        )
        self.bw_api_manager.create_stream(
            ["arr"], ["!userData"], api_key=config.BINANCE_API_KEY, api_secret=config.BINANCE_API_SECRET_KEY
        )


        if config.PRICE_TYPE == Config.PRICE_TYPE_ORDERBOOK:

            bridge_coin = config.BRIDGE_SYMBOL
            coin_symbols = []

            for coin in self.db.get_coins():
                coin_symbols.append(coin.symbol.lower() + bridge_coin.lower())

            self.bw_api_manager.create_stream(
                ["bookTicker"], coin_symbols
            )
            
        self.binance_client = binance_client
        self.pending_orders: Set[Tuple[str, int]] = set()
        self.pending_orders_mutex: threading.Lock = threading.Lock()
        self._processorThread = threading.Thread(target=self._stream_processor)
        self._processorThread.start()

    def acquire_order_guard(self):
        return OrderGuard(self.pending_orders, self.pending_orders_mutex)

    def _fetch_pending_orders(self):
        pending_orders: Set[Tuple[str, int]]
        with self.pending_orders_mutex:
            pending_orders = self.pending_orders.copy()
        for (symbol, order_id) in pending_orders:
            order = None
            while True:
                try:
                    order = self.binance_client.get_order(symbol=symbol, orderId=order_id)
                except (BinanceRequestException, BinanceAPIException) as e:
                    self.logger.error(f"Got exception during fetching pending order: {e}")
                if order is not None:
                    break
                time.sleep(1)
            fake_report = {
                "symbol": order["symbol"],
                "side": order["side"],
                "order_type": order["type"],
                "order_id": order["orderId"],
                "cumulative_quote_asset_transacted_quantity": float(order["cummulativeQuoteQty"]),
                "cumulative_filled_quantity": float(order["executedQty"]),
                "current_order_status": order["status"],
                "order_price": float(order["price"]),
                "transaction_time": order["time"],
            }
            self.logger.info(f"Pending order {order_id} for symbol {symbol} fetched:\n{fake_report}", False)
            self.cache.orders[fake_report["order_id"]] = BinanceOrder(fake_report)

    def _invalidate_balances(self):
        with self.cache.open_balances() as balances:
            balances.clear()

    def _stream_processor(self):
        while True:
            if self.bw_api_manager.is_manager_stopping():
                sys.exit()

            stream_signal = self.bw_api_manager.pop_stream_signal_from_stream_signal_buffer()
            stream_data = self.bw_api_manager.pop_stream_data_from_stream_buffer()

            if stream_signal is not False:
                signal_type = stream_signal["type"]
                stream_id = stream_signal["stream_id"]
                if signal_type == "CONNECT":
                    stream_info = self.bw_api_manager.get_stream_info(stream_id)
                    if "!userData" in stream_info["markets"]:
                        self.logger.debug("Connect for userdata arrived", False)
                        self._fetch_pending_orders()
                        self._invalidate_balances()
            if stream_data is not False and "event_type" in stream_data:
                self._process_stream_data(stream_data)
            if stream_data is False and stream_signal is False:
                time.sleep(0.01)

    def _process_stream_data(self, stream_data):
        event_type = stream_data["event_type"]
        if event_type == "executionReport":  # !userData
            self.logger.debug(f"execution report: {stream_data}")
            order = BinanceOrder(stream_data)
            self.cache.orders[order.id] = order
        elif event_type == "balanceUpdate":  # !userData
            self.logger.debug(f"Balance update: {stream_data}")
            with self.cache.open_balances() as balances:
                asset = stream_data["asset"]
                if asset in balances:
                    del balances[stream_data["asset"]]
        elif event_type in ("outboundAccountPosition", "outboundAccountInfo"):  # !userData
            self.logger.debug(f"{event_type}: {stream_data}")
            with self.cache.open_balances() as balances:
                for bal in stream_data["balances"]:
                    balances[bal["asset"]] = float(bal["free"])
        elif event_type == "24hrMiniTicker":
            for event in stream_data["data"]:
                self.cache.ticker_values[event["symbol"]] = float(event["close_price"])
        elif event_type == "bookTicker":
                self.cache.ticker_values_ask[stream_data["symbol"]] = float(stream_data["best_ask_price"])
                self.cache.ticker_values_bid[stream_data["symbol"]] = float(stream_data["best_bid_price"])
        else:
            self.logger.error(f"Unknown event type found: {event_type}\n{stream_data}")

    def close(self):
        self.bw_api_manager.stop_manager_with_all_streams()
