from collections import defaultdict
from datetime import datetime, timedelta, timezone
from traceback import format_exc
from typing import Dict
from sqlalchemy.orm.session import Session
from binance.client import Client

from .binance_api_manager import BinanceAPIManager, BinanceOrderBalanceManager
from .binance_stream_manager import BinanceCache, BinanceOrder
from .historic_kline_cache import HistoricKlineCache
from .config import Config
from .database import Database
from .logger import Logger
from .models import Coin, Pair, Trade, TradeState
from .strategies import get_strategy

class MockBinanceManager(BinanceAPIManager):
    def __init__(
            self,
            client: Client,
            binance_cache: BinanceCache,
            config: Config,
            db: Database,
            logger: Logger,
            start_date: datetime = None,
            start_balances: Dict[str, float] = None,
    ):
        super().__init__(client, binance_cache, config, db, logger, BinanceOrderBalanceManager(logger, config, client, binance_cache))
        self.config = config
        self.datetime = start_date or datetime(2021, 1, 1)
        self.balances = start_balances or {config.BRIDGE.symbol: 100}
        self.ignored_symbols = ["BTTBTC"]
        self.trades = 0
        self.positve_coin_jumps = 0
        self.negative_coin_jumps = 0
        self.paid_fees = {}
        self.coins_trades= {}
        self.historic_kline_cache = HistoricKlineCache(client, logger)

    def now(self):
        return self.datetime.replace(tzinfo=timezone.utc)

    def setup_websockets(self):
        pass  # No websockets are needed for backtesting

    def increment(self, interval=1):
        self.datetime += timedelta(minutes=interval)

    def get_fee(self, origin_coin: Coin, target_coin: Coin, selling: bool):
        if self.config.TRADE_FEE != "auto":
            return float(self.config.TRADE_FEE)
            
        return 0.001

    def get_min_notional(self, origin_symbol: str, target_symbol: str):
        return 10.0

    def get_buy_price(self, ticker_symbol: str):
        return self.get_ticker_price(ticker_symbol)

    def get_sell_price(self, ticker_symbol: str):
        price = self.get_ticker_price(ticker_symbol)
        if price is not None:
            price = price * 0.998
        return price

    def get_ticker_price(self, ticker_symbol: str):
        """
        Get ticker price of a specific coin
        """
        return self.historic_kline_cache.get_historical_ticker_price(ticker_symbol, self.now())

    def get_currency_balance(self, currency_symbol: str, force=False):
        """
        Get balance of a specific coin
        """
        return self.balances.get(currency_symbol, 0)

    def buy_alt(self, origin_coin: Coin, target_coin: Coin, buy_price: float):
        origin_symbol = origin_coin.symbol
        target_symbol = target_coin.symbol

        target_balance = self.get_currency_balance(target_symbol)
        from_coin_price = self.get_ticker_price(origin_symbol + target_symbol)

        order_quantity = self._buy_quantity(origin_symbol, target_symbol, target_balance, from_coin_price)
        target_quantity = order_quantity * from_coin_price
        fee = order_quantity * self.get_fee(origin_coin, target_coin, False)
        self.balances[target_symbol] -= target_quantity
        self.balances[origin_symbol] = self.balances.get(origin_symbol, 0) + order_quantity - fee
        if origin_symbol not in self.paid_fees.keys():
            self.paid_fees[origin_symbol] = 0
        self.paid_fees[origin_symbol] += fee
        if origin_symbol not in self.coins_trades.keys():
            self.coins_trades[origin_symbol] = []
        self.coins_trades[origin_symbol].append(self.balances[origin_symbol])

        diff = self.get_diff(origin_symbol)
        diff_str = ""
        if diff is None:
            diff_str = "None"
        else:
            diff_str = f"{diff} %"

        self.logger.info(
            f"{self.datetime} Bought {origin_symbol} {round(self.balances[origin_symbol], 4)} for {from_coin_price} {target_symbol}. Gain: {diff_str}"
        )
        
        if diff is not None:
            if diff > 0.0:
                self.positve_coin_jumps +=1
            else:
                self.negative_coin_jumps += 1

        event = defaultdict(
            lambda: None,
            order_price=from_coin_price,
            cumulative_quote_asset_transacted_quantity=0.0,
            cumulative_filled_quantity=0.0,
        )

        session: Session
        with self.db.db_session() as session:
            from_coin = session.merge(origin_coin)
            to_coin = session.merge(target_coin)
            trade = Trade(from_coin, to_coin, False)
            trade.datetime = self.datetime
            trade.state = TradeState.COMPLETE
            session.add(trade)
            # Flush so that SQLAlchemy fills in the id column
            session.flush()
            self.db.send_update(trade)

        self.trades += 1
        return BinanceOrder(event)

    def sell_alt(self, origin_coin: Coin, target_coin: Coin, sell_price: float):
        origin_symbol = origin_coin.symbol
        target_symbol = target_coin.symbol

        origin_balance = self.get_currency_balance(origin_symbol)
        from_coin_price = self.get_ticker_price(origin_symbol + target_symbol)

        order_quantity = self._sell_quantity(origin_symbol, target_symbol, origin_balance)
        target_quantity = order_quantity * from_coin_price

        fee = target_quantity * self.get_fee(origin_coin, target_coin, True)
        self.balances[target_symbol] = self.balances.get(target_symbol, 0) + target_quantity - fee
        if target_symbol not in self.paid_fees.keys():
            self.paid_fees[target_symbol] = 0
        self.paid_fees[target_symbol] += fee
        self.balances[origin_symbol] -= order_quantity

        self.logger.info(
            f"{self.datetime} Sold {origin_symbol} for {from_coin_price} {target_symbol}"
        )
        
        self.trades += 1
        return {"price": from_coin_price}

    def collate_coins(self, target_symbol: str):      
        return self.collate(target_symbol, self.balances)

    def collate_fees(self, target_symbol: str):        
        return self.collate(target_symbol, self.paid_fees)

    def collate(self, target_symbol: str, balances: dict):
        total = 0
        for coin, balance in balances.items():
            if coin == target_symbol:
                total += balance
                continue
            if coin == self.config.BRIDGE.symbol:
                price = self.get_ticker_price(target_symbol + coin)
                if price is None:
                    continue
                total += balance / price
            else:
                price = self.get_ticker_price(coin + target_symbol)
                if price is None:
                    continue
                total += price * balance
        return total
    
    def get_diff(self, symbol):
        if len(self.coins_trades[symbol]) == 1:
            return None
        return round(((self.coins_trades[symbol][-1] - self.coins_trades[symbol][-2]) /self.coins_trades[symbol][-1] * 100 ),2)

