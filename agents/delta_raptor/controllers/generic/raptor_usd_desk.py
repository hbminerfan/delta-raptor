"""raptor_usd_desk — functional-style two-sided quoting for a zero-fee stablecoin
pair, recomputed fresh from live executor state every tick rather than tracked
in a running internal ledger.

There is very little persistent state on purpose: the only things kept across
ticks are high-water marks for filled notional / fees per executor id (needed
because an executor can be pruned after it closes) and the pace anchor used
to draw the straight-line volume line. Everything else — which side is
over-weight, whether the book is thin, whether a catch-up order is due — is
derived from `executors_info` and the live order book on each call.

A catch-up order is a plain limit priced past the touch by `reach_ticks`
ticks; it is never a market order, and its size is the smaller of a fixed
ladder step and a fraction of the opposing side's resting depth.

Single-lane (1-sided): unlike the other churn desks, only one side is ever
quoted — whichever direction (BUY/SELL) would correct the account's current
skew back toward even, re-picked every tick. The idle side is never quoted
at all, and any maker left on it from a just-flipped skew is torn down
immediately. The active side's resting maker is a fixed `ladder_steps_usd[0]`
clip; if it sits unfilled past `maker_timeout_s` it is cancelled and replaced
one tick later with a crossing limit of the same size, so this desk keeps
generating turnover continuously in whichever direction it is currently
leaning, rather than waiting for the book to come to it.

Capital note: dual-arm **70% / ~$560** of the entry's $800 competition
capital (volume arm), leaving **~$240 (30%)** for the native XRPL P&L harvest loop.
Entry stop-loss is **$80 (10% of $800 total)**, shared ceiling — not 10% of this arm only.
"""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Tuple

from pydantic import Field

from hummingbot.core.data_type.common import MarketDict, PriceType, TradeType
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction

DUST_USD = Decimal("6")


class RaptorUsdDeskConfig(ControllerConfigBase):
    controller_type: str = "generic"
    controller_name: str = "raptor_usd_desk"

    connector_name: str = Field("binance")
    trading_pair: str = Field("USD1-USDC")

    volume_goal_usd: Decimal = Field(Decimal("1200000"), json_schema_extra={"is_updatable": True})
    goal_window_s: int = Field(172800)
    goal_deadline_ts: int = Field(0, json_schema_extra={"is_updatable": True})

    ladder_steps_usd: Tuple[Decimal, Decimal] = Field(
        (Decimal("150"), Decimal("280")),
        description="Two catch-up sizes: the smaller one used while only modestly behind pace, the larger once "
                    "behind by more than catchup_escalate_usd.")
    catchup_escalate_usd: Decimal = Field(Decimal("5000"), json_schema_extra={"is_updatable": True})
    catchup_trigger_usd: Decimal = Field(Decimal("1800"), json_schema_extra={"is_updatable": True})
    opposing_depth_cap: Decimal = Field(Decimal("0.28"), json_schema_extra={"is_updatable": True})
    reach_ticks: int = Field(2, json_schema_extra={"is_updatable": True})

    widen_step_in: bool = Field(True, json_schema_extra={"is_updatable": True})
    skew_ceiling_usd: Decimal = Field(Decimal("280"), json_schema_extra={"is_updatable": True})

    maker_timeout_s: float = Field(
        7.0, json_schema_extra={"is_updatable": True},
        description="The active side's resting maker is cancelled and replaced with a crossing limit of the same "
                    "clip once it has sat unfilled this long.")

    fee_ceiling_bp: Decimal = Field(Decimal("0.5"), json_schema_extra={"is_updatable": True})
    drawdown_ceiling_usd: Decimal = Field(
        Decimal("80"),
        json_schema_extra={"is_updatable": True},
        description="Entry stop: 10% of full $800 race envelope ($80), NOT 10% of volume sleeve. "
                    "Trip when desk equity falls $80 from genesis value.",
    )
    peg_floor: Decimal = Field(Decimal("0.998"), json_schema_extra={"is_updatable": True})
    peg_ceiling: Decimal = Field(Decimal("1.002"), json_schema_extra={"is_updatable": True})

    hold: bool = Field(False, json_schema_extra={"is_updatable": True})
    retire: bool = Field(False, json_schema_extra={"is_updatable": True})
    retire_base_share: Decimal = Field(Decimal("0.5"), json_schema_extra={"is_updatable": True})

    tick_interval_s: float = Field(1.0)

    def update_markets(self, markets: MarketDict) -> MarketDict:
        markets[self.connector_name] = markets.get(self.connector_name, set()) | {self.trading_pair}
        return markets


