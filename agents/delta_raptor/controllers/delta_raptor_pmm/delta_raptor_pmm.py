"""Delta Raptor PMM — pmm_simple plus a coded price-drift requote.

Two refresh paths, both in code (no LLM involved):

1. Timed refresh: every ``executor_refresh_time`` seconds (default 300 = 5 min)
   a resting order is cancelled and requoted — inherited from pmm_simple.
2. Drift refresh: every ``drift_check_interval`` seconds (default 60) each
   resting order's price is compared with the price the controller would quote
   right now. If they differ by more than ``drift_threshold_pct`` (default
   0.005 = 0.5%), that order is cancelled early and requoted on the next pass,
   even if the 5-minute timer has not expired.

Filled / trading executors are never touched by either path.
"""

from decimal import Decimal
from typing import List

from pydantic import Field

from hummingbot.core.data_type.common import TradeType
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import ExecutorAction, StopExecutorAction


class DeltaRaptorPMMConfig(MarketMakingControllerConfigBase):
    controller_name: str = "delta_raptor_pmm"
    drift_check_interval: int = Field(
        default=60,
        json_schema_extra={"prompt": "Seconds between price-drift checks (e.g. 60): ",
                           "prompt_on_new": True},
    )
    drift_threshold_pct: Decimal = Field(
        default=Decimal("0.005"),
        json_schema_extra={"prompt": "Requote early when an order is this far from the "
                                     "current quote price, as a fraction (0.005 = 0.5%): ",
                           "prompt_on_new": True},
    )


class DeltaRaptorPMMController(MarketMakingControllerBase):
    def __init__(self, config: DeltaRaptorPMMConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._last_drift_check = 0.0

    def get_executor_config(self, level_id: str, price: Decimal, amount: Decimal):
        trade_type = self.get_trade_type_from_level_id(level_id)
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            level_id=level_id,
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            entry_price=price,
            amount=amount,
            triple_barrier_config=self.config.triple_barrier_config,
            leverage=self.config.leverage,
            side=trade_type,
        )

    def _target_price(self, level_id: str) -> Decimal:
        """Price this level would be quoted at right now."""
        trade_type = self.get_trade_type_from_level_id(level_id)
        level = self.get_level_from_level_id(level_id)
        spreads = getattr(self.config, f"{trade_type.name.lower()}_spreads")
        reference_price = Decimal(self.processed_data["reference_price"])
        spread = Decimal(str(spreads[level])) * Decimal(self.processed_data["spread_multiplier"])
        side = Decimal("-1") if trade_type == TradeType.BUY else Decimal("1")
        return reference_price * (Decimal("1") + side * spread)

    def executors_to_early_stop(self) -> List[ExecutorAction]:
        now = self.market_data_provider.time()
        if now - self._last_drift_check < self.config.drift_check_interval:
            return []
        self._last_drift_check = now
        if not self.processed_data or "reference_price" not in self.processed_data:
            return []

        resting = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda x: x.is_active and not x.is_trading,
        )
        actions: List[ExecutorAction] = []
        for ex in resting:
            level_id = ex.custom_info.get("level_id") or getattr(ex.config, "level_id", None)
            placed = getattr(ex.config, "entry_price", None)
            if not level_id or not placed:
                continue
            target = self._target_price(level_id)
            if target <= 0:
                continue
            drift = abs(Decimal(placed) - target) / target
            if drift > self.config.drift_threshold_pct:
                self.logger().info(
                    f"DRIFT REQUOTE {level_id}: placed {placed:.6f} vs target {target:.6f} "
                    f"({drift:.4%} > {self.config.drift_threshold_pct:.2%})"
                )
                actions.append(StopExecutorAction(controller_id=self.config.id, executor_id=ex.id))
        return actions
