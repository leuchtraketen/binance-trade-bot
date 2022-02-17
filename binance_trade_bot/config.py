import configparser
import os

import binance.client

from .models import Coin

CFG_FL_NAME = "user.cfg"
USER_CFG_SECTION = "binance_user_config"


class Config:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    ORDER_TYPE_MARKET = "market"
    ORDER_TYPE_LIMIT = "limit"

    PRICE_TYPE_ORDERBOOK = "orderbook"
    PRICE_TYPE_TICKER = "ticker"

    def __init__(self):
        # Init config
        config = configparser.ConfigParser()
        config["DEFAULT"] = {
            "bridge": "USDT",
            "scout_multiplier": "5",
            "scout_margin": "0.8",
            "scout_sleep_time": "5",
            "scout_debug":"true",
            "use_margin":"true",
            "hourToKeepScoutHistory": "1",
            "tld": "com",
            "trade_fee": "auto",
            "strategy": "default",
            "enable_paper_trading": "false",
            "sell_timeout": "0",
            "buy_timeout": "0",
            "sell_order_type": self.ORDER_TYPE_MARKET,
            "buy_order_type": self.ORDER_TYPE_LIMIT,
            "sell_max_price_change": "0.005",
            "buy_max_price_change": "0.005",
            "price_type": self.PRICE_TYPE_ORDERBOOK,
            "accept_losses": "false",
            "max_idle_hours": "3",
            "ratio_adjust_weight": "100",
            "auto_adjust_bnb_balance": "false",
            "auto_adjust_bnb_balance_rate": "3",
            "trailing_stop": "true",
            "trailing_stop_coin_price_multiplier_init": "0.9965",
            "trailing_stop_coin_price_multiplier": "0.9955",
            "trailing_stop_ratio_calc_coin_price_multiplier": "0.9995",
            "supported_coins_method": "list",
            "auto_coin_selector_min_volume": "80000000",
            "auto_coin_selector_add_coins_from_list": "False",
            "auto_coin_selector_add_owned_coins": "False",
            "auto_coin_selector_min_volume_owned_coins": "0",
            "auto_coin_selector_min_volume_coins_from_list": "0",
            "use_funding_wallet": "True",
            "min_balance_bridge_transfer_main2funding": "10",
            "max_balance_bridge_transfer_main2funding": "10000",
            "min_balance_bridge_transfer_funding2main": "10",
            "max_balance_bridge_transfer_funding2main": "10000",
            "min_balance_bridge_main_during_jump": "50",
            "min_balance_bridge_funding_after_jump": "0",
        }

        if not os.path.exists(CFG_FL_NAME):
            print("No configuration file (user.cfg) found! See README. Assuming default config...")
            config[USER_CFG_SECTION] = {}
        else:
            config.read(CFG_FL_NAME)

        self.BRIDGE_SYMBOL = os.environ.get("BRIDGE_SYMBOL") or config.get(USER_CFG_SECTION, "bridge")
        self.BRIDGE = Coin(self.BRIDGE_SYMBOL, False)

        # Prune settings
        self.SCOUT_HISTORY_PRUNE_TIME = float(
            os.environ.get("HOURS_TO_KEEP_SCOUTING_HISTORY") or config.get(USER_CFG_SECTION, "hourToKeepScoutHistory")
        )

        # Get config for scout
        self.SCOUT_MULTIPLIER = float(
            os.environ.get("SCOUT_MULTIPLIER") or config.get(USER_CFG_SECTION, "scout_multiplier")
        )
        self.SCOUT_MARGIN = float(
            os.environ.get("SCOUT_MARGIN") or config.get(USER_CFG_SECTION, "scout_margin")
        )
        self.SCOUT_SLEEP_TIME = int(
            os.environ.get("SCOUT_SLEEP_TIME") or config.get(USER_CFG_SECTION, "scout_sleep_time")
        )

        self.RATIO_ADJUST_WEIGHT = int(
            os.environ.get("RATIO_ADJUST_WEIGHT") or config.get(USER_CFG_SECTION, "ratio_adjust_weight")
        )

        self.RATIO_ADJUST_WEIGHT = int(
            os.environ.get("RATIO_ADJUST_WEIGHT") or config.get(USER_CFG_SECTION, "ratio_adjust_weight")
        )

        self.MIN_BALANCE_BRIDGE_TRANSFER_MAIN2FUNDING = int(
            os.environ.get("MIN_BALANCE_BRIDGE_TRANSFER_MAIN2FUNDING") or config.get(USER_CFG_SECTION,
                                                                                     "min_balance_bridge_transfer_main2funding")
        )
        self.MAX_BALANCE_BRIDGE_TRANSFER_MAIN2FUNDING = int(
            os.environ.get("MAX_BALANCE_BRIDGE_TRANSFER_MAIN2FUNDING") or config.get(USER_CFG_SECTION,
                                                                                     "max_balance_bridge_transfer_main2funding")
        )
        self.MIN_BALANCE_BRIDGE_TRANSFER_FUNDING2MAIN = int(
            os.environ.get("MIN_BALANCE_BRIDGE_TRANSFER_FUNDING2MAIN") or config.get(USER_CFG_SECTION,
                                                                                     "min_balance_bridge_transfer_funding2main")
        )
        self.MAX_BALANCE_BRIDGE_TRANSFER_FUNDING2MAIN = int(
            os.environ.get("MAX_BALANCE_BRIDGE_TRANSFER_FUNDING2MAIN") or config.get(USER_CFG_SECTION,
                                                                                     "max_balance_bridge_transfer_funding2main")
        )
        self.MIN_BALANCE_BRIDGE_MAIN_DURING_JUMP = int(
            os.environ.get("MIN_BALANCE_BRIDGE_MAIN_DURING_JUMP") or config.get(USER_CFG_SECTION,
                                                                                "min_balance_bridge_main_during_jump")
        )
        self.MIN_BALANCE_BRIDGE_FUNDING_AFTER_JUMP = int(
            os.environ.get("MIN_BALANCE_BRIDGE_FUNDING_AFTER_JUMP") or config.get(USER_CFG_SECTION,
                                                                                  "min_balance_bridge_funding_after_jump")
        )

        # Get config for binance
        self.BINANCE_API_KEY = os.environ.get("API_KEY") or config.get(USER_CFG_SECTION, "api_key")
        self.BINANCE_API_SECRET_KEY = os.environ.get("API_SECRET_KEY") or config.get(USER_CFG_SECTION, "api_secret_key")
        self.BINANCE_TLD = os.environ.get("TLD") or config.get(USER_CFG_SECTION, "tld")

        self.CURRENT_COIN_SYMBOL = os.environ.get("CURRENT_COIN_SYMBOL") or config.get(USER_CFG_SECTION, "current_coin")

        # Get supported coin list from the environment
        supported_coin_list = [
            coin.strip() for coin in os.environ.get("SUPPORTED_COIN_LIST", "").split() if coin.strip()
        ]

        # Get supported coin list from supported_coin_list file
        if not supported_coin_list and os.path.exists("supported_coin_list"):
            with open("supported_coin_list") as rfh:
                for line in rfh:
                    line = line.strip()
                    if not line or line.startswith("#") or line in supported_coin_list:
                        continue
                    supported_coin_list.append(line)
        if self.CURRENT_COIN_SYMBOL and self.CURRENT_COIN_SYMBOL not in supported_coin_list:
            supported_coin_list.append(self.CURRENT_COIN_SYMBOL)
        self.SUPPORTED_COIN_LIST = supported_coin_list

        self.TRADE_FEE = os.environ.get("TRADE_FEE") or config.get(USER_CFG_SECTION, "trade_fee")

        self.STRATEGY = os.environ.get("STRATEGY") or config.get(USER_CFG_SECTION, "strategy")

        self.SCOUT_DEBUG = str(os.environ.get("SCOUT_DEBUG") or config.get(USER_CFG_SECTION, "scout_debug")).lower() == "true"

        self.USE_MARGIN = str(os.environ.get("USE_MARGIN") or config.get(USER_CFG_SECTION, "use_margin")).lower() == "true"

        enable_paper_trading_str = os.environ.get("ENABLE_PAPER_TRADING") or config.get(USER_CFG_SECTION, "enable_paper_trading")
        self.ENABLE_PAPER_TRADING = enable_paper_trading_str == "true" or enable_paper_trading_str == "True"

        self.SELL_TIMEOUT = os.environ.get("SELL_TIMEOUT") or config.get(USER_CFG_SECTION, "sell_timeout")
        self.BUY_TIMEOUT = os.environ.get("BUY_TIMEOUT") or config.get(USER_CFG_SECTION, "buy_timeout")

        order_type_map = {
            self.ORDER_TYPE_LIMIT: binance.client.Client.ORDER_TYPE_LIMIT,
            self.ORDER_TYPE_MARKET: binance.client.Client.ORDER_TYPE_MARKET,
        }

        sell_order_type = os.environ.get("SELL_ORDER_TYPE") or config.get(
            USER_CFG_SECTION, "sell_order_type", fallback=self.ORDER_TYPE_MARKET
        )
        if sell_order_type not in order_type_map:
            raise Exception(
                f"{self.ORDER_TYPE_LIMIT} or {self.ORDER_TYPE_MARKET} expected, got {sell_order_type}"
                "for sell_order_type"
            )
        self.SELL_ORDER_TYPE = order_type_map[sell_order_type]

        self.SELL_MAX_PRICE_CHANGE = os.environ.get("SELL_MAX_PRICE_CHANGE") or config.get(USER_CFG_SECTION, "sell_max_price_change")

        buy_order_type = os.environ.get("BUY_ORDER_TYPE") or config.get(
            USER_CFG_SECTION, "buy_order_type", fallback=self.ORDER_TYPE_LIMIT
        )
        if buy_order_type not in order_type_map:
            raise Exception(
                f"{self.ORDER_TYPE_LIMIT} or {self.ORDER_TYPE_MARKET} expected, got {buy_order_type}"
                "for buy_order_type"
            )
        #if buy_order_type == self.ORDER_TYPE_MARKET:
            #raise Exception(
            #    "Market buys are reported to do extreme losses, they are disabled right now,"
            #    "comment this line only if you know what you're doing"
            #)
        self.BUY_ORDER_TYPE = order_type_map[buy_order_type]

        self.BUY_MAX_PRICE_CHANGE = os.environ.get("BUY_MAX_PRICE_CHANGE") or config.get(USER_CFG_SECTION, "buy_max_price_change")

        price_types = {
            self.PRICE_TYPE_ORDERBOOK,
            self.PRICE_TYPE_TICKER
        }

        price_type = os.environ.get("PRICE_TYPE") or config.get(
            USER_CFG_SECTION, "price_type", fallback=self.PRICE_TYPE_ORDERBOOK
        )
        if price_type not in price_types:
            raise Exception(f"{self.PRICE_TYPE_ORDERBOOK} or {self.PRICE_TYPE_TICKER} expected, got {price_type} for price_type")
        self.PRICE_TYPE = price_type

        accept_losses_str = os.environ.get("ACCEPT_LOSSES") or config.get(USER_CFG_SECTION, "accept_losses")
        self.ACCEPT_LOSSES = accept_losses_str == 'true' or accept_losses_str == 'True'

        self.MAX_IDLE_HOURS = os.environ.get("MAX_IDLE_HOURS") or config.get(USER_CFG_SECTION, "max_idle_hours")

        auto_adjust_bnb_balance_str = os.environ.get("AUTO_ADJUST_BNB_BALANCE") or config.get(USER_CFG_SECTION, "auto_adjust_bnb_balance")
        self.AUTO_ADJUST_BNB_BALANCE = str(auto_adjust_bnb_balance_str).lower() == "true"

        self.AUTO_ADJUST_BNB_BALANCE_RATE = float(
            os.environ.get("AUTO_ADJUST_BNB_BALANCE_RATE") or config.get(USER_CFG_SECTION, "auto_adjust_bnb_balance_rate")
        )

        trailing_stop_str = os.environ.get("TRAILING_STOP") or config.get(USER_CFG_SECTION, "trailing_stop")
        self.TRAILING_STOP = str(trailing_stop_str).lower() == "true"

        self.TRAILING_STOP_COIN_PRICE_MULTIPLIER_INIT = float(
            os.environ.get("TRAILING_STOP_COIN_PRICE_MULTIPLIER_INIT") or config.get(USER_CFG_SECTION, "trailing_stop_coin_price_multiplier_init")
        )

        self.TRAILING_STOP_COIN_PRICE_MULTIPLIER = float(
            os.environ.get("TRAILING_STOP_COIN_PRICE_MULTIPLIER") or config.get(USER_CFG_SECTION, "trailing_stop_coin_price_multiplier")
        )

        self.TRAILING_STOP_RATIO_CALC_COIN_PRICE_MULTIPLIER = float(
            os.environ.get("TRAILING_STOP_RATIO_CALC_COIN_PRICE_MULTIPLIER") or config.get(USER_CFG_SECTION, "trailing_stop_ratio_calc_coin_price_multiplier")
        )

        supported_coins_method_str = os.environ.get("SUPPORTED_COINS_METHOD") or config.get(USER_CFG_SECTION, "supported_coins_method")
        self.SUPPORTED_COINS_METHOD = str(supported_coins_method_str).lower()

        # Get auto coin selector blacklist list from the environment
        auto_coin_selector_blacklist = [
            coin.strip() for coin in os.environ.get("AUTO_COIN_SELECTOR_BLACKLIST", "").split() if coin.strip()
        ]

        # Get auto coin selector blacklist from auto_coin_selector_blacklist file
        if not auto_coin_selector_blacklist and os.path.exists("auto_coin_selector_blacklist"):
            with open("auto_coin_selector_blacklist") as rfh:
                for line in rfh:
                    line = line.strip()
                    if not line or line.startswith("#") or line in auto_coin_selector_blacklist:
                        continue
                    auto_coin_selector_blacklist.append(line)
        self.AUTO_COIN_SELECTOR_BLACKLIST = auto_coin_selector_blacklist

        self.AUTO_COIN_SELECTOR_MIN_VOLUME = float(os.environ.get("AUTO_COIN_SELECTOR_MIN_VOLUME") or config.get(USER_CFG_SECTION, "auto_coin_selector_min_volume"))
        self.AUTO_COIN_SELECTOR_MIN_VOLUME_OWNED_COINS = float(os.environ.get("AUTO_COIN_SELECTOR_MIN_VOLUME_OWNED_COINS") or config.get(USER_CFG_SECTION, "auto_coin_selector_min_volume_owned_coins"))
        self.AUTO_COIN_SELECTOR_MIN_VOLUME_COINS_FROM_LIST = float(os.environ.get("AUTO_COIN_SELECTOR_MIN_VOLUME_COINS_FROM_LIST") or config.get(USER_CFG_SECTION, "auto_coin_selector_min_volume_coins_from_list"))

        auto_coin_selector_add_coins_from_list_str = os.environ.get("AUTO_COIN_SELECTOR_ADD_COINS_FROM_LIST") or config.get(USER_CFG_SECTION,"auto_coin_selector_add_coins_from_list")
        self.AUTO_COIN_SELECTOR_ADD_COINS_FROM_LIST = str(auto_coin_selector_add_coins_from_list_str).lower() == "true"

        auto_coin_selector_add_owned_coins_str = os.environ.get("AUTO_COIN_SELECTOR_ADD_OWNED_COINS") or config.get(USER_CFG_SECTION,"auto_coin_selector_add_owned_coins")
        self.AUTO_COIN_SELECTOR_ADD_OWNED_COINS = str(auto_coin_selector_add_owned_coins_str).lower() == "true"

        self.USE_FUNDING_WALLET = str(os.environ.get("USE_FUNDING_WALLET") or config.get(USER_CFG_SECTION, "use_funding_wallet")).lower() == "true"
