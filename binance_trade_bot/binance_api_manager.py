from datetime import datetime, timezone
import json
import math
import os
import time
import traceback
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Callable, Dict, Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException
from cachetools import TTLCache, cached
from sqlalchemy.util.langhelpers import symbol

from .binance_stream_manager import BinanceCache, BinanceOrder, BinanceStreamManager, OrderGuard
from .config import Config
from .database import Database
from .logger import Logger
from .models import Coin

def float_as_decimal_str(num: float):
    return f"{num:0.08f}".rstrip("0").rstrip(".")  # remove trailing zeroes too

class AbstractOrderBalanceManager(ABC):
    @abstractmethod
    def get_currency_balance(self, currency_symbol: str, force=False):
        pass

    @abstractmethod
    def create_order(self, **params):
        pass

    def make_order(
        self, 
        side: str, 
        symbol: str, 
        quantity: float,
        quote_quantity: float,
        price: float
    ):
        params = {
            "symbol": symbol,
            "side": side,
            "quantity": float_as_decimal_str(quantity),
            "type": Client.ORDER_TYPE_MARKET,
        }
        if side == Client.SIDE_BUY:
            del params["quantity"]
            params["quoteOrderQty"] = float_as_decimal_str(quote_quantity)
        return self.create_order(**params)

class BinanceOrderBalanceManager(AbstractOrderBalanceManager):
    def __init__(self, logger: Logger, config: Config, binance_client: Client, cache: BinanceCache):
        self.logger = logger
        self.config = config
        self.binance_client = binance_client
        self.cache = cache

    def make_order(
        self,
        side: str,
        symbol: str,
        quantity: float,
        price: float,
        quote_quantity: float,
    ):
        params = {
            "symbol": symbol,
            "side": side,
            "quantity": float_as_decimal_str(quantity),
            "type": self.config.BUY_ORDER_TYPE if side == Client.SIDE_BUY else self.config.SELL_ORDER_TYPE,
        }
        if params["type"] == Client.ORDER_TYPE_LIMIT:
            params["timeInForce"] = self.binance_client.TIME_IN_FORCE_GTC
            params["price"] = float_as_decimal_str(price)
        elif side == Client.SIDE_BUY:
            del params["quantity"]
            params["quoteOrderQty"] = float_as_decimal_str(quote_quantity)
        return self.create_order(**params)

    def create_order(self, **params):
        return self.binance_client.create_order(**params)

    def get_currency_balance(self, currency_symbol: str, force=False):
        """
        Get balance of a specific coin
        """
        with self.cache.open_balances() as cache_balances:
            balance = cache_balances.get(currency_symbol, None)
            if force or balance is None:
                cache_balances.clear()
                cache_balances.update(
                    {
                        currency_balance["asset"]: float(currency_balance["free"])
                        for currency_balance in self.binance_client.get_account()["balances"]
                    }
                )
                self.logger.debug(f"Fetched all balances: {cache_balances}")
                if currency_symbol not in cache_balances:
                    cache_balances[currency_symbol] = 0.0
                    return 0.0
                return cache_balances.get(currency_symbol, 0.0)

            return balance

