#!python3

from .binance_api_manager import BinanceAPIManager
from .config import Config
from .database import Database
from .logger import Logger
from .scheduler import SafeScheduler
from .strategies import get_strategy
from .auto_coin_selector import AutoCoinSelector

import sys
import time
import readchar

from threading import Thread

def key_thread():
    k = None
    while True:
        k = repr(readchar.readkey())
        print(f"key: {k}")

def main():
    logger = Logger()
    logger.info("Starting")

    config = Config()
    db = Database(logger, config)

    if config.ENABLE_PAPER_TRADING:
        manager = BinanceAPIManager.create_manager_paper_trading(config, db, logger, {config.BRIDGE.symbol: 21_000.0})
    else:
        manager = BinanceAPIManager.create_manager(config, db, logger)


    logger.info("Creating database schema if it doesn't already exist")
    db.create_database()


    if config.SUPPORTED_COINS_METHOD == 'auto':
        auto_coin_selector = AutoCoinSelector(manager, db, logger, config)
        coins_to_trade = auto_coin_selector.get_coins_to_trade()
        db.set_coins(coins_to_trade)
    else:
        db.set_coins(config.SUPPORTED_COIN_LIST)


    # needs to be executed AFTER updating to new coins!
    manager.setup_websockets()

    
    # check if we can access API feature that require valid config
    try:
        _ = manager.get_account()
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Couldn't access Binance API - API keys may be wrong or lack sufficient permissions")
        logger.error(e)
        return
    strategy = get_strategy(config.STRATEGY)
    if strategy is None:
        logger.error("Invalid strategy name")
        return
    trader = strategy(manager, db, logger, config)
    logger.info(f"Chosen strategy: {config.STRATEGY}")

    if config.USE_MARGIN:
        logger.warning(f"Use scout margin: {config.SCOUT_MARGIN} %")
    else:
        logger.warning(f"Use scout multiplier: {config.SCOUT_MULTIPLIER}")

    if config.ENABLE_PAPER_TRADING:
        logger.warning("RUNNING IN PAPER-TRADING MODE")
    else:
        logger.warning("RUNNING IN REAL TRADING MODE")

    logger.info(f"Buy type: {config.BUY_ORDER_TYPE}, Sell type: {config.SELL_ORDER_TYPE}")
    logger.info(f"Max price changes for buys: {config.BUY_MAX_PRICE_CHANGE}, Max price changes for sells: {config.SELL_MAX_PRICE_CHANGE}")
    logger.info(f"Using {config.PRICE_TYPE} prices")

    thread = Thread(target = key_thread)
    ## thread.start()
    # time.sleep(10000)


    trader.initialize()

    schedule = SafeScheduler(logger)
    schedule.every(config.SCOUT_SLEEP_TIME).seconds.do(trader.scout).tag("scouting")
    schedule.every(15).seconds.do(trader.track_last_prices).tag("track last prices")

    #if config.SUPPORTED_COINS_METHOD == 'auto':
    #    schedule.every(10).minutes.do(db.set_coins, symbols=auto_coin_selector.get_coins_to_trade()).tag("update supported coins")
    # todo: blocking, also ratio init has to be done again, should not be executed while an order is running or trailing stop or anything like that....

    schedule.every(1).minutes.do(trader.update_values).tag("updating value history")
    schedule.every(1).minutes.do(db.prune_scout_history).tag("pruning scout history")
    schedule.every(1).hours.do(db.prune_value_history).tag("pruning value history")
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    finally:
        manager.stream_manager.close()
