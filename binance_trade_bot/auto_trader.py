from collections import defaultdict
from datetime import datetime
from typing import Dict, List
import json
import time

from sqlalchemy.orm import Session

from .binance_api_manager import BinanceAPIManager
from .config import Config
from .database import Database, LogScout
from .logger import Logger
from .models import Coin, CoinValue, Pair

import colorama
from colorama import Fore, Back, Style




class RatioDebug:

    from_coin_price_now = None
    to_coin_price_now = None
    from_coin_price_database = None
    to_coin_price_database = None

    def __init__(self):
        True

    def __repr__(self):
        return f"now: {self.from_coin_price_now} / {self.to_coin_price_now}, database: {self.from_coin_price_database} / {self.to_coin_price_database}"


class AutoTrader:
    def __init__(self, binance_manager: BinanceAPIManager, database: Database, logger: Logger, config: Config):
        self.manager = binance_manager
        self.db = database
        self.logger = logger
        self.config = config
        self.failed_buy_order = False

        self.trailing_stop = None
        self.allow_trade = not self.config.TRAILING_STOP

        self.trailing_stop_timeout = None

    def initialize(self):
        self.initialize_trade_thresholds()

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
                pair.from_coin_price = from_coin_price
                pair.to_coin_price = coin_price

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
                    if pair.from_coin == self.config.BRIDGE:
                        continue
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
                    pair.from_coin_price = from_coin_price
                    pair.to_coin_price = to_coin_price

    def scout(self):
        """
        Scout for potential jumps from the current coin to another coin
        """
        raise NotImplementedError()


    def _get_simulated_coin_price(self, coin_price, log: bool):
        if self.trailing_stop is not None:
            simulated_coin_price = self.trailing_stop * self.config.TRAILING_STOP_RATIO_CALC_COIN_PRICE_MULTIPLIER
        else:
            simulated_coin_price = coin_price * self.config.TRAILING_STOP_COIN_PRICE_MULTIPLIER_INIT * self.config.TRAILING_STOP_RATIO_CALC_COIN_PRICE_MULTIPLIER

        if self.allow_trade == True:
            simulated_coin_price = coin_price

        if log:
            self.logger.info(f"simulated sell price: {simulated_coin_price}", notification=False)


        return simulated_coin_price


    def _get_ratios(self, coin: Coin, coin_price, excluded_coins: List[Coin] = []):
        """
        Given a coin, get the current price ratio for every other enabled coin
        """
        ratio_dict: Dict[Pair, float] = {}
        prices: Dict[str, float] = {}
        ratio_debug: Dict[Pair, RatioDebug] = {}

        scout_logs = []
        excluded_coin_symbols = [c.symbol for c in excluded_coins]
        for pair in self.db.get_pairs_from(coin):
            #skip excluded coins
            if pair.to_coin.symbol in excluded_coin_symbols:
                continue

            candidate_coin_price = self.manager.get_buy_price(pair.to_coin + self.config.BRIDGE)
            prices[pair.to_coin_id] = candidate_coin_price

            if candidate_coin_price is None:
                self.logger.info("Skipping scouting... candidate coin {} not found".format(pair.to_coin + self.config.BRIDGE))
                continue

            scout_logs.append(LogScout(pair, pair.ratio, coin_price, candidate_coin_price))

            # Obtain (current coin)/(optional coin)
            current2possible_ratio = coin_price / candidate_coin_price

            from_fee =        self.manager.get_fee(pair.from_coin, self.config.BRIDGE, True)
            to_fee =          self.manager.get_fee(pair.to_coin,   self.config.BRIDGE, False)
            transaction_fee = from_fee + to_fee - from_fee * to_fee

            ######  Original formula:  #####
            #
            #   ((current2possible_ratio - transaction_fee * self.config.SCOUT_MULTIPLIER * current2possible_ratio) - pair.ratio)
            #
            #

            ######  Normalized formula: #####
            #
            #   ((current2possible_ratio - transaction_fee * self.config.SCOUT_MULTIPLIER * current2possible_ratio) - pair.ratio) * 100 / pair.ratio
            #   ((current2possible_ratio - transaction_fee * self.config.SCOUT_MULTIPLIER * current2possible_ratio) / pair.ratio - 1) * 100
            #   ((1                      - transaction_fee * self.config.SCOUT_MULTIPLIER) * current2possible_ratio / pair.ratio - 1) * 100
            #
            #  short:
            #
            #   ((1 - transaction_fee * self.config.SCOUT_MULTIPLIER) * current2possible_ratio / pair.ratio - 1) * 100
            #
            #  from: https://github.com/edeng23/binance-trade-bot/issues/385
            #

            ######  Margin formula: #####
            #
            #   ((1 - transaction_fee) * current2possible_ratio / pair.ratio - 1) * 100 - self.config.SCOUT_MARGIN
            #
            #  from: https://github.com/edeng23/binance-trade-bot/pull/417/files#diff-d2579cf2f5170dacac5d1dbfa2ac255210dbd8fba50b28aa04cbee38aed1e9fbR138
            #

            if self.config.USE_MARGIN:
                ratio_dict[pair] = ((1 - transaction_fee) * current2possible_ratio / pair.ratio - 1) * 100 - self.config.SCOUT_MARGIN
            else:
                ratio_dict[pair] = ((1 - transaction_fee * self.config.SCOUT_MULTIPLIER) * current2possible_ratio / pair.ratio - 1) * 100


            d = RatioDebug()
            d.from_coin_price_now = coin_price
            d.to_coin_price_now = candidate_coin_price
            d.from_coin_price_database = pair.from_coin_price
            d.to_coin_price_database = pair.to_coin_price
            ratio_debug[pair] = d


        self.db.batch_log_scout(scout_logs)
        return (ratio_dict, prices, ratio_debug)


    def _get_jump_candidate_log(self, coin: Coin, coin_price: float, excluded_coins: List[Coin] = []):
        ratio_dict_all, prices, ratio_debug = self._get_ratios(coin, self._get_simulated_coin_price(coin_price, False), excluded_coins)

        # keep only ratios bigger than zero
        ratio_dict = {k: v for k, v in ratio_dict_all.items() if v > 0}

        # ratio_dict_str = json.dumps(ratio_dict)
        # ratio_dict_all_str = json.dumps(ratio_dict_all)
        # self.logger.info(f"ratios: {ratio_dict_all_str}\n")

        ratio_dict_all_sorted = {k: v for k, v in sorted(ratio_dict_all.items(), key=lambda item: item[1])}