class BinanceAPIManager:
    def __init__(
        self,
        client: Client,
        cache: BinanceCache,
        config: Config,
        db: Database,
        logger: Logger,
        order_balance_manager: AbstractOrderBalanceManager,
    ):
        self.binance_client = client
        self.db = db
        self.logger = logger
        self.config = config
        self.cache = cache
        self.order_balance_manager = order_balance_manager
        self.stream_manager: Optional[BinanceStreamManager] = None
        self.setup_websockets()

    @staticmethod
    def _common_factory(
        config: Config,
        db: Database,
        logger: Logger,
        ob_factory: Callable[[Client, BinanceCache], AbstractOrderBalanceManager],
    ) -> "BinanceAPIManager":
        cache = BinanceCache()
        # initializing the client class calls `ping` API endpoint, verifying the connection
        client = Client(
            config.BINANCE_API_KEY,
            config.BINANCE_API_SECRET_KEY,
            tld=config.BINANCE_TLD,
        )
        return BinanceAPIManager(client, cache, config, db, logger, ob_factory(client, cache))

    @staticmethod
    def create_manager(config: Config, db: Database, logger: Logger) -> "BinanceAPIManager":
        return BinanceAPIManager._common_factory(
            config, db, logger, lambda client, cache: BinanceOrderBalanceManager(logger, config, client, cache)
        )

    @staticmethod
    def create_manager_paper_trading(
        config: Config, db: Database, logger: Logger, initial_balances: Optional[Dict[str, float]] = None
    ) -> "BinanceAPIManager":
        manager = BinanceAPIManager._common_factory(
            config,
            db,
            logger,
            lambda client, cache: PaperOrderBalanceManager(
                config.BRIDGE.symbol, client, cache, initial_balances or {config.BRIDGE.symbol: 100.0}
            ),
        )
        manager.order_balance_manager.manager = manager

        return manager

    def now(self):
        return datetime.now(tz=timezone.utc)

    def setup_websockets(self):
        self.stream_manager = BinanceStreamManager(
            self.cache,
            self.config,
            self.binance_client,
            self.logger,
        )

    @cached(cache=TTLCache(maxsize=1, ttl=43200))
    def get_trade_fees(self) -> Dict[str, float]:
        return {ticker["symbol"]: float(ticker["takerCommission"]) for ticker in self.binance_client.get_trade_fee()}

    @cached(cache=TTLCache(maxsize=1, ttl=60))
    def get_using_bnb_for_fees(self):
        return self.binance_client.get_bnb_burn_spot_margin()["spotBNBBurn"]

    def get_fee(self, origin_coin: Coin, target_coin: Coin, selling: bool):
        if self.config.TRADE_FEE != "auto":
            return float(self.config.TRADE_FEE)

        base_fee = self.get_trade_fees()[origin_coin + target_coin]
        if not self.get_using_bnb_for_fees():
            return base_fee

        # The discount is only applied if we have enough BNB to cover the fee
        amount_trading = (
            self._sell_quantity(origin_coin.symbol, target_coin.symbol)
            if selling
            else self._buy_quantity(origin_coin.symbol, target_coin.symbol)
        )

        fee_amount = amount_trading * base_fee * 0.75
        if origin_coin.symbol == "BNB":
            fee_amount_bnb = fee_amount
        else:
            origin_price = self.get_ticker_price(origin_coin + Coin("BNB"))
            if origin_price is None:
                return base_fee
            fee_amount_bnb = fee_amount * origin_price

        bnb_balance = self.get_currency_balance("BNB")

        if bnb_balance >= fee_amount_bnb:
            return base_fee * 0.75
        return base_fee

    def get_account(self):
        """
        Get account information
        """
        return self.binance_client.get_account()

    def get_buy_price(self, ticker_symbol: str):
        price_type = self.config.PRICE_TYPE
        if price_type == Config.PRICE_TYPE_ORDERBOOK:
            return self.get_ask_price(ticker_symbol)
        else:
            return self.get_ticker_price(ticker_symbol)
            
    def get_sell_price(self, ticker_symbol: str):
        price_type = self.config.PRICE_TYPE
        if price_type == Config.PRICE_TYPE_ORDERBOOK:
            return self.get_bid_price(ticker_symbol)
        else:
            return self.get_ticker_price(ticker_symbol)

    def get_ticker_price(self, ticker_symbol: str):
        """
        Get ticker price of a specific coin
        """
        price = self.cache.ticker_values.get(ticker_symbol, None)
        if price is None and ticker_symbol not in self.cache.non_existent_tickers:
            self.cache.ticker_values = {
                ticker["symbol"]: float(ticker["price"]) for ticker in self.binance_client.get_symbol_ticker()
            }
            self.logger.debug(f"Fetched all ticker prices: {self.cache.ticker_values}")
            price = self.cache.ticker_values.get(ticker_symbol, None)
            if price is None:
                self.logger.info(f"Ticker does not exist: {ticker_symbol} - will not be fetched from now on")
                self.cache.non_existent_tickers.add(ticker_symbol)

        return price

    def get_ask_price(self, ticker_symbol: str):
        """
        Get best ask price of a specific coin
        """
        price = self.cache.ticker_values_ask.get(ticker_symbol, None)
        if price is None and ticker_symbol not in self.cache.non_existent_tickers:
            try:
                ticker = self.binance_client.get_orderbook_ticker(symbol = ticker_symbol)
                price = float(ticker['askPrice'])
            except BinanceAPIException as e:
                if e.code == -1121: # invalid symbol
                    price = None
                else:
                    raise e
            if price is None:
                self.logger.info(f"Ticker does not exist: {ticker_symbol} - will not be fetched from now on")
                self.cache.non_existent_tickers.add(ticker_symbol)

        return price

    def get_bid_price(self, ticker_symbol: str):
        """
        Get best bid price of a specific coin
        """
        price = self.cache.ticker_values_bid.get(ticker_symbol, None)
        if price is None and ticker_symbol not in self.cache.non_existent_tickers:
            try:
                ticker = self.binance_client.get_orderbook_ticker(symbol = ticker_symbol)
                price = float(ticker['bidPrice'])
            except BinanceAPIException as e:
                if e.code == -1121: # invalid symbol
                    price = None
                else:
                    raise e
            if price is None:
                self.logger.info(f"Ticker does not exist: {ticker_symbol} - will not be fetched from now on")
                self.cache.non_existent_tickers.add(ticker_symbol)
        
        return price

    def get_currency_balance(self, currency_symbol: str, force=False) -> float:
        """
        Get balance of a specific coin
        """
        return self.order_balance_manager.get_currency_balance(currency_symbol, force)

    def retry(self, func, *args, **kwargs):
        for attempt in range(20):
            try:
                return func(*args, **kwargs)
            except Exception:  # pylint: disable=broad-except
                self.logger.warning(f"Failed to Buy/Sell. Trying Again (attempt {attempt}/20)")
                if attempt == 0:
                    self.logger.warning(traceback.format_exc())
            time.sleep(1)
        return None

    def get_symbol_filter(self, origin_symbol: str, target_symbol: str, filter_type: str):
        return next(
            _filter
            for _filter in self.binance_client.get_symbol_info(origin_symbol + target_symbol)["filters"]
            if _filter["filterType"] == filter_type
        )

    @cached(cache=TTLCache(maxsize=2000, ttl=43200))
    def get_alt_tick(self, origin_symbol: str, target_symbol: str):
        step_size = self.get_symbol_filter(origin_symbol, target_symbol, "LOT_SIZE")["stepSize"]
        if step_size.find("1") == 0:
            return 1 - step_size.find(".")
        return step_size.find("1") - 1

    @cached(cache=TTLCache(maxsize=2000, ttl=43200))
    def get_min_notional(self, origin_symbol: str, target_symbol: str):
        return float(self.get_symbol_filter(origin_symbol, target_symbol, "MIN_NOTIONAL")["minNotional"])

    @cached(cache=TTLCache(maxsize=2000, ttl=43200))
    def get_min_qty(self, origin_symbol: str, target_symbol: str):
        return float(self.get_symbol_filter(origin_symbol, target_symbol, "LOT_SIZE")["minQty"])

    def _wait_for_order(
        self, order_id, origin_symbol: str, target_symbol: str
    ) -> Optional[BinanceOrder]:  # pylint: disable=unsubscriptable-object
        while True:
            order_status: BinanceOrder = self.cache.orders.get(order_id, None)
            if order_status is not None:
                break
            self.logger.debug(f"Waiting for order {order_id} to be created")
            time.sleep(1)

        self.logger.debug(f"Order created: {order_status}")

        while order_status.status != "FILLED":
            try:
                order_status = self.cache.orders.get(order_id, None)

                self.logger.debug(f"Waiting for order {order_id} to be filled")

                if self._should_cancel_order(order_status):
                    cancel_order = None
                    while cancel_order is None:
                        cancel_order = self.binance_client.cancel_order(
                            symbol=origin_symbol + target_symbol, orderId=order_id
                        )
                    self.logger.info("Order timeout, canceled...")

                    # sell partially
                    if order_status.status == "PARTIALLY_FILLED" and order_status.side == "BUY":
                        self.logger.info("Sell partially filled amount")

                        order_quantity = self._sell_quantity(origin_symbol, target_symbol)
                        partially_order = None
                        while partially_order is None:
                            partially_order = self.binance_client.order_market_sell(
                                symbol=origin_symbol + target_symbol, quantity=order_quantity
                            )

                    self.logger.info("Going back to scouting mode...")
                    return None

                if order_status.status == "CANCELED":
                    self.logger.info("Order is canceled, going back to scouting mode...")
                    return None

                time.sleep(1)
            except BinanceAPIException as e:
                self.logger.info(e)
                time.sleep(1)
            except Exception as e:  # pylint: disable=broad-except
                self.logger.info(f"Unexpected Error: {e}")
                time.sleep(1)

        self.logger.debug(f"Order filled: {order_status}")
        return order_status

    def wait_for_order(
        self, order_id, origin_symbol: str, target_symbol: str, order_guard: OrderGuard
    ) -> Optional[BinanceOrder]:  # pylint: disable=unsubscriptable-object
        with order_guard:
            return self._wait_for_order(order_id, origin_symbol, target_symbol)

    def _should_cancel_order(self, order_status):
        minutes = (time.time() - order_status.time / 1000) / 60
        timeout = 0

        if order_status.side == "SELL":
            timeout = float(self.config.SELL_TIMEOUT)
        else:
            timeout = float(self.config.BUY_TIMEOUT)

        if timeout and minutes > timeout and order_status.status == "NEW":
            return True

        if timeout and minutes > timeout and order_status.status == "PARTIALLY_FILLED":
            if order_status.side == "SELL":
                return True

            if order_status.side == "BUY":
                current_price = self.get_buy_price(order_status.symbol)
                if float(current_price) * (1 - 0.001) > float(order_status.price):
                    return True

        return False

    def _adjust_bnb_balance(self, origin_coin: Coin, target_coin: Coin):
        if not self.get_using_bnb_for_fees():
            # No need to adjust bnb balance if not using bnb for fees
            return

        base_fee = self.get_trade_fees()[origin_coin + target_coin]

        # The discount is only applied if we have enough BNB to cover the fee
        amount_trading = self._buy_quantity(origin_coin.symbol, target_coin.symbol)

        fee_amount = amount_trading * base_fee * 0.75
        if origin_coin.symbol == "BNB":
            fee_amount_bnb = fee_amount
        else:
            origin_price = self.get_ticker_price(origin_coin.symbol + "BNB")
            if origin_price is None:
                return
            fee_amount_bnb = fee_amount * origin_price

        bnb_balance = self.get_currency_balance("BNB")

        if bnb_balance >= fee_amount_bnb:
            # No need to buy more bnb
            return

        min_qty = self.get_min_qty("BNB", target_coin.symbol)
        alt_tick = self.get_alt_tick("BNB", target_coin.symbol)
        # Default value of AUTO_ADJUST_BNB_BALANCE_RATE is 3, means trying to buy 3x BNB compare to the commision needed by the coming order.
        # Put "3x" as default since: 1. buy commision, 2. sell commision, 3. buffer, since selling price may rise and then needs more comission.
        fee_amount_bnb_ceil = math.ceil((fee_amount_bnb * self.config.AUTO_ADJUST_BNB_BALANCE_RATE - bnb_balance) * 10 ** alt_tick) / float(10 ** alt_tick)

        min_notional = self.get_min_notional("BNB", target_coin.symbol)
        bnb_price = self.get_ticker_price("BNB" + target_coin.symbol)
        # multiply 1.01 considering that market price is changing
        min_qty_for_min_notinal = math.ceil((min_notional / bnb_price) * 1.01 * 10 ** alt_tick) / float(10 ** alt_tick)

        buy_quantity = max(min_qty, fee_amount_bnb_ceil, min_qty_for_min_notinal)

        self.logger.info(f"Needed/available BNB balance: {fee_amount_bnb}/{bnb_balance}, buy quantity: {buy_quantity}...")

        self.retry(self._buy_alt, Coin("BNB"), target_coin, bnb_price, buy_quantity)

    def buy_alt(self, origin_coin: Coin, target_coin: Coin, buy_price: float) -> BinanceOrder:
        return self.retry(self._buy_alt, origin_coin, target_coin, buy_price)

    def _buy_quantity(
        self, origin_symbol: str, target_symbol: str, target_balance: float = None, from_coin_price: float = None
    ):
        target_balance = target_balance or self.get_currency_balance(target_symbol)
        from_coin_price = from_coin_price or self.get_buy_price(origin_symbol + target_symbol)

        origin_tick = self.get_alt_tick(origin_symbol, target_symbol)
        return math.floor(target_balance * 10 ** origin_tick / from_coin_price) / float(10 ** origin_tick)

    @staticmethod
    def float_as_decimal_str(num: float):
        return f"{num:0.08f}".rstrip("0").rstrip(".")  # remove trailing zeroes too    

    def _buy_alt(self, origin_coin: Coin, target_coin: Coin, buy_price: float, buy_quantity: float=None):  # pylint: disable=too-many-locals
        """
        Buy altcoin
        """
        if self.config.AUTO_ADJUST_BNB_BALANCE and origin_coin.symbol != "BNB":
            self._adjust_bnb_balance(origin_coin, target_coin)

        origin_symbol = origin_coin.symbol
        target_symbol = target_coin.symbol

        with self.cache.open_balances() as balances:
            balances.clear()

        origin_balance = self.get_currency_balance(origin_symbol)
        target_balance = self.get_currency_balance(target_symbol)
        from_coin_price = self.get_buy_price(origin_symbol + target_symbol)

        buy_max_price_change = float(self.config.BUY_MAX_PRICE_CHANGE)
        if from_coin_price > buy_price * (1.0 + buy_max_price_change):
            self.logger.info("Buy price became higher, cancel buy")
            return None
        #from_coin_price = min(buy_price, from_coin_price)
        trade_log = self.db.start_trade_log(origin_coin, target_coin, False)

        if buy_quantity is None:
            order_quantity = self._buy_quantity(origin_symbol, target_symbol, target_balance, from_coin_price)
        else:
            order_quantity = buy_quantity
        self.logger.info(f"Buying {order_quantity} <{origin_symbol}> (price: {round(buy_price, 2)})")

        # Try to buy until successful
        order = None
        order_guard = self.stream_manager.acquire_order_guard()
        while order is None:
            try:
                order = self.order_balance_manager.make_order(
                    side=Client.SIDE_BUY,
                    symbol=origin_symbol + target_symbol,
                    quantity=order_quantity,
                    quote_quantity=target_balance,
                    price=from_coin_price,
                )
                self.logger.info(order, False)
            except BinanceAPIException as e:
                self.logger.info(e)
                time.sleep(1)
            except Exception as e:  # pylint: disable=broad-except
                self.logger.warning(f"Unexpected Error: {e}")

        executed_qty = float(order.get("executedQty", 0))
        if executed_qty > 0 and order["status"] == "FILLED":
            order_quantity = executed_qty  # Market buys provide QTY of actually bought asset

        trade_log.set_ordered(origin_balance, target_balance, order_quantity)

        order_guard.set_order(origin_symbol, target_symbol, int(order["orderId"]))
        order = self.wait_for_order(order["orderId"], origin_symbol, target_symbol, order_guard)

        if order is None:
            return None

        self.logger.info(f"Bought {order_quantity} <{origin_symbol}> (price: {round(order.price, 2)}) for a total of {order.cumulative_quote_qty} {target_symbol}")

        trade_log.set_complete(order.cumulative_quote_qty)

        return order

    def sell_alt(self, origin_coin: Coin, target_coin: Coin, sell_price: float) -> BinanceOrder:
        return self.retry(self._sell_alt, origin_coin, target_coin, sell_price)

    def _sell_quantity(self, origin_symbol: str, target_symbol: str, origin_balance: float = None):
        origin_balance = origin_balance or self.get_currency_balance(origin_symbol)

        origin_tick = self.get_alt_tick(origin_symbol, target_symbol)
        return math.floor(origin_balance * 10 ** origin_tick) / float(10 ** origin_tick)

    def _sell_alt(self, origin_coin: Coin, target_coin: Coin, sell_price: float):  # pylint: disable=too-many-locals
        """
        Sell altcoin
        """
        origin_symbol = origin_coin.symbol
        target_symbol = target_coin.symbol

        # get fresh balances
        with self.cache.open_balances() as balances:
            balances.clear()

        origin_balance = self.get_currency_balance(origin_symbol)
        target_balance = self.get_currency_balance(target_symbol)
        from_coin_price = self.get_sell_price(origin_symbol + target_symbol)

        sell_max_price_change = float(self.config.SELL_MAX_PRICE_CHANGE)
        if from_coin_price < sell_price * (1.0 - sell_max_price_change):
            self.logger.info("Sell price became lower, skipping sell")
            return None  # skip selling below price from ratio
        #from_coin_price = max(from_coin_price, sell_price)

        trade_log = self.db.start_trade_log(origin_coin, target_coin, True)

        order_quantity = self._sell_quantity(origin_symbol, target_symbol, origin_balance)
        self.logger.info(f"Selling {order_quantity} <{origin_symbol}> (price: {round(sell_price, 2)}). Balance: {origin_balance} <{origin_symbol}>")

        order = None
        order_guard = self.stream_manager.acquire_order_guard()
        while order is None:
            try:
                order = self.order_balance_manager.make_order(
                    side=Client.SIDE_SELL,
                    symbol=origin_symbol + target_symbol,
                    quantity=order_quantity,
                    quote_quantity=from_coin_price * order_quantity,
                    price=from_coin_price,
                )
                self.logger.info(order, False)
            except BinanceAPIException as e:
                self.logger.info(e)
                time.sleep(1)
            except Exception as e:  # pylint: disable=broad-except
                self.logger.warning(f"Unexpected Error: {e}")

        self.logger.info("order", False)
        self.logger.info(order, False)

        trade_log.set_ordered(origin_balance, target_balance, order_quantity)

        order_guard.set_order(origin_symbol, target_symbol, int(order["orderId"]))
        order = self.wait_for_order(order["orderId"], origin_symbol, target_symbol, order_guard)

        if order is None:
            return None

        new_balance = self.get_currency_balance(origin_symbol)
        while new_balance >= origin_balance:
            new_balance = self.get_currency_balance(origin_symbol, True)

        self.logger.info(f"Sold {order_quantity} <{origin_symbol}> (price: {round(order.price, 2)}) for a total of {order.cumulative_quote_qty} {target_symbol}")

        trade_log.set_complete(order.cumulative_quote_qty)

        return order

