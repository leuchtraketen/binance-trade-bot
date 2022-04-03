import json

from .binance_api_manager import BinanceAPIManager
from .config import Config
from .database import Database, LogScout
from .logger import Logger

class AutoCoinSelector:
    def __init__(self, binance_manager: BinanceAPIManager, database: Database, logger: Logger, config: Config):
        self.manager = binance_manager
        self.db = database
        self.logger = logger
        self.config = config


    def get_coins_to_trade(self):

        self.logger.info(f"Using auto coin selector to get coins to trade - min volume: {self.config.AUTO_COIN_SELECTOR_MIN_VOLUME}")

        owned_coins = self.db.get_owned_coins()
        if self.config.AUTO_COIN_SELECTOR_ADD_OWNED_COINS:
            self.logger.info(f"Adding also previously owned coins {owned_coins} - min volume: {self.config.AUTO_COIN_SELECTOR_MIN_VOLUME_OWNED_COINS}")

        if self.config.AUTO_COIN_SELECTOR_ADD_COINS_FROM_LIST:
            self.logger.info(f"Adding also coins from list {self.config.SUPPORTED_COIN_LIST} - min volume: {self.config.AUTO_COIN_SELECTOR_MIN_VOLUME_COINS_FROM_LIST}")

        coins_to_trade = []

        tradable_coins = self.manager.get_tradable_coins(self.config.BRIDGE.symbol)

        current_coin = self.db.get_current_coin()
        if current_coin is None:
            current_coin = self.config.CURRENT_COIN_SYMBOL
        else:
            current_coin = current_coin.symbol

        for coin in tradable_coins:

            # append current coin if current coin isn't an option anymore yet we're still hodling it
            # we can't even remove it when it's blacklisted because we're simply HODLing it
            if current_coin and coin == current_coin and coin not in coins_to_trade:
                coins_to_trade.append(coin)
                self.logger.info(f"Adding {coin}: volume = {float(ticker['quoteVolume'])}")

            if coin in self.config.AUTO_COIN_SELECTOR_BLACKLIST:
                continue

            ticker = self.manager.get_ticker(coin + self.config.BRIDGE.symbol)
            ########## if float(ticker['quoteVolume']) >= self.config.AUTO_COIN_SELECTOR_MIN_VOLUME:
            ##########     coins_to_trade.append(coin)
            ##########     self.logger.info(f"Adding {coin}: volume = {float(ticker['quoteVolume'])}")

            # append owned_coins if configured so
            if self.config.AUTO_COIN_SELECTOR_ADD_OWNED_COINS:
                if coin in owned_coins:
                    if coin not in coins_to_trade:
                        if float(ticker['quoteVolume']) >= self.config.AUTO_COIN_SELECTOR_MIN_VOLUME_OWNED_COINS:
                            coins_to_trade.append(coin)
                            self.logger.info(f"Adding {coin}: volume = {float(ticker['quoteVolume'])}")


            # append supported_coin_list if configured so
            if self.config.AUTO_COIN_SELECTOR_ADD_COINS_FROM_LIST:
                if coin in self.config.SUPPORTED_COIN_LIST:
                    if coin not in coins_to_trade:
                        if float(ticker['quoteVolume']) >= self.config.AUTO_COIN_SELECTOR_MIN_VOLUME_COINS_FROM_LIST:
                            coins_to_trade.append(coin)
                            self.logger.info(f"Adding {coin}: volume = {float(ticker['quoteVolume'])}")


        return coins_to_trade