#        self.logger.info(f"\n")
        for f_pair, f_ratio in ratio_dict_all_sorted.items():
#            self.logger.info(f"pair: {f_pair}, ratio: {f_ratio}")
            True

        if self.config.SCOUT_DEBUG:
            s = ""
            s += "best candidates: "
            sep = ""
            for f_pair, f_ratio in reversed({k: ratio_dict_all_sorted[k] for k in list(ratio_dict_all_sorted)[-4:]}.items()):
                f_ratio_rounded = round(f_ratio, 5)
                f_ratio_debug = ratio_debug[f_pair]
                s += sep
                s += f"{f_pair.to_coin.symbol} ({f_ratio_rounded})"
                sep = ", "
            s += ", "
            s += "worst candidates: "
            sep = ""
            for f_pair, f_ratio in {k: ratio_dict_all_sorted[k] for k in list(ratio_dict_all_sorted)[:2]}.items():
                f_ratio_rounded = round(f_ratio, 5)
                f_ratio_debug = ratio_debug[f_pair]
                s += sep
                s += f"{f_pair.to_coin.symbol} ({f_ratio_rounded})"
                sep = ", "
            s += "\n"
            s += "best candidates:\n"
            for f_pair, f_ratio in reversed({k: ratio_dict_all_sorted[k] for k in list(ratio_dict_all_sorted)[-4:]}.items()):
                f_ratio_rounded = round(f_ratio, 5)
                f_ratio_debug = ratio_debug[f_pair]
                s += f"  - {f_pair.to_coin.symbol} ({f_ratio_rounded} [{f_ratio_debug}])\n"
            s += "worst candidates:\n"
            for f_pair, f_ratio in {k: ratio_dict_all_sorted[k] for k in list(ratio_dict_all_sorted)[:2]}.items():
                f_ratio_rounded = round(f_ratio, 5)
                f_ratio_debug = ratio_debug[f_pair]
                s += f"  - {f_pair.to_coin.symbol} ({f_ratio_rounded} [{f_ratio_debug}])\n"
        else:
            s = ""
            s += "best candidates: "
            sep = ""
            for f_pair, f_ratio in reversed({k: ratio_dict_all_sorted[k] for k in list(ratio_dict_all_sorted)[-4:]}.items()):
                f_ratio_rounded = round(f_ratio, 5)
                s += sep
                s += f"{f_pair.to_coin.symbol} ({f_ratio_rounded})"
                sep = ", "
            s += ", "
            s += "worst candidates: "
            sep = ""
            for f_pair, f_ratio in {k: ratio_dict_all_sorted[k] for k in list(ratio_dict_all_sorted)[:2]}.items():
                f_ratio_rounded = round(f_ratio, 5)
                s += sep
                s += f"{f_pair.to_coin.symbol} ({f_ratio_rounded})"
                sep = ", "

        return s


    def _jump_to_best_coin(self, coin: Coin, coin_price: float, excluded_coins: List[Coin] = []):
        """
        Given a coin, search for a coin to jump to
        pretend a lower coin price of given coin to determine if jump would still be profitable
        """

        self.logger.info(f"current {Fore.MAGENTA}{coin}{Style.RESET_ALL} price: {Back.BLACK}{Fore.WHITE}{Style.BRIGHT} {coin_price} {Style.RESET_ALL} {self.config.BRIDGE}", notification=False)
        self.logger.info(f"trailing stop: {Fore.CYAN if self.trailing_stop is not None else Fore.RED}{self.trailing_stop}{Style.RESET_ALL}", notification=False)

        if self.trailing_stop_timeout is not None:
            self.logger.info(f"trailing stop timeout in: {str(self.trailing_stop_timeout-time.time()) + 's'}", notification=False)

        ratio_dict_all, prices, ratio_debug = self._get_ratios(coin, self._get_simulated_coin_price(coin_price, True), excluded_coins)

        # keep only ratios bigger than zero
        ratio_dict = {k: v for k, v in ratio_dict_all.items() if v > 0}

        if self.config.TRAILING_STOP:

            # if we have any viable options, pick the one with the biggest ratio
            if ratio_dict:

                best_pair = max(ratio_dict, key=ratio_dict.get)

                self.logger.info(f"best pair = {best_pair}", notification=False)

                if self.allow_trade == False:

                    trailing_stop_price = coin_price * self.config.TRAILING_STOP_COIN_PRICE_MULTIPLIER

                    if self.trailing_stop is None:
                        self.trailing_stop = coin_price * self.config.TRAILING_STOP_COIN_PRICE_MULTIPLIER_INIT
                        self.trailing_stop_timeout = time.time()+60 # init with a lower timeout, if there is movement, the timeout will be set to a higher value
                        self.logger.info(f"Will probably jump from {coin} to <{best_pair.to_coin.symbol}>")
                        self.logger.info(f"{coin}: current price: {coin_price} {self.config.BRIDGE}")
                        self.logger.info(f"{coin}: trailing stop: {self.trailing_stop} {self.config.BRIDGE}") # prozentualen abstand anzeigen?

                    if trailing_stop_price >= self.trailing_stop:
                        self.trailing_stop = trailing_stop_price
                        self.trailing_stop_timeout = time.time()+180
                        self.logger.info(f"{coin}: current price: {coin_price} {self.config.BRIDGE}. trailing stop: {self.trailing_stop} {self.config.BRIDGE} {Back.BLUE}{Fore.CYAN}{Style.BRIGHT} ↑↑↑ {Style.RESET_ALL}", notification=False)
                    else:
                        if coin_price <= self.trailing_stop:
                            self.logger.info(f"{coin}: current price: {coin_price} {self.config.BRIDGE}")
                            self.logger.info(f"{coin}: trailing stop: {self.trailing_stop} {self.config.BRIDGE} REACHED!") # prozentualen abstand anzeigen?
                            self.allow_trade = True
                        else:
                            if self.trailing_stop_timeout < time.time():
                                self.allow_trade = True
                                self.logger.info(f"{coin}: TRAILING STOP TIMEOUT REACHED!")

                            self.logger.info(f"{coin}: current price: {coin_price}. trailing stop: {self.trailing_stop} {self.config.BRIDGE}", notification=False)

                    return

                self.logger.info(f"Jumping from {coin} to <{best_pair.to_coin_id}>")

                self.transaction_through_bridge(best_pair, coin_price, prices[best_pair.to_coin_id])

                self.trailing_stop = None
                self.allow_trade = not self.config.TRAILING_STOP
                self.trailing_stop_timeout = None

            else:
                if self.allow_trade == True:
                    self.logger.info(f"{Fore.RED}Won't jump{Style.RESET_ALL} from {coin} to another one, ratio got worse")
                else:
                    if self.trailing_stop is not None:
                        self.logger.info(f"{Fore.RED}Removing trailing stop{Style.RESET_ALL}, ratio got worse")

                self.trailing_stop = None
                self.allow_trade = not self.config.TRAILING_STOP
                self.trailing_stop_timeout = None

        else: # if not self.config.TRAILING_STOP:

            # if we have any viable options, pick the one with the biggest ratio
            if ratio_dict:
                best_pair = max(ratio_dict, key=ratio_dict.get)
                self.logger.info(f"best pair = {best_pair}", notification=False)
                self.logger.info(f"Jumping from {coin} to <{best_pair.to_coin_id}>")
                self.transaction_through_bridge(best_pair, coin_price, prices[best_pair.to_coin_id])



    def bridge_scout(self):
        """
        If we have any bridge coin leftover, buy a coin with it that we won't immediately trade out of
        """
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)

        for coin in self.db.get_coins():
            current_coin_price = self.manager.get_sell_price(coin + self.config.BRIDGE)

            if current_coin_price is None:
                continue

            ratio_dict, prices, ratio_debug = self._get_ratios(coin, current_coin_price)
            if not any(v > 0 for v in ratio_dict.values()):
                # There will only be one coin where all the ratios are negative. When we find it, buy it if we can
                if bridge_balance > self.manager.get_min_notional(coin.symbol, self.config.BRIDGE.symbol):
                    self.logger.info(f"Will be purchasing {coin} using bridge coin")
                    result = self.manager.buy_alt(coin, self.config.BRIDGE, self.manager.get_sell_price(coin + self.config.BRIDGE))
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
