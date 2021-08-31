from collections import defaultdict
from datetime import datetime
from typing import Dict, List
import json

from sqlalchemy.orm import Session

from .binance_api_manager import BinanceAPIManager
from .config import Config
from .database import Database, LogScout
from .logger import Logger
from .models import Coin, CoinValue, Pair


class AutoTrader:
    def __init__(self, binance_manager: BinanceAPIManager, database: Database, logger: Logger, config: Config):
        self.manager = binance_manager
        self.db = database
        self.logger = logger
        self.config = config
        self.failed_buy_order = False

        self.last_prices = {}

        self.trailing_stop = None
        self.allow_trade = False

    def initialize(self):
        self.initialize_trade_thresholds()
        self.track_last_prices()

    def transaction_through_bridge(self, pair: Pair, sell_price: float, buy_price: float):
        """
        Jump from the source coin to the destination coin through bridge coin
        """
        can_sell = False
        balance = self.manager.get_currency_balance(pair.from_coin.symbol)

        if balance and balance * sell_price > self.manager.get_min_notional(
            pair.from_coin.symbol, self.config.BRIDGE.symbol
        ):
            can_sell = True
        else:
            self.logger.info("Skipping sell")

        if can_sell and self.manager.sell_alt(pair.from_coin, self.config.BRIDGE, sell_price) is None:
            self.logger.info("Couldn't sell, going back to scouting mode...")
            return None

        result = self.manager.buy_alt(pair.to_coin, self.config.BRIDGE, buy_price)
        if result is not None:
            self.db.set_current_coin(pair.to_coin)
            price = result.price
            if abs(price) < 1e-15:
                price = result.cumulative_quote_qty / result.cumulative_filled_quantity

            self.update_trade_threshold(pair.to_coin, price)
            self.failed_buy_order = False
            return result

        self.logger.info("Couldn't buy, going back to scouting mode...")
        self.failed_buy_order = True
        return None

    def update_trade_threshold(self, coin: Coin, coin_price: float):
        """
        Update all the coins with the threshold of buying the current held coin
        """

        if coin_price is None:
            self.logger.info("Skipping update... current coin {} not found".format(coin + self.config.BRIDGE))
            return

        session: Session
        with self.db.db_session() as session:
            for pair in session.query(Pair).filter(Pair.to_coin == coin):
                from_coin_price = self.manager.get_sell_price(pair.from_coin + self.config.BRIDGE)

                if from_coin_price is None:
                    self.logger.info(
                        "Skipping update for coin {} not found".format(pair.from_coin + self.config.BRIDGE)
                    )
                    continue

                pair.ratio = from_coin_price / coin_price

    def initialize_trade_thresholds(self):
        """
        Initialize the buying threshold of all the coins for trading between them
        """
        session: Session
        with self.db.db_session() as session:
            pairs = session.query(Pair).filter(Pair.ratio.is_(None)).all()
            grouped_pairs = defaultdict(list)
            for pair in pairs:
                if pair.from_coin.enabled and pair.to_coin.enabled:
                    grouped_pairs[pair.from_coin.symbol].append(pair)
            for from_coin_symbol, group in grouped_pairs.items():
                self.logger.info(f"Initializing {from_coin_symbol} vs [{', '.join([p.to_coin.symbol for p in group])}]")
                for pair in group:
                    from_coin_price = self.manager.get_sell_price(pair.from_coin + self.config.BRIDGE)
                    if from_coin_price is None:
                        self.logger.info(
                            "Skipping initializing {}, symbol not found".format(pair.from_coin + self.config.BRIDGE)
                        )
                        continue

                    to_coin_price = self.manager.get_buy_price(pair.to_coin + self.config.BRIDGE)
                    if to_coin_price is None:
                        self.logger.info(
                            "Skipping initializing {}, symbol not found".format(pair.to_coin + self.config.BRIDGE)
                        )
                        continue

                    pair.ratio = from_coin_price / to_coin_price

    def scout(self):
        """
        Scout for potential jumps from the current coin to another coin
        """
        raise NotImplementedError()

    def _get_ratios(self, coin: Coin, coin_price, excluded_coins: List[Coin] = []):
        """
        Given a coin, get the current price ratio for every other enabled coin
        """
        ratio_dict: Dict[Pair, float] = {}
        prices: Dict[str, float] = {}

        scout_logs = []
        excluded_coin_symbols = [c.symbol for c in excluded_coins]
        for pair in self.db.get_pairs_from(coin):
            #skip excluded coins
            if pair.to_coin.symbol in excluded_coin_symbols:
                continue

            optional_coin_price = self.manager.get_buy_price(pair.to_coin + self.config.BRIDGE)
            prices[pair.to_coin_id] = optional_coin_price

            if optional_coin_price is None:
                self.logger.info(
                    "Skipping scouting... optional coin {} not found".format(pair.to_coin + self.config.BRIDGE)
                )
                continue

            scout_logs.append(LogScout(pair, pair.ratio, coin_price, optional_coin_price))

            # Obtain (current coin)/(optional coin)
            coin_opt_coin_ratio = coin_price / optional_coin_price

            transaction_fee = self.manager.get_fee(pair.from_coin, self.config.BRIDGE, True) + self.manager.get_fee(
                pair.to_coin, self.config.BRIDGE, False
            )

            ratio_dict[pair] = (
                coin_opt_coin_ratio - transaction_fee * self.config.SCOUT_MULTIPLIER * coin_opt_coin_ratio
            ) - pair.ratio
        self.db.batch_log_scout(scout_logs)
        return (ratio_dict, prices)


    def _jump_to_best_coin(self, coin: Coin, coin_price: float, excluded_coins: List[Coin] = []):
        """
        Given a coin, search for a coin to jump to
        pretend a lower coin price of given coin to determine if jump would still be profitable
        """
        simulated_sell_price = coin_price * 0.994
        if self.allow_trade == True:
            simulated_sell_price = coin_price

        ratio_dict, prices = self._get_ratios(coin, simulated_sell_price, excluded_coins)

        # keep only ratios bigger than zero
        ratio_dict = {k: v for k, v in ratio_dict.items() if v > 0}
        ratio_dict_all = {k: v for k, v in ratio_dict.items()}

        # ratio_dict_str = json.dumps(ratio_dict)
        # ratio_dict_all_str = json.dumps(ratio_dict_all)
        # self.logger.info(f"ratios: {ratio_dict_all_str}\n")

        # if we have any viable options, pick the one with the biggest ratio
        if ratio_dict:
            best_pair = max(ratio_dict, key=ratio_dict.get)

            if self.allow_trade == False:

                trailing_stop_price = simulated_sell_price * 0.996

                if self.trailing_stop is None:
                    self.trailing_stop = trailing_stop_price * 1.0056
                    self.logger.info(f"Probably will jump from {coin} to another one ({best_pair.to_coin.symbol})")
                    self.logger.info(f"{coin}: current price: {coin_price}")
                    self.logger.info(f"{coin}: init trailing stop: {self.trailing_stop}") # prozentualen abstand anzeigen?

                if trailing_stop_price >= self.trailing_stop:
                    self.trailing_stop = trailing_stop_price
                    print(f"{coin}: current price: {coin_price}. raising trailing stop: {self.trailing_stop}", end="\r")
                else:
                    if coin_price <= self.trailing_stop:
                        self.logger.info(f"{coin}: current price: {coin_price}")
                        self.logger.info(f"{coin}: reached trailing stop: {self.trailing_stop}") # prozentualen abstand anzeigen?
                        self.allow_trade = True
                    else:
                        print(f"{coin}: current price: {coin_price}. Did not reach trailing stop: {self.trailing_stop}", end="\r")

                return

            self.logger.info(f"Trailing stop ({self.trailing_stop}) triggered! Jumping from {coin} to {best_pair.to_coin_id}")

            self.transaction_through_bridge(best_pair, coin_price, prices[best_pair.to_coin_id])

            self.trailing_stop = None
            self.allow_trade = False

        else:
            if self.allow_trade == True:
                self.logger.info(f"Won't jump from {coin} to another one, ratio got worse")

            self.trailing_stop = None
            self.allow_trade = False

    def bridge_scout(self):
        """
        If we have any bridge coin leftover, buy a coin with it that we won't immediately trade out of
        """
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)

        for coin in self.db.get_coins():
            current_coin_price = self.manager.get_sell_price(coin + self.config.BRIDGE)

            if current_coin_price is None:
                continue

            ratio_dict, _ = self._get_ratios(coin, current_coin_price)
            if not any(v > 0 for v in ratio_dict.values()):
                # There will only be one coin where all the ratios are negative. When we find it, buy it if we can
                if bridge_balance > self.manager.get_min_notional(coin.symbol, self.config.BRIDGE.symbol):
                    self.logger.info(f"Will be purchasing {coin} using bridge coin")
                    result = self.manager.buy_alt(
                        coin, self.config.BRIDGE, self.manager.get_sell_price(coin + self.config.BRIDGE)
                    )
                    if result is not None:
                        self.db.set_current_coin(coin)
                        self.failed_buy_order = False
                        return coin
                    else:
                        self.failed_buy_order = True
        return None

    def update_values(self):
        """
        Log current value state of all altcoin balances against BTC and USDT in DB.
        """
        now = datetime.now()

        coins = self.db.get_coins(True)
        cv_batch = []
        for coin in coins:
            balance = self.manager.get_currency_balance(coin.symbol)
            if balance == 0:
                continue
            usd_value = self.manager.get_ticker_price(coin + self.config.BRIDGE_SYMBOL)
            btc_value = self.manager.get_ticker_price(coin + "BTC")

            cv = CoinValue(coin, balance, usd_value, btc_value, datetime=now)
            cv_batch.append(cv)
        self.db.batch_update_coin_values(cv_batch)


    # not used
    def track_last_prices(self):
        """
        Log current value state of all altcoin balances against BTC and USDT in var.
        """

        coins = self.db.get_coins(True)
        for coin in coins:
            balance = self.manager.get_currency_balance(coin.symbol)
            if balance == 0:
                continue
            usd_value = self.manager.get_ticker_price(coin + self.config.BRIDGE_SYMBOL)
            btc_value = self.manager.get_ticker_price(coin + "BTC")

            if coin + self.config.BRIDGE_SYMBOL not in self.last_prices:
                self.last_prices[coin + self.config.BRIDGE_SYMBOL] = [usd_value]
            else:
                self.last_prices[coin + self.config.BRIDGE_SYMBOL].append(usd_value)

            if coin + "BTC" not in self.last_prices:
                self.last_prices[coin + "BTC"] = [btc_value]
            else:
                self.last_prices[coin + "BTC"].append(btc_value)

            # keep just the 10 last values
            if len(self.last_prices[coin + self.config.BRIDGE_SYMBOL]) == 11:
                del self.last_prices[coin + self.config.BRIDGE_SYMBOL][0]

            if len(self.last_prices[coin + "BTC"]) == 11:
                del self.last_prices[coin + "BTC"][0]