class MockDatabase(Database):
    def __init__(self, logger: Logger, config: Config):
        super().__init__(logger, config, "sqlite:///", True)

    def log_scout(self, pair: Pair, target_ratio: float, current_coin_price: float, other_coin_price: float):
        pass

def backtest(
        start_date: datetime = None,
        end_date: datetime = None,
        interval=1,
        yield_interval=100,
        start_balances: Dict[str, float] = None,
        starting_coin: str = None,
        config: Config = None,
):
    """

    :param config: Configuration object to use
    :param start_date: Date to  backtest from
    :param end_date: Date to backtest up to
    :param interval: Number of virtual minutes between each scout
    :param yield_interval: After how many intervals should the manager be yielded
    :param start_balances: A dictionary of initial coin values. Default: {BRIDGE: 100}
    :param starting_coin: The coin to start on. Default: first coin in coin list

    :return: The final coin balances
    """
    config = config or Config()
    logger = Logger("backtesting", enable_notifications=False)

    end_date = end_date or datetime.today()

    db = MockDatabase(logger, config)
    db.create_database()
    db.set_coins(config.SUPPORTED_COIN_LIST)
    manager = MockBinanceManager(        
        Client(config.BINANCE_API_KEY, config.BINANCE_API_SECRET_KEY, tld=config.BINANCE_TLD),
        BinanceCache(),
        config, 
        db, 
        logger,
        start_date,
        start_balances
    )

    starting_coin = db.get_coin(starting_coin or config.CURRENT_COIN_SYMBOL or config.SUPPORTED_COIN_LIST[0])
    if manager.get_currency_balance(starting_coin.symbol) == 0:
        manager.buy_alt(starting_coin, config.BRIDGE, 0.0)  # doesn't matter mocking manager don't look at fixed price
    db.set_current_coin(starting_coin)

    strategy = get_strategy(config.STRATEGY)
    if strategy is None:
        logger.error("Invalid strategy name")
        return manager
    trader = strategy(manager, db, logger, config)
    trader.initialize()

    yield manager

    n = 1
    try:
        while manager.datetime < end_date:
            try:
                trader.scout()
            except Exception:  # pylint: disable=broad-except
                logger.warning(format_exc())
            manager.increment(interval)
            if n % yield_interval == 0:
                yield manager
            n += 1
    except KeyboardInterrupt:
        pass
    return manager
