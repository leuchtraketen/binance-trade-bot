from datetime import datetime

from binance_trade_bot.auto_trader import AutoTrader


class Strategy(AutoTrader):
    def scout(self):
        """
        Scout for potential jumps from the current coin to another coin
        """
        active_coins = self.get_active_coins()

        # Display on the console, the current coin+Bridge, so users can see *some* activity and not think the bot
        # has stopped. Not logging though to reduce log size.
        print(
            f"{self.manager.now()} - CONSOLE - INFO - I am scouting the best trades. "
            f"Current coins: {active_coins} ",
            end="\n",
        )

        for coin in active_coins:
            coin_price = self.manager.get_sell_price(coin + self.config.BRIDGE)

            if coin_price is None:
                self.logger.info("Skipping scouting... current coin {} not found".format(coin + self.config.BRIDGE))
                continue
            
            #fetch active coin again to avoid some coins fusioning by jumping to the same coin in the same scout run
            current_active_coins = self.get_active_coins()

            self._jump_to_best_coin(coin, coin_price, current_active_coins)

            # if a jump fails try buying another coin to avoid the next coin takes the bridge value
            if self.failed_buy_order:
                self.bridge_scout()

        # no active coin left. buy one.
        if len(active_coins) == 0:
            self.logger.info("No active coin found. Going to buy one. If you want to have more than one coin you just need to buy coins from your coinlist.")
            self.bridge_scout()

    def get_active_coins(self):
        active_coins = []

        for coin in self.db.get_coins(True):
            current_coin_balance = self.manager.get_currency_balance(coin.symbol)
            coin_price = self.manager.get_sell_price(coin + self.config.BRIDGE)

            if coin_price is None:
                self.logger.info("Skipping scouting... coin {} not found".format(coin + self.config.BRIDGE))
                continue
            
            min_notional = self.manager.get_min_notional(coin.symbol, self.config.BRIDGE.symbol)
            if coin_price * current_coin_balance > min_notional:
                active_coins.append(coin)

        return active_coins

    def bridge_scout(self):
        """
        If we have any bridge coin leftover, buy a coin with it that we won't immediately trade out of
        """
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)

        active_coins = self.get_active_coins()
        active_coin_symbols = [c.symbol for c in active_coins]
        for coin in self.db.get_coins():
            #skip active coins, we dont want coin fusion
            if coin.symbol in active_coin_symbols:
                continue

            current_coin_price = self.manager.get_sell_price(coin + self.config.BRIDGE)

            if current_coin_price is None:
                continue

            ratio_dict, _prices, ratio_debug = self._get_ratios(coin, current_coin_price, active_coins)
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