class RaptorUsdDeskController(ControllerBase):

    def __init__(self, config: RaptorUsdDeskConfig, *args, **kwargs):
        self.config = config
        kwargs.setdefault("update_interval", float(config.tick_interval_s))
        super().__init__(config, *args, **kwargs)
        self._genesis_ts: Optional[float] = None
        self._pace_anchor = None
        self._fill_hwm: Dict[str, Decimal] = {}
        self._fee_hwm: Dict[str, Decimal] = {}
        self._genesis_value: Optional[Decimal] = None
        self._tripped: Optional[str] = None
        self._off_peg_logged = False
        self._cross_due: bool = False

    def update_config(self, new_config):
        keys = [k for k, f in type(self.config).model_fields.items() if (f.json_schema_extra or {}).get("is_updatable")]
        before = {k: getattr(self.config, k) for k in keys}
        super().update_config(new_config)
        moved = [f"{k}:{before[k]}->{getattr(self.config, k)}" for k in keys if before[k] != getattr(self.config, k)]
        if moved:
            self.logger().info(f"[{self.config.id}] " + " ".join(moved))

    def _coins(self) -> Tuple[str, str]:
        return tuple(self.config.trading_pair.split("-"))

    def _total(self) -> Tuple[Decimal, Decimal]:
        base, quote = self._coins()
        conn = self.market_data_provider.get_connector(self.config.connector_name)
        return Decimal(str(conn.get_balance(base))), Decimal(str(conn.get_balance(quote)))

    def _free(self) -> Tuple[Decimal, Decimal]:
        base, quote = self._coins()
        conn = self.market_data_provider.get_connector(self.config.connector_name)
        reader = getattr(conn, "get_available_balance", None) or conn.get_balance
        return Decimal(str(reader(base))), Decimal(str(reader(quote)))

    def _tick_size(self) -> Decimal:
        try:
            r = self.market_data_provider.get_trading_rules(self.config.connector_name, self.config.trading_pair)
            return Decimal(str(r.min_price_increment))
        except Exception:
            return Decimal("0")

    def _px(self, kind) -> Decimal:
        return Decimal(str(self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, kind)))

    def _book_sides(self):
        try:
            ob = self.market_data_provider.get_order_book(self.config.connector_name, self.config.trading_pair)
            bid, ask = next(ob.bid_entries(), None), next(ob.ask_entries(), None)
            return None if bid is None or ask is None else (Decimal(str(bid.amount)), Decimal(str(ask.amount)))
        except Exception:
            return None

    def _filled_and_fees(self) -> Tuple[Decimal, Decimal]:
        for ex in self.executors_info:
            v = Decimal(str(ex.filled_amount_quote or 0))
            if v > self._fill_hwm.get(ex.id, Decimal("0")):
                self._fill_hwm[ex.id] = v
            f = Decimal(str(getattr(ex, "cum_fees_quote", 0) or 0))
            if f > self._fee_hwm.get(ex.id, Decimal("0")):
                self._fee_hwm[ex.id] = f
        return sum(self._fill_hwm.values(), Decimal("0")), sum(self._fee_hwm.values(), Decimal("0"))

    def _pace_line(self, now, deadline, filled, idle) -> Decimal:
        goal = self.config.volume_goal_usd

        def at(anchor, t):
            t0, v0, g, dl = anchor
            f = min(1.0, max(0.0, (t - t0) / max(1.0, dl - t0)))
            return v0 + (g - v0) * Decimal(str(f))

        if self._pace_anchor is None:
            self._pace_anchor = (self._genesis_ts, Decimal("0"), goal, deadline)
        elif (self._pace_anchor[2], self._pace_anchor[3]) != (goal, deadline) or idle:
            now_v = at(self._pace_anchor, now)
            self._pace_anchor = (now, min(now_v, filled) if idle else now_v, goal, deadline)
        return at(self._pace_anchor, now)

    async def update_processed_data(self):
        now = self.market_data_provider.time()
        if self._genesis_ts is None:
            self._genesis_ts = now
        deadline = self.config.goal_deadline_ts or (self._genesis_ts + self.config.goal_window_s)
        mid = self._px(PriceType.MidPrice)
        on_peg = self.config.peg_floor <= mid <= self.config.peg_ceiling
        base_bal, quote_bal = self._total()
        filled, _ = self._filled_and_fees()
        idle = self.config.hold or self.config.retire or not on_peg
        self.processed_data = {
            "now": now, "deadline": deadline, "mid": mid, "base": base_bal, "quote": quote_bal,
            "on_peg": on_peg, "filled": filled, "pace": self._pace_line(now, deadline, filled, idle),
        }

    @staticmethod
    def _units(usd: Decimal, px: Decimal) -> Decimal:
        return Decimal("0") if px <= 0 else (usd / px).quantize(Decimal("1"), rounding=ROUND_DOWN)

    def _order(self, side: TradeType, amount: Decimal, price: Optional[Decimal], strat: ExecutionStrategy):
        cfg = OrderExecutorConfig(timestamp=self.market_data_provider.time(), connector_name=self.config.connector_name,
                                  trading_pair=self.config.trading_pair, side=side, amount=amount, price=price,
                                  execution_strategy=strat, controller_id=self.config.id)
        return CreateExecutorAction(executor_config=cfg, controller_id=self.config.id)

    def determine_executor_actions(self) -> List[ExecutorAction]:
        pd = self.processed_data
        if not pd:
            return []
        c = self.config
        active = [ex for ex in self.executors_info if ex.is_active]
        value = pd["base"] * pd["mid"] + pd["quote"]
        if self._genesis_value is None:
            self._genesis_value = value

        filled, fees = self._filled_and_fees()
        if self._tripped is None:
            if filled > Decimal("1000") and (fees / filled * 10000) > c.fee_ceiling_bp:
                self._tripped = "fee"
            elif self._genesis_value - value > c.drawdown_ceiling_usd:
                self._tripped = "drawdown"
        if self._tripped or not pd["on_peg"] or c.hold:
            if not pd["on_peg"] and not self._off_peg_logged:
                self.logger().warning(f"[{c.id}] off peg {pd['mid']}")
                self._off_peg_logged = True
            if pd["on_peg"]:
                self._off_peg_logged = False
            return [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in active]

        makers: Dict[TradeType, list] = {TradeType.BUY: [], TradeType.SELL: []}
        takers: list = []
        for ex in active:
            (makers[ex.config.side] if getattr(ex.config, "execution_strategy", None) == ExecutionStrategy.LIMIT_MAKER
             else takers).append(ex)

        base_val, quote_val = pd["base"] * pd["mid"], pd["quote"]
        skew = base_val - (base_val + quote_val) / 2
        bid, ask = self._px(PriceType.BestBid), self._px(PriceType.BestAsk)

        if c.retire:
            if makers[TradeType.BUY] or makers[TradeType.SELL]:
                return [StopExecutorAction(controller_id=c.id, executor_id=ex.id)
                       for ex in makers[TradeType.BUY] + makers[TradeType.SELL]]
            if takers:
                return []
            total = base_val + quote_val
            gap = base_val - total * c.retire_base_share
            if abs(gap) < DUST_USD:
                return []
            tick = self._tick_size()
            if gap > 0:
                px = bid - tick * c.reach_ticks
                return [self._order(TradeType.SELL, self._units(gap, px), px, ExecutionStrategy.LIMIT)]
            px = ask + tick * c.reach_ticks
            return [self._order(TradeType.BUY, self._units(-gap, px), px, ExecutionStrategy.LIMIT)]

        deficit = pd["pace"] - pd["filled"]
        if deficit > c.catchup_trigger_usd and not takers:
            side = TradeType.SELL if skew >= 0 else TradeType.BUY
            if makers[side]:
                return [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in makers[side]]
            tick = self._tick_size()
            px = (bid - tick * c.reach_ticks) if side == TradeType.SELL else (ask + tick * c.reach_ticks)
            rung = c.ladder_steps_usd[1] if deficit > c.catchup_escalate_usd else c.ladder_steps_usd[0]
            sides = self._book_sides()
            if sides is not None:
                far = sides[0] if side == TradeType.SELL else sides[1]
                rung = min(rung, far * pd["mid"] * c.opposing_depth_cap)
            avail = base_val if side == TradeType.SELL else quote_val
            size_usd = min(rung, avail)
            if size_usd >= DUST_USD:
                return [self._order(side, self._units(size_usd, px), px, ExecutionStrategy.LIMIT)]

        # Single-lane: only the skew-correcting side is ever quoted. Tear down any
        # maker left on the idle side first (e.g. right after skew flips direction).
        active_side = TradeType.SELL if skew >= 0 else TradeType.BUY
        idle_side = TradeType.BUY if active_side == TradeType.SELL else TradeType.SELL
        actions: List[ExecutorAction] = [
            StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in makers[idle_side]
        ]

        now = pd["now"]
        timed_out = [ex for ex in makers[active_side] if now - float(ex.config.timestamp) >= c.maker_timeout_s]
        if timed_out:
            self._cross_due = True
            return actions + [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in timed_out]

        touch = {TradeType.BUY: bid, TradeType.SELL: ask}
        if c.widen_step_in:
            tick = self._tick_size()
            if tick > 0:
                width = ((ask - bid) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                if width >= 3:
                    touch = {TradeType.BUY: bid + tick, TradeType.SELL: ask - tick}
                elif width == 2:
                    touch = ({TradeType.BUY: bid, TradeType.SELL: ask - tick} if base_val >= quote_val
                            else {TradeType.BUY: bid + tick, TradeType.SELL: ask})

        allow = skew <= c.skew_ceiling_usd if active_side == TradeType.BUY else skew >= -c.skew_ceiling_usd
        if not allow:
            return actions

        if not makers[active_side] and self._cross_due and not takers:
            self._cross_due = False
            tick = self._tick_size()
            px = (bid - tick * c.reach_ticks) if active_side == TradeType.SELL else (ask + tick * c.reach_ticks)
            avail = base_val if active_side == TradeType.SELL else quote_val
            size_usd = min(c.ladder_steps_usd[0], avail)
            if size_usd >= DUST_USD:
                actions.append(self._order(active_side, self._units(size_usd, px), px, ExecutionStrategy.LIMIT))
            return actions

        stale = [ex for ex in makers[active_side] if Decimal(str(ex.config.price)) != touch[active_side]]
        actions += [StopExecutorAction(controller_id=c.id, executor_id=ex.id) for ex in stale]
        if makers[active_side] and not stale:
            return actions
        free_base, free_quote = self._free()
        spend = free_quote if active_side == TradeType.BUY else free_base * pd["mid"]
        size_usd = min(spend, c.ladder_steps_usd[0])
        if size_usd >= DUST_USD:
            actions.append(self._order(active_side, self._units(size_usd, touch[active_side]), touch[active_side],
                                       ExecutionStrategy.LIMIT_MAKER))
        return actions

    def to_format_status(self) -> List[str]:
        pd = self.processed_data or {}
        return [f"=== {self.config.id} ===", f"  filled={pd.get('filled', 0):.0f} pace={pd.get('pace', 0):.0f}"]
