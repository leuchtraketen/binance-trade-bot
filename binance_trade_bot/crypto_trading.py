#!python3
import time

from .binance_api_manager import BinanceAPIManager
from .config import Config
from .database import Database
from .logger import Logger
from .scheduler import SafeScheduler
from .strategies import get_strategy


def main():
    logger = Logger()
    logger.info("Starting")

    config = Config()
    db = Database(logger, config)
    if config.ENABLE_PAPER_TRADING:
        manager = BinanceAPIManager.create_manager_paper_trading(config, db, logger, {config.BRIDGE.symbol: 21_000.0})
    else:
        manager = BinanceAPIManager.create_manager(config, db, logger)

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
    if config.ENABLE_PAPER_TRADING:
        logger.warning("RUNNING IN PAPER-TRADING MODE")
    else:
        logger.warning("RUNNING IN REAL TRADING MODE")

    logger.info(f"Buy type: {config.BUY_ORDER_TYPE}, Sell type: {config.SELL_ORDER_TYPE}")
    logger.info(f"Max price changes for buys: {config.BUY_MAX_PRICE_CHANGE}, Max price changes for sells: {config.SELL_MAX_PRICE_CHANGE}")
    logger.info(f"Using {config.PRICE_TYPE} prices")

    logger.info("Creating database schema if it doesn't already exist")
    db.create_database()

    db.set_coins(config.SUPPORTED_COIN_LIST)
    db.migrate_old_state()

    trader.initialize()

    schedule = SafeScheduler(logger)
    schedule.every(config.SCOUT_SLEEP_TIME).seconds.do(trader.scout).tag("scouting")
    schedule.every(15).seconds.do(trader.track_last_prices).tag("track last prices")
    schedule.every(1).minutes.do(trader.update_values).tag("updating value history")
    schedule.every(1).minutes.do(db.prune_scout_history).tag("pruning scout history")
    schedule.every(1).hours.do(db.prune_value_history).tag("pruning value history")
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    finally:
        manager.stream_manager.close()
