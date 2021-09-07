from binance_trade_bot.models import trade
import random
import sys
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.expression import and_


from binance_trade_bot.auto_trader import AutoTrader
from binance_trade_bot.database import Pair, Trade, Coin


class Strategy(AutoTrader):
    def initialize(self):
        self.logger.info(f"CAUTION: The db_reset strategy can lead to losses! A lower idle timeout increases the risk! Use this strategy only if you know what you are doing, did alot of backtests and can live with possible losses.")

        if self.config.ACCEPT_LOSSES != True:
            self.logger.error("You need accept losses by setting accept_losses=true in the user.cfg or setting the environment variable ACCEPT_LOSSES to true in order to use this strategy!")
            raise Exception()

        super().initialize()
        self.initialize_current_coin()
        self.reinit_threshold = datetime(1970, 1, 1, tzinfo=timezone.utc)

        self.logger.info(f"Using {self.config.MAX_IDLE_HOURS} hours as maximum idle timeout after not trading.")

    def scout(self):
        #check if previous buy order failed. If so, bridge scout for a new coin.
        if self.failed_buy_order:
            self.bridge_scout()

        session: Session
        with self.db.db_session() as session:
            last_trade = session.query(Trade).order_by(Trade.datetime.desc()).first()
            if last_trade != None:
                last_trade_time = last_trade.datetime.replace(tzinfo=timezone.utc)
                max_idle_timeout = float(self.config.MAX_IDLE_HOURS)
                allowed_idle_time = last_trade_time + timedelta(hours=max_idle_timeout)
                base_time: datetime = self.manager.now()
                if base_time >= allowed_idle_time and base_time >= self.reinit_threshold:
                    self.logger.info(f"Last trade was before {max_idle_timeout} hours! Going to reinit ratios.")
                    self.re_initialize_trade_thresholds()
                    self.logger.info("Finished reiniting the ratios.")
                    self.reinit_threshold = base_time + timedelta(hours=1)
                

        """
        Scout for potential jumps from the current coin to another coin
        """
        current_coin = self.db.get_current_coin()
        current_coin_price = self.manager.get_sell_price(current_coin + self.config.BRIDGE)

        if current_coin_price is None:
            self.logger.info("Skipping scouting... current coin {} not found".format(current_coin + self.config.BRIDGE))
            return

        # Display on the console, the current coin+Bridge, so users can see *some* activity and not think the bot has
        # stopped. Not logging though to reduce log size.
        print(
            f"{datetime.now()} - CONSOLE - INFO - I am scouting the best trades. "
            f"{current_coin}: current price: {current_coin_price} {self.config.BRIDGE}",
            end="\n",
        )

        self._jump_to_best_coin(current_coin, current_coin_price)

    def bridge_scout(self):
        current_coin = self.db.get_current_coin()
        if self.manager.get_currency_balance(current_coin.symbol) > self.manager.get_min_notional(
            current_coin.symbol, self.config.BRIDGE.symbol
        ):
            # Only scout if we don't have enough of the current coin
            return
        new_coin = super().bridge_scout()
        if new_coin is not None:
            self.db.set_current_coin(new_coin)

    def initialize_current_coin(self):
        """
        Decide what is the current coin, and set it up in the DB.
        """
        if self.db.get_current_coin() is None:
            current_coin_symbol = self.config.CURRENT_COIN_SYMBOL
            if not current_coin_symbol:
                current_coin_symbol = random.choice(self.config.SUPPORTED_COIN_LIST)

            self.logger.info(f"Setting initial coin to {current_coin_symbol}")

            if current_coin_symbol not in self.config.SUPPORTED_COIN_LIST:
                sys.exit("***\nERROR!\nSince there is no backup file, a proper coin name must be provided at init\n***")
            self.db.set_current_coin(current_coin_symbol)

            # if we don't have a configuration, we selected a coin at random... Buy it so we can start trading.
            if self.config.CURRENT_COIN_SYMBOL == "":
                current_coin = self.db.get_current_coin()
                self.logger.info(f"Purchasing {current_coin} to begin trading")
                self.manager.buy_alt(
                    current_coin, self.config.BRIDGE, self.manager.get_buy_price(current_coin + self.config.BRIDGE)
                )
                self.logger.info("Ready to start trading")
            else:
                current_balance = self.manager.get_currency_balance(current_coin_symbol)
                sell_price = self.manager.get_sell_price(current_coin_symbol + self.config.BRIDGE.symbol)
                if current_balance is not None and current_balance * sell_price < self.manager.get_min_notional(current_coin_symbol, self.config.BRIDGE.symbol):
                    self.logger.info(f"Purchasing {current_coin_symbol} to begin trading")
                    current_coin = self.db.get_current_coin()
                    self.manager.buy_alt(
                        current_coin, self.config.BRIDGE, self.manager.get_buy_price(current_coin + self.config.BRIDGE)
                    )
                    self.logger.info("Ready to start trading")

    def re_initialize_trade_thresholds(self):
        """
        Re-initialize all the thresholds ( hard reset - as deleting db )
        """
        #updates all ratios
        print('************INITIALIZING RATIOS**********')
        session: Session
        with self.db.db_session() as session:
            c1 = aliased(Coin)
            c2 = aliased(Coin)
            for pair in session.query(Pair).\
                join(c1, and_(Pair.from_coin_id == c1.symbol, c1.enabled == True)).\
                join(c2, and_(Pair.to_coin_id == c2.symbol, c2.enabled == True)).\
                all():
                if not pair.from_coin.enabled or not pair.to_coin.enabled:
                    continue
                self.logger.info(f"Initializing {pair.from_coin} vs {pair.to_coin}", False)

                from_coin_price = self.manager.get_sell_price(pair.from_coin + self.config.BRIDGE)
                if from_coin_price is None:
                    self.logger.info(
                        "Skipping initializing {}, symbol not found".format(pair.from_coin + self.config.BRIDGE),
                        False
                    )
                    continue

                to_coin_price = self.manager.get_buy_price(pair.to_coin + self.config.BRIDGE)
                if to_coin_price is None:
                    self.logger.info(
                        "Skipping initializing {}, symbol not found".format(pair.to_coin + self.config.BRIDGE),
                        False
                    )
                    continue

                pair.ratio = from_coin_price / to_coin_price
                pair.from_coin_price = from_coin_price
                pair.to_coin_price = to_coin_price
