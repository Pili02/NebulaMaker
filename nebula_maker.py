import logging
from decimal import Decimal
from typing import Dict, List
import time

from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import OrderFilledEvent
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.data_feed.candles_feed.candles_factory import (
    CandlesFactory,
    CandlesConfig,
)
from hummingbot.connector.connector_base import ConnectorBase
import pandas_ta as ta


class NebulaMaker(ScriptStrategyBase):
    base_spread = Decimal("0.005")
    buy_spread = Decimal("0.005")
    sell_spread = Decimal("0.005")
    order_refresh_time = 60
    order_amount = Decimal("0.6")
    create_timestamp = 0
    trading_pair = "ETH-USDT"
    exchange = "binance_paper_trade"
    price_source = PriceType.MidPrice
    candle_exchange = "binance"
    candles_interval = "1m"
    candles_length = 30
    max_records = 1000
    markets = {exchange: {trading_pair}}
    stop_loss_price = Decimal("0")
    position_size = Decimal("0")
    last_buy_price = Decimal("0")
    last_sell_price = Decimal("0")
    realized_pnl = Decimal("0")
    unrealized_pnl = Decimal("0")
    pnl = Decimal("0")
    # Stop loss cooldown tracking
    stop_loss_triggered = False
    stop_loss_cooldown_time = 0
    stop_loss_cooldown_period = 60  # 60 seconds cooldown
    # Profit/loss thresholds
    profit_threshold = Decimal("0.5")  # USDT
    loss_threshold = Decimal("-0.5")  # USDT
    # Stop loss check interval
    stop_loss_check_interval = 10  # Check stop loss every 10 seconds
    last_stop_loss_check_time = 0  # Track when we last checked stop loss

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)
        self.candles = CandlesFactory.get_candle(
            CandlesConfig(
                connector=self.candle_exchange,
                trading_pair=self.trading_pair,
                interval=self.candles_interval,
                max_records=self.max_records,
            )
        )
        self.candles.start()
        # Reset PnL tracking
        self.pnl = Decimal("0")
        self.realized_pnl = Decimal("0")
        self.unrealized_pnl = Decimal("0")
        self.position_size = Decimal("0")
        self.last_buy_price = Decimal("0")
        self.last_sell_price = Decimal("0")
        # Add stop loss cooldown tracking
        self.stop_loss_triggered = False
        self.stop_loss_cooldown_time = 0
        self.stop_loss_cooldown_period = 60
        # Initialize stop loss check time
        self.last_stop_loss_check_time = 0

    def on_stop(self):
        self.candles.stop()

    def on_tick(self):
        """Called on every tick (1 second interval)."""
        if not self.ready_to_trade:
            return

        # Check if we need to exit due to stop loss - only check at intervals
        current_time = time.time()
        if (
            current_time - self.last_stop_loss_check_time
            >= self.stop_loss_check_interval
        ):
            self.last_stop_loss_check_time = current_time
            if self.check_stop_loss():
                self.logger().info("Stop loss triggered, placing exit order")
                current_price = self.connectors[self.exchange].get_price_by_type(
                    self.trading_pair, self.price_source
                )

                # Cancel all existing orders first
                self.cancel_all_orders()

                # For LONG positions: sell to exit
                if self.position_size > 0:
                    # Use market order for immediate execution
                    sell_order = OrderCandidate(
                        trading_pair=self.trading_pair,
                        is_maker=False,  # Use taker order for immediate execution
                        order_type=OrderType.MARKET,  # Use market order
                        order_side=TradeType.SELL,
                        amount=abs(self.position_size),
                        price=current_price,  # Price will be ignored for market orders
                    )
                    self.place_order(connector_name=self.exchange, order=sell_order)
                    self.logger().info(
                        f"Placed market sell order to exit LONG position"
                    )

                # For SHORT positions: buy to exit
                elif self.position_size < 0:
                    # Use market order for immediate execution
                    buy_order = OrderCandidate(
                        trading_pair=self.trading_pair,
                        is_maker=False,  # Use taker order for immediate execution
                        order_type=OrderType.MARKET,  # Use market order
                        order_side=TradeType.BUY,
                        amount=abs(self.position_size),
                        price=current_price,  # Price will be ignored for market orders
                    )
                    self.place_order(connector_name=self.exchange, order=buy_order)
                    self.logger().info(
                        f"Placed market buy order to exit SHORT position"
                    )

                return

        # Check if we need to refresh orders
        if self.create_timestamp <= self.current_timestamp:
            self.logger().info("=== Starting new tick cycle ===")

            # Check if we're in a position and need to exit based on profit/loss
            if self.position_size != Decimal("0"):
                current_price = self.connectors[self.exchange].get_price_by_type(
                    self.trading_pair, self.price_source
                )

                # Calculate current PnL
                if self.position_size > 0:  # Long position
                    # For long positions: (current_price - entry_price) * position_size
                    self.unrealized_pnl = (
                        current_price - self.last_buy_price
                    ) * self.position_size
                else:  # Short position
                    # For short positions: (entry_price - current_price) * abs(position_size)
                    self.unrealized_pnl = (self.last_sell_price - current_price) * abs(
                        self.position_size
                    )

                self.pnl = self.realized_pnl + self.unrealized_pnl
                self.logger().info(
                    f"Current PnL: {self.pnl} (Realized: {self.realized_pnl}, Unrealized: {self.unrealized_pnl})"
                )

                # If we're making a profit above threshold, exit immediately
                if self.pnl > self.profit_threshold:
                    self.logger().info(
                        f"Profit threshold reached ({self.pnl}), exiting position immediately"
                    )
                    self.cancel_all_orders()

                    if self.position_size > 0:  # Long position
                        sell_order = OrderCandidate(
                            trading_pair=self.trading_pair,
                            is_maker=True,
                            order_type=OrderType.LIMIT,
                            order_side=TradeType.SELL,
                            amount=abs(self.position_size),
                            price=current_price
                            * (
                                Decimal("1") - Decimal("0.001")
                            ),  # Slightly below market to ensure fill
                        )
                        self.place_order(connector_name=self.exchange, order=sell_order)
                        self.logger().info(
                            f"Placing limit sell order to take profit at {current_price}"
                        )
                    else:  # Short position
                        buy_order = OrderCandidate(
                            trading_pair=self.trading_pair,
                            is_maker=True,
                            order_type=OrderType.LIMIT,
                            order_side=TradeType.BUY,
                            amount=abs(self.position_size),
                            price=current_price
                            * (
                                Decimal("1") + Decimal("0.001")
                            ),  # Slightly above market to ensure fill
                        )
                        self.place_order(connector_name=self.exchange, order=buy_order)
                        self.logger().info(
                            f"Placing limit buy order to take profit at {current_price}"
                        )

                    self.create_timestamp = (
                        self.order_refresh_time + self.current_timestamp
                    )
                    self.logger().info("=== Tick cycle completed ===")
                    return

                # If we're making a loss but below threshold, maintain position
                elif self.pnl < self.loss_threshold:
                    self.logger().info(
                        f"Loss below threshold ({self.pnl}), maintaining position"
                    )
                    # Continue with normal order refresh
                else:
                    self.logger().info(
                        f"PnL within thresholds ({self.pnl}), maintaining position"
                    )
                    # Continue with normal order refresh

            # Normal order refresh
            self.cancel_all_orders()
            proposals = self.create_proposal()
            self.place_orders(proposals)
            self.create_timestamp = self.order_refresh_time + self.current_timestamp
            self.logger().info("=== Tick cycle completed ===")

    def place_orders(self, proposal: List[OrderCandidate]) -> None:
        for order in proposal:
            self.place_order(connector_name=self.exchange, order=order)

    def place_order(self, connector_name: str, order: OrderCandidate):
        if order.order_side == TradeType.SELL:
            self.sell(
                connector_name=connector_name,
                trading_pair=order.trading_pair,
                amount=order.amount,
                order_type=order.order_type,
                price=order.price,
            )
        elif order.order_side == TradeType.BUY:
            self.buy(
                connector_name=connector_name,
                trading_pair=order.trading_pair,
                amount=order.amount,
                order_type=order.order_type,
                price=order.price,
            )

    def cancel_all_orders(self):
        for order in self.get_active_orders(connector_name=self.exchange):
            self.cancel(self.exchange, order.trading_pair, order.client_order_id)

    def get_candles_with_features(self):
        if self.candles.candles_df is None or self.candles.candles_df.empty:
            self.logger().warning("No candle data available yet.")
            return None

        candles_df = self.candles.candles_df.copy()
        candles_df["RSI"] = ta.rsi(candles_df["close"], length=self.candles_length)
        candles_df["EMA_30"] = ta.ema(candles_df["close"], length=30)
        candles_df["Volatility"] = (
            ta.natr(
                high=candles_df["high"],
                low=candles_df["low"],
                close=candles_df["close"],
                length=self.candles_length,
            )
            / 100
        )
        buyers = candles_df["volume"][candles_df["close"] > candles_df["open"]].sum()
        sellers = candles_df["volume"][candles_df["close"] <= candles_df["open"]].sum()
        max_vol = max(buyers, sellers)
        if max_vol > 0:
            candles_df["Depth"] = (buyers - sellers) / max_vol
        else:
            candles_df["Depth"] = 0
        return candles_df

    def did_fill_order(self, event: OrderFilledEvent):
        """
        This method is triggered whenever an order is filled.

        :param event: OrderFilledEvent containing details about the filled order.
        """
        self.logger().info(f"Order Filled: {event}")

        if event.trade_type == TradeType.BUY:
            # For buy orders, track the buy price and update position
            self.last_buy_price = event.price
            self.position_size += event.amount
            self.logger().info(
                f"Buy order filled. Position size: {self.position_size}, Last buy price: {self.last_buy_price}"
            )

            # Calculate realized PnL if we're closing a short position
            if self.last_sell_price > Decimal("0") and self.position_size >= Decimal(
                "0"
            ):
                # We're closing a short position (position_size was negative, now it's positive or zero)
                realized_pnl = (self.last_sell_price - event.price) * event.amount
                self.realized_pnl += realized_pnl
                self.logger().info(
                    f"Buy order filled. Realized PnL from previous sell: {realized_pnl}"
                )
                # Reset last_sell_price since we've closed the short position
                self.last_sell_price = Decimal("0")

            # Set stop loss for long position
            self.set_stop_loss_for_long(event.price)

        elif event.trade_type == TradeType.SELL:
            # For sell orders, track the sell price and update position
            self.last_sell_price = event.price
            self.position_size -= event.amount
            self.logger().info(
                f"Sell order filled. Position size: {self.position_size}, Last sell price: {self.last_sell_price}"
            )

            # Calculate realized PnL if we're closing a long position
            if self.last_buy_price > Decimal("0") and self.position_size <= Decimal(
                "0"
            ):
                # We're closing a long position (position_size was positive, now it's negative or zero)
                realized_pnl = (event.price - self.last_buy_price) * event.amount
                self.realized_pnl += realized_pnl
                self.logger().info(
                    f"Sell order filled. Realized PnL from previous buy: {realized_pnl}"
                )
                # Reset last_buy_price since we've closed the long position
                self.last_buy_price = Decimal("0")

            # Set stop loss for short position
            self.set_stop_loss_for_short(event.price)

        # Update total PnL
        self.pnl = self.realized_pnl + self.unrealized_pnl
        self.logger().info(
            f"Total PnL updated: {self.pnl} (Realized: {self.realized_pnl}, Unrealized: {self.unrealized_pnl})"
        )

    def set_stop_loss_for_long(self, entry_price: Decimal):
        """Set stop loss for a long position."""
        # Set stop loss at 1% below entry price
        stop_loss_percentage = Decimal("0.01")
        self.stop_loss_price = entry_price * (
            Decimal("1") - stop_loss_percentage - self.buy_spread
        )
        self.logger().info(
            f"Setting stop loss for long position at {self.stop_loss_price} ({(stop_loss_percentage * 100):.2f}% below entry price)"
        )

    def set_stop_loss_for_short(self, entry_price: Decimal):
        """Set stop loss for a short position."""
        # Set stop loss at 1% above entry price
        stop_loss_percentage = Decimal("0.01")
        self.stop_loss_price = entry_price * (
            Decimal("1") + stop_loss_percentage + self.sell_spread
        )
        self.logger().info(
            f"Setting stop loss for short position at {self.stop_loss_price} ({(stop_loss_percentage * 100):.2f}% above entry price)"
        )

    def check_stop_loss(self):
        """Check if stop loss conditions are met and log them."""
        if self.stop_loss_price == 0:
            return False

        # Check if we're in cooldown period
        current_time = time.time()
        if (
            self.stop_loss_triggered
            and current_time - self.stop_loss_cooldown_time
            < self.stop_loss_cooldown_period
        ):
            self.logger().info(
                f"Stop loss cooldown period active. {int(self.stop_loss_cooldown_period - (current_time - self.stop_loss_cooldown_time))} seconds remaining."
            )
            return False

        current_price = self.connectors[self.exchange].get_price_by_type(
            self.trading_pair, self.price_source
        )

        # For LONG positions: exit if price falls below stop loss
        if self.position_size > 0:
            # Only log stop loss checks at intervals
            if (
                time.time() - self.last_stop_loss_check_time
                >= self.stop_loss_check_interval
            ):
                self.logger().info(f"STOP LOSS CHECK - Position: LONG")
                self.logger().info(
                    f"STOP LOSS CHECK - Current price: {current_price}, Stop loss price: {self.stop_loss_price}"
                )

            if current_price < self.stop_loss_price:
                self.logger().info(
                    f"STOP LOSS TRIGGERED - LONG position - Current price {current_price} below stop loss {self.stop_loss_price}"
                )
                self.stop_loss_triggered = True
                self.stop_loss_cooldown_time = current_time
                return True
            return False

        # For SHORT positions: exit if price rises above stop loss
        elif self.position_size < 0:
            # Only log stop loss checks at intervals
            if (
                time.time() - self.last_stop_loss_check_time
                >= self.stop_loss_check_interval
            ):
                self.logger().info(f"STOP LOSS CHECK - Position: SHORT")
                self.logger().info(
                    f"STOP LOSS CHECK - Current price: {current_price}, Stop loss price: {self.stop_loss_price}"
                )

            if current_price > self.stop_loss_price:
                self.logger().info(
                    f"STOP LOSS TRIGGERED - SHORT position - Current price {current_price} above stop loss {self.stop_loss_price}"
                )
                self.stop_loss_triggered = True
                self.stop_loss_cooldown_time = current_time
                return True
            return False

        return False

    def create_proposal(self) -> List[OrderCandidate]:
        ref_price = self.connectors[self.exchange].get_price_by_type(
            self.trading_pair, self.price_source
        )

        candles_df = self.get_candles_with_features()
        if candles_df is None:
            self.logger().warning(
                "Skipping proposal creation due to missing candle data."
            )
            return []

        # Calculate the spread based on the depth ratio
        depth_ratio = candles_df["Depth"].iloc[-1]
        ema_30 = candles_df["EMA_30"].iloc[-1]
        rsi = candles_df["RSI"].iloc[-1]
        rsi_factor = (rsi - 50) / 50
        ema_30_factor = 0
        if ema_30 > ref_price:
            ema_30_factor = -1
        else:
            ema_30_factor = 1

        # Calculate alpha using all factors
        alpha = 5 * depth_ratio + 3 * ema_30_factor + 2 * rsi_factor
        self.logger().info(f"alpha: {alpha}")

        # If we're in a position, maintain it with limit orders
        if self.position_size != Decimal("0"):
            if self.position_size > 0:  # Long position
                # Place limit sell order to take profit
                sell_price = ref_price * (Decimal("1") + self.sell_spread)
                sell_order = OrderCandidate(
                    trading_pair=self.trading_pair,
                    is_maker=True,
                    order_type=OrderType.LIMIT,
                    order_side=TradeType.SELL,
                    amount=abs(self.position_size),
                    price=sell_price,
                )
                self.logger().info(
                    f"Maintaining long position with limit sell order at {sell_price}"
                )
                return [sell_order]
            else:  # Short position
                # Place limit buy order to take profit
                buy_price = ref_price * (Decimal("1") - self.buy_spread)
                buy_order = OrderCandidate(
                    trading_pair=self.trading_pair,
                    is_maker=True,
                    order_type=OrderType.LIMIT,
                    order_side=TradeType.BUY,
                    amount=abs(self.position_size),
                    price=buy_price,
                )
                self.logger().info(
                    f"Maintaining short position with limit buy order at {buy_price}"
                )
                return [buy_order]

        # If we're not in a position, create new orders based on alpha
        depth_factor = abs(alpha)

        # Calculate spreads based on alpha
        if alpha > 0:  # Bullish signal - go long
            # Asymmetric spreads: lower buy spread, higher sell spread when alpha > 0
            self.buy_spread = self.base_spread * (
                Decimal("1") + Decimal("0.5") * Decimal(str(depth_factor))
            )  # Reduced multiplier for buy spread
            self.sell_spread = self.base_spread * (
                Decimal("1") + Decimal("1.5") * Decimal(str(depth_factor))
            )  # Increased multiplier for sell spread
        else:  # Bearish signal - go short
            # Use the same asymmetric spread logic for bearish signals
            # Higher buy spread, lower sell spread when alpha < 0
            self.buy_spread = self.base_spread * (
                Decimal("1") + Decimal("1.5") * Decimal(str(depth_factor))
            )  # Increased multiplier for buy spread
            self.sell_spread = self.base_spread * (
                Decimal("1") + Decimal("0.5") * Decimal(str(depth_factor))
            )  # Reduced multiplier for sell spread

        # Calculate prices with spreads
        buy_price = ref_price * (Decimal("1") - self.buy_spread)  # Buy below market
        sell_price = ref_price * (Decimal("1") + self.sell_spread)  # Sell above market

        # Place limit orders for both buy and sell to ensure equal number of orders
        buy_order = OrderCandidate(
            trading_pair=self.trading_pair,
            is_maker=True,
            order_type=OrderType.LIMIT,
            order_side=TradeType.BUY,
            amount=Decimal(self.order_amount),
            price=buy_price,
        )

        sell_order = OrderCandidate(
            trading_pair=self.trading_pair,
            is_maker=True,
            order_type=OrderType.LIMIT,
            order_side=TradeType.SELL,
            amount=Decimal(self.order_amount),
            price=sell_price,
        )

        self.logger().info(
            f"Placing limit buy order at {buy_price} and limit sell order at {sell_price}"
        )
        return [buy_order, sell_order]

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Market connectors are not ready."

        lines = []

        # Show balances
        balance_df = self.get_balance_df()
        lines.extend(
            ["", "  Balances:"]
            + ["    " + line for line in balance_df.to_string(index=False).split("\n")]
        )

        # Show PnL with more details
        lines.append("")
        lines.append("  PnL Information:")
        lines.append(f"    Current PnL: {self.pnl:.8f} USDT")
        lines.append(f"    Realized PnL: {self.realized_pnl:.8f} USDT")
        lines.append(f"    Unrealized PnL: {self.unrealized_pnl:.8f} USDT")
        lines.append(f"    Position Size: {self.position_size:.8f} ETH")
        lines.append(f"    Last Buy Price: {self.last_buy_price:.8f} USDT")
        lines.append(f"    Last Sell Price: {self.last_sell_price:.8f} USDT")
        lines.append(f"    Profit Threshold: {self.profit_threshold:.8f} USDT")
        lines.append(f"    Loss Threshold: {self.loss_threshold:.8f} USDT")

        # Show active orders
        try:
            orders_df = self.active_orders_df()
            lines.extend(
                ["", "  Active Orders:"]
                + [
                    "    " + line
                    for line in orders_df.to_string(index=False).split("\n")
                ]
            )
        except ValueError:
            lines.append("")
            lines.append("  No active maker orders.")

        # Market metrics
        candles_df = self.get_candles_with_features()
        ref_price = self.connectors[self.exchange].get_price_by_type(
            self.trading_pair, self.price_source
        )
        if candles_df is not None and not candles_df.empty:
            latest_rsi = candles_df["RSI"].iloc[-1]
            latest_ema = candles_df["EMA_30"].iloc[-1]
            latest_volatility = candles_df["Volatility"].iloc[-1]
            latest_depth = candles_df["Depth"].iloc[-1]

            # Calculate current alpha
            rsi_factor = (latest_rsi - 50) / 50
            ema_30_factor = 1 if latest_ema > ref_price else -1
            current_alpha = 5 * latest_depth + 3 * ema_30_factor + 2 * rsi_factor

            lines.extend(
                [
                    "\n----------------------------------------------------------------------\n",
                    "  Market Metrics:",
                    f"    Current Price   : {ref_price:.4f}",
                    f"    RSI             : {latest_rsi:.2f}",
                    f"    EMA-30          : {latest_ema:.4f}",
                    f"    Volatility (NATR): {latest_volatility:.4f}",
                    f"    Depth            : {latest_depth:.4f}",
                    f"    Alpha            : {current_alpha:.4f}",
                    f"    Buy Spread      : {self.buy_spread * 100:.2f}%",
                    f"    Sell Spread     : {self.sell_spread * 100:.2f}%",
                ]
            )

            # Recent candle data
            lines.extend(
                [
                    "\n----------------------------------------------------------------------\n",
                    "  Recent Candles:",
                    "",
                ]
            )
            lines.extend(
                [
                    "    " + line
                    for line in candles_df.tail()
                    .iloc[::-1]
                    .to_string(index=False)
                    .split("\n")
                ]
            )
        else:
            lines.append("")
            lines.append("  Candle data is not available yet.")

        return "\n".join(lines)
