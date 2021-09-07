from typing import List
from re import search
from binance.client import Client

from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.expression import and_

from .logger import Logger
from .config import Config
from .database import Database
from .binance_api_manager import BinanceAPIManager
from .auto_trader import AutoTrader
from .models.coin import Coin
from .models.pair import Pair

class WarmUpDatabase(Database):
    def __init__(self, logger: Logger, config: Config, uri="sqlite:///data/crypto_trading.db"):
        super().__init__(logger, config, uri)

    def set_coins_to_warmup(self, symbols: List[str], warmup_symbols: List[str]):
        session: Session

         # Add coins to the database and set them as enabled or not
        with self.db_session() as session:
            coins: List[Coin] = session.query(Coin).all()

            # For all warmup symbols and add them to the database if they don't exist
            for symbol in warmup_symbols:
                coin = next((coin for coin in coins if coin.symbol == symbol), None)
                if coin is None:
                    newCoin = Coin(symbol, False)
                    session.add(newCoin)
                    coins.append(newCoin)

            # For all the coins in the database, if the symbol no longer appears
            # in the config file, set the coin as disabled
            for coin in coins:
                if coin.symbol not in symbols:
                    coin.enabled = False

            # For all the symbols in the config file, add them to the database
            # if they don't exist
            for symbol in symbols:
                coin = next((coin for coin in coins if coin.symbol == symbol), None)
                if coin is None:
                    session.add(Coin(symbol))
                else:
                    coin.enabled = True

        # For all the combinations of coins in the database, add a pair to the database
        with self.db_session() as session:
            c1 = aliased(Coin)
            c2 = aliased(Coin)
            p = aliased(Pair)

            #select all pairs with non exiting pair entry in db and add the missing pair entries
            query = session.query(c1, c2).\
                join(c2, c2.symbol != c1.symbol).\
                outerjoin(p, and_(p.from_coin_id == c1.symbol, p.to_coin_id == c2.symbol)).\
                filter(p.id == None)

            pairs = query.all()
            for fromCoin, toCoin in pairs:
                #exclude bridge coin pairs
                if fromCoin.symbol != self.config.BRIDGE_SYMBOL and toCoin.symbol != self.config.BRIDGE_SYMBOL:
                    session.add(Pair(fromCoin, toCoin))

class WarmUpTrader(AutoTrader):

    def initialize_trade_thresholds(self):
        """
        Initialize the buying threshold of all the coins for trading between them
        """
        session: Session
        with self.db.db_session() as session:
            for pair in session.query(Pair).filter(Pair.ratio.is_(None)).all():
                from_coin_price = self.manager.get_ticker_price(pair.from_coin + self.config.BRIDGE)
                if from_coin_price is None:
                    self.logger.info(
                        "Skipping initializing {}, symbol not found".format(pair.from_coin + self.config.BRIDGE)
                    )
                    continue

                to_coin_price = self.manager.get_ticker_price(pair.to_coin + self.config.BRIDGE)
                if to_coin_price is None or to_coin_price == 0:
                    self.logger.info(
                        "Skipping initializing {}, symbol not found".format(pair.to_coin + self.config.BRIDGE)
                    )
                    continue

                pair.ratio = from_coin_price / to_coin_price
                pair.from_coin_price = from_coin_price
                pair.to_coin_price = to_coin_price

def warmup_database(coin_list: List[str] = None, db_path = "data/crypto_trading.db", config: Config = None):
    logger = Logger()
    logger.info("Starting database warmup")

    logger.info(f'Will be using {db_path} as database')
    dbPathUri = f"sqlite:///{db_path}"

    config = config or Config()
    db = WarmUpDatabase(logger, config, dbPathUri)
    manager = BinanceAPIManager.create_manager(config, db, logger)
    # check if we can access API feature that require valid config
    try:
        _ = manager.get_account()
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Couldn't access Binance API - API keys may be wrong or lack sufficient permissions")
        logger.error(e)
        return

    logger.info("Creating database schema if it doesn't already exist")
    db.create_database()
    logger.info("Done creating database schema")

    warmup_coin_list = coin_list or get_all_bridge_coins(manager.binance_client, config)
    logger.info(f'Going to warm up the following coins: {warmup_coin_list}')

    logger.info("Adding coins and pairs to database for warm up")
    db.set_coins_to_warmup(config.SUPPORTED_COIN_LIST, warmup_coin_list)
    logger.info("Done adding coins to warm up")

    logger.info("Starting warm up")
    trader = WarmUpTrader(manager, db, logger, config)
    trader.initialize_trade_thresholds()
    logger.info("Done. Your database is now warmed up")
    
    manager.stream_manager.close()

def get_all_bridge_coins(client: Client, config: Config):
    #fetch all tickers
    all_symbols = client.get_symbol_ticker()

    all_bridge_coins: List[str] = []
    for pair in all_symbols:
        symbol = pair["symbol"]
        #search for coins tradeable via bridge. exlude UP DOWN BEAR BULL stuff
        if search(f"^\w*(?<!UP){config.BRIDGE_SYMBOL}$", symbol) \
            and search(f"^\w*(?<!DOWN){config.BRIDGE_SYMBOL}$", symbol)\
            and search(f"^\w*(?<!BEAR){config.BRIDGE_SYMBOL}$", symbol)\
            and search(f"^\w*(?<!BULL){config.BRIDGE_SYMBOL}$", symbol)\
        :
            all_bridge_coins.append(symbol.replace(config.BRIDGE_SYMBOL, ""))
    return all_bridge_coins