class PaperOrderBalanceManager(AbstractOrderBalanceManager):
    PERSIST_FILE_PATH = "data/paper_wallet.json"

    def __init__(
        self,
        bridge_symbol: str,
        client: Client,
        cache: BinanceCache,
        initial_balances: Dict[str, float],
        read_persist=True,
    ):
        self.manager: BinanceAPIManager = None
        self.balances = initial_balances
        self.bridge = bridge_symbol
        self.client = client
        self.cache = cache
        self.fake_order_id = 0
        if read_persist:
            data = self._read_persist()
            if data is not None:
                if "balances" in data:
                    self.balances = data["balances"]
                    self.fake_order_id = data["fake_order_id"]
                else:
                    self.balances = data  # to support older format

    def _read_persist(self):
        if os.path.exists(self.PERSIST_FILE_PATH):
            with open(self.PERSIST_FILE_PATH) as json_file:
                return json.load(json_file)
        return None

    def _write_persist(self):
        with open(self.PERSIST_FILE_PATH, "w") as json_file:
            json.dump({"balances": self.balances, "fake_order_id": self.fake_order_id}, json_file)

    def get_currency_balance(self, currency_symbol: str, force=False):
        return self.balances.get(currency_symbol, 0.0)

    def create_order(self, **params):
        return {}

    def make_order(
        self, 
        side: str, 
        symbol: str, 
        quantity: float,
        quote_quantity: float,
        price: float
    ):
        symbol_base = symbol[: -len(self.bridge)]
        if side == Client.SIDE_SELL:
            fees = self.manager.get_fee(Coin(symbol_base), Coin(self.bridge), True)
            self.balances[self.bridge] = self.get_currency_balance(self.bridge) + quote_quantity * (1 - fees)
            self.balances[symbol_base] = self.get_currency_balance(symbol_base) - quantity
        else:
            fees = self.manager.get_fee(Coin(symbol_base), Coin(self.bridge), False)
            self.balances[self.bridge] = self.get_currency_balance(self.bridge) - quote_quantity
            self.balances[symbol_base] = self.get_currency_balance(symbol_base) + quantity * (1 - fees)
        self.cache.balances_changed_event.set()
        super().make_order(side, symbol, quantity, quote_quantity, price)
        if side == Client.SIDE_BUY:
            # we do it only after buy for transaction speed
            # probably should be a better idea to make it a postponed call
            self._write_persist()

        self.fake_order_id += 1       

        forder = BinanceOrder(
            defaultdict(
                lambda: "",
                order_id=str(self.fake_order_id),
                current_order_status="FILLED",
                executedQty=str(quantity),
                cumulative_filled_quantity=str(quote_quantity),
                cumulative_quote_asset_transacted_quantity=str(quote_quantity),
                order_price=str(price),
                side=side,
                type=Client.ORDER_TYPE_MARKET,
            )
        )
        self.cache.orders[str(self.fake_order_id)] = forder

        return defaultdict(
            lambda: "",
            orderId=str(self.fake_order_id),
            status="FILLED",
            executedQty=str(quantity),
            cumulative_filled_quantity=str(quote_quantity),
            price=str(price),
            side=side,
            type=Client.ORDER_TYPE_MARKET,
        )
