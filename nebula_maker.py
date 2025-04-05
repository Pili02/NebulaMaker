import logging
from decimal import Decimal
from typing import Dict, List

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


class Position:
    """
    Simple class to track position information
    """

    def __init__(self, amount: Decimal, entry_price: Decimal):
        self.amount = amount
        self.entry_price = entry_price


class NebulaMaker(ScriptStrategyBase):
    base_spread = 0.0005
    spread = 0.0005
    order_refresh_time = 120
    order_refresh_time_base = 120
    order_amount = 0.04
    create_timestamp = 0
    trading_pair = "ETH-USDT"
    exchange = "binance_paper_trade"
    price_source = PriceType.MidPrice
    candle_exchange = "binance"
    candles_interval = "1m"
    candles_length = 30
    max_records = 1000
    markets = {exchange: {trading_pair}}

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

    def on_stop(self):
        self.candles.stop()

    def on_tick(self):
        if self.create_timestamp <= self.current_timestamp:
            self.cancel_all_orders()
            proposal = self.create_proposal()
            self.place_orders(proposal)
            self.create_timestamp = self.order_refresh_time + self.current_timestamp

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
        candles_df["EMA_50"] = ta.ema(candles_df["close"], length=50)
        candles_df["Volatility"] = ta.stdev(
            candles_df["close"], length=self.candles_length
        )
        candles_df["Volume_MA"] = ta.sma(
            candles_df["volume"], length=self.candles_length
        )
        return candles_df

    def adjust_order_refresh_time(self, base_rate, volatility, k=Decimal("0.1")):
        """
        Adjusts order refresh time based on volatility with a more conservative k-factor
        """
        volatility_factor = Decimal("1") + k * Decimal(str(volatility))
        return base_rate * volatility_factor

    def did_fill_order(self, event: OrderFilledEvent):
        """
        This method is triggered whenever an order is filled.

        :param event: OrderFilledEvent containing details about the filled order.
        """
        self.logger().info(f"Order Filled: {event}")

        # Add the trade details to trade history
        trade_record = {
            "timestamp": event.timestamp,
            "order_id": event.order_id,
            "trading_pair": event.trading_pair,
            "trade_type": event.trade_type,
            "amount": event.amount,
            "price": event.price,
        }

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

        latest_rsi = candles_df["RSI"].iloc[-1]
        latest_ema = candles_df["EMA_50"].iloc[-1]
        latest_volatility = candles_df["Volatility"].iloc[-1]

        self.order_refresh_time = int(
            self.adjust_order_refresh_time(
                Decimal(self.order_refresh_time_base), Decimal(latest_volatility)
            )
        )

        # Dynamic spread adjustment based on volatility
        volatility_factor = Decimal("1") + Decimal("0.01") * Decimal(
            str(latest_volatility)
        )
        self.spread = float(Decimal(str(self.base_spread)) * volatility_factor)

        # More conservative price adjustments based on RSI
        if latest_rsi > 70 and ref_price > latest_ema:
            ref_price = ref_price * Decimal("1.02")
        elif latest_rsi < 30 and ref_price < latest_ema:
            ref_price = ref_price * Decimal("0.98")
        self.logger().info(f"Ref Price: {ref_price}")
        buy_price = ref_price * Decimal(1 - Decimal(self.spread))
        sell_price = ref_price * Decimal(1 + Decimal(self.spread))
        self.logger().info(f"Buy Price: {buy_price}, Sell Price: {sell_price}")

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

        # Show active orders
        try:
            df = self.active_orders_df()
            lines.extend(
                ["", "  Orders:"]
                + ["    " + line for line in df.to_string(index=False).split("\n")]
            )
        except ValueError:
            lines.extend(["", "  No active maker orders."])

        # Market metrics
        ref_price = self.connectors[self.exchange].get_price_by_type(
            self.trading_pair, self.price_source
        )
        candles_df = self.get_candles_with_features()

        if candles_df is not None:
            latest_rsi = candles_df["RSI"].iloc[-1]
            latest_ema = candles_df["EMA_50"].iloc[-1]
            latest_volatility = candles_df["Volatility"].iloc[-1]

            lines.extend(
                [
                    "\n----------------------------------------------------------------------\n",
                    "  Market Metrics:",
                    f"  Current Price: {ref_price:.4f}",
                    f"  RSI: {latest_rsi:.2f}",
                    f"  EMA-50: {latest_ema:.4f}",
                    f"  Volatility: {latest_volatility:.4f}",
                    f"  Spread: {self.spread * 100:.2f}%",
                ]
            )

        # Show recent candles
        if candles_df is not None:
            lines.extend(
                [
                    "\n----------------------------------------------------------------------\n"
                ]
            )
            lines.extend(["  Recent Candles:", ""])
            lines.extend(
                [
                    "    " + line
                    for line in candles_df.tail()
                    .iloc[::-1]
                    .to_string(index=False)
                    .split("\n")
                ]
            )

        return "\n".join(lines)
