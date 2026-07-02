# Block 5 — Risk Manager: Implementation Specification

Status: v1. Conforms to `docs/design.md` §3 (Block 5) and §4 (cross-cutting rules).
Language: Python 3.11, asyncio, single process. All timestamps are `float` epoch
seconds obtained from the injected `Clock` (never `time.time()` directly), so the
block is replayable by Block 8.

---

## 1. Overview and responsibilities

The Risk Manager is the **only** path from a `TradeIntent` (Block 4) to an
`OrderRequest` (Block 6). Every intent either becomes exactly one `OrderRequest`
or exactly one `IntentRejection` — never both, never neither.

Responsibilities:

1. **Sizing.** Convert each accepted intent into a quantity using
   conviction-scaled fractional Kelly with hard caps (§5).
2. **Pre-trade checks.** Enforce every limit before the order leaves the block
   (§6). No post-trade-only limits exist: if it isn't checked here, it isn't a limit.
3. **Position & PnL ledger.** Maintain the authoritative internal position,
   average price, realized and unrealized PnL per instrument, updated on every
   fill and every mark (§4).
4. **Kill switch.** Own the four-state kill-switch machine (§8): on daily-loss
   breach, feed failure, reconciliation mismatch, `FlattenCommand`, or manual
   trip, it cancels everything via Block 6 and flattens. Re-arm is manual only.
5. **Publishing.** Emit `PositionState` continuously, `IntentRejection` and
   `KillSwitch` events to the bus (journaled; consumed by Block 9).

Explicit **non-responsibilities** (do NOT implement here):

- **No signal logic.** The Risk Manager never inspects `TradeIntent.reason`
  features to decide direction or timing. It only sizes and gates.
- **No direct broker communication.** It never touches the broker API; all
  orders, cancels, and broker reconciliation go through Block 6 (single-writer
  rule, design §4.2).
- **No execution tactics.** Passive/aggressive order working, retries, and
  slippage measurement belong to Block 6. The Risk Manager emits at most a
  limit price and time-in-force.
- **No calendar ownership.** Session phases and EOD deadlines come from
  Block 7; the Risk Manager reacts to them.

Fail-safe direction (design §4.3): any internal error while processing an
intent results in rejection, not approval. Any unrecoverable internal error
(unhandled exception in the main task) trips the kill switch to
`HALTED_FLATTENING` before re-raising.

---

## 2. Inputs, outputs, bus wiring

| Direction | Message | From/To | Notes |
|---|---|---|---|
| in | `TradeIntent` | Block 4 | one queue, bounded 1 000, overflow = reject with `R_QUEUE_OVERFLOW` |
| in | `Fill` | Block 6 | bounded 10 000; **must never be dropped** — overflow is fatal (trip kill switch) |
| in | `OrderState` | Block 6 | order lifecycle: `NEW, ACKED, PARTIAL, FILLED, CANCELLED, REJECTED` |
| in | `BookState` | Block 2 | for marking, vol estimation, price sanity; bounded 10 000, drop-oldest allowed |
| in | `BookIntegrity` | Block 2 | crossed-book / staleness flags |
| in | `FeedStatus` | Block 1 | `LIVE, STALE, GAP, DOWN` per instrument |
| in | `SessionPhase` | Block 7 | `PRE_OPEN, OPEN_AUCTION, TRADING, WIND_DOWN, FORCE_FLAT, CLOSED` |
| in | `FlattenCommand` | Block 7 | `mode ∈ {PASSIVE, AGGRESSIVE}, deadline_ts` |
| in | `ReconciliationReport` | Block 6 | `{instrument, broker_qty, ts}`; Block 6 publishes after each broker position poll |
| out | `OrderRequest` | Block 6 | §3.2 |
| out | `CancelAllRequest` | Block 6 | `{reason, ts}`; Block 6 cancels every resting order |
| out | `IntentRejection` | bus (Block 9) | §3.3 |
| out | `PositionState` | bus (Blocks 7, 9) | on every fill and on a 1 s timer per non-flat instrument |
| out | `KillSwitchEvent` | bus (Blocks 6, 7, 9) | on every state transition |

All inbound messages are funneled into **one** asyncio consumer task (§9.2) so
all mutable state has a single writer and no locks are needed.

---

## 3. Data structures

All dataclasses use `@dataclass(slots=True, frozen=True)` unless marked
mutable. Quantities are `float` (fractional lots exist on some venues) and are
always an exact integer multiple of the instrument's `lot_size` after sizing.
Prices are `float`, always an exact multiple of `tick_size` where an order
price is emitted.

### 3.1 Enums

```python
class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"

class TimeInForce(str, Enum):
    IOC = "IOC"    # used for MARKET orders and kill-switch flattening
    DAY = "DAY"    # used for LIMIT entries (Block 6 manages working/expiry)

class KillState(str, Enum):
    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    HALTED_FLATTENING = "HALTED_FLATTENING"
    HALTED_FLAT = "HALTED_FLAT"

class KillTrigger(str, Enum):
    DAILY_LOSS_SOFT = "DAILY_LOSS_SOFT"
    DAILY_LOSS_HARD = "DAILY_LOSS_HARD"
    FEED_STALE = "FEED_STALE"
    FEED_DOWN = "FEED_DOWN"
    RECON_MISMATCH = "RECON_MISMATCH"
    FLATTEN_COMMAND_PASSIVE = "FLATTEN_COMMAND_PASSIVE"
    FLATTEN_COMMAND_AGGRESSIVE = "FLATTEN_COMMAND_AGGRESSIVE"
    MANUAL = "MANUAL"
    INTERNAL_ERROR = "INTERNAL_ERROR"

class RejectionCode(str, Enum):
    R_KILL_SWITCH = "R_KILL_SWITCH"          # kill state forbids this intent
    R_STALE_INTENT = "R_STALE_INTENT"        # intent older than max_intent_age_ms
    R_DUPLICATE_INTENT = "R_DUPLICATE_INTENT"# intent_id seen, or in-flight entry on instrument
    R_SESSION_PHASE = "R_SESSION_PHASE"      # phase forbids entries (or reduces)
    R_TIME_TO_CLOSE = "R_TIME_TO_CLOSE"      # intraday feasibility failed
    R_FEED_HEALTH = "R_FEED_HEALTH"          # feed not LIVE / book stale / vol not warm
    R_PRICE_COLLAR = "R_PRICE_COLLAR"        # spread / mid-jump / limit-offset sanity failed
    R_MIN_SIZE = "R_MIN_SIZE"                # sized qty < min_qty after rounding/clamping
    R_POSITION_CAP = "R_POSITION_CAP"        # per-instrument notional cap headroom < 1 lot
    R_GROSS_EXPOSURE = "R_GROSS_EXPOSURE"    # gross notional cap headroom < 1 lot
    R_TRADE_LOSS = "R_TRADE_LOSS"            # qty * stop_distance > max_trade_loss_usd
    R_DAILY_LOSS = "R_DAILY_LOSS"            # projected worst case breaches daily limit
    R_THROTTLE = "R_THROTTLE"                # token bucket empty
    R_QUEUE_OVERFLOW = "R_QUEUE_OVERFLOW"    # intent queue full on enqueue
    R_UNKNOWN_INSTRUMENT = "R_UNKNOWN_INSTRUMENT"  # instrument not in reference config
```

### 3.2 `OrderRequest`

Matches the design-doc schema, extended with `request_id` (idempotency key for
Block 6 retries) and `reduce_only` (required for kill-switch flattening and
REDUCE_ONLY clamping). These two extensions are intentional and must be
mirrored in Block 6's spec.

```python
@dataclass(slots=True, frozen=True)
class OrderRequest:
    request_id: str          # uuid4 hex, unique per emission
    intent_id: str           # originating intent, or "KILL:<trigger>:<instrument>"
    instrument: str
    side: Side
    qty: float               # > 0, integer multiple of lot_size
    type: OrderType
    limit_price: float | None  # required iff type == LIMIT; multiple of tick_size
    time_in_force: TimeInForce
    reduce_only: bool
    ts: float                # emission time (Clock)
```

### 3.3 `IntentRejection`

```python
@dataclass(slots=True, frozen=True)
class IntentRejection:
    intent_id: str
    rule: RejectionCode
    detail: str              # human-readable, includes the numbers that failed
    ts: float
```

`detail` MUST contain the observed value and the limit, e.g.
`"gross_notional after=61,250.00 cap=60,000.00"`.

### 3.4 `PositionState` (published) and internal ledger

```python
@dataclass(slots=True, frozen=True)
class PositionState:
    instrument: str
    qty: float               # signed; + long, - short
    avg_price: float         # volume-weighted open price; 0.0 when flat
    realized_pnl: float      # gross realized PnL since session start (fees excluded)
    unrealized_pnl: float    # (mark - avg_price) * qty
    fees_paid: float         # cumulative fees since session start   (extension)
    mark_price: float        # price used for unrealized_pnl          (extension)
    mark_ts: float           # ts of the BookState behind mark_price  (extension)
    marks_stale: bool        # True if mark older than mark_staleness (extension)
    ts: float
```

Internal mutable ledger entry (not published):

```python
@dataclass(slots=True)
class _Position:
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    mark_price: float = 0.0
    mark_ts: float = 0.0
```

### 3.5 Open-order tracking

```python
@dataclass(slots=True)
class _OpenOrder:
    request_id: str
    intent_id: str
    instrument: str
    side: Side
    qty_total: float
    qty_remaining: float     # decremented by fills
    opening: bool            # True if it increases |position| (counts toward caps)
    stop_distance: float     # $/unit used at sizing time (for projected-loss math)
    created_ts: float
    state: str               # NEW/ACKED/PARTIAL until terminal
```

`self._open_orders: dict[str, _OpenOrder]` keyed by `request_id` (Block 6 must
echo `request_id` as/alongside `order_id` on `Fill` and `OrderState`; if the
venue assigns its own id, Block 6 maintains the mapping). Entries are removed
on terminal `OrderState` (`FILLED`, `CANCELLED`, `REJECTED`).

### 3.6 `KillSwitchEvent`

```python
@dataclass(slots=True, frozen=True)
class KillSwitchEvent:
    state: KillState         # state after the transition
    prev_state: KillState
    trigger: KillTrigger
    reason: str              # detail, e.g. "day_pnl_net=-2050.00 limit=-2000.00"
    ts: float
```

---

## 4. Position & PnL ledger

The ledger is driven **only by `Fill` events** — never by order acks, never by
intents. Broker truth arrives via `ReconciliationReport` and is used only to
detect mismatches (§8), never to silently overwrite the ledger.

### 4.1 Sign conventions

- `qty` is signed: long > 0, short < 0.
- A fill contributes signed quantity `Δ = +fill.qty` for BUY, `-fill.qty` for
  SELL (`fill.qty` is always positive).

### 4.2 Fill application (average-price method)

Let `q` = position qty before the fill, `a` = avg price before, `p` = fill
price, `Δ` = signed fill qty. Fees are **never** folded into `avg_price`; they
accumulate in `fees_paid` on every fill: `fees_paid += fill.fee`.

Case A — opening or increasing (`q == 0` or `sign(Δ) == sign(q)`):

```
avg_price ← (a·|q| + p·|Δ|) / (|q| + |Δ|)
qty       ← q + Δ
```

Case B — reducing without crossing zero (`sign(Δ) ≠ sign(q)` and `|Δ| ≤ |q|`):

```
realized_pnl ← realized_pnl + (p − a) · |Δ| · sign(q)
qty          ← q + Δ
avg_price unchanged; if qty becomes 0, set avg_price ← 0.0
```

Case C — fill crosses zero (`sign(Δ) ≠ sign(q)` and `|Δ| > |q|`). **Position
flips are forbidden at order level** (§5.4): the Risk Manager never emits an
order that can flip. If a fill nonetheless crosses zero (broker overfill,
reconciliation bug), handle it deterministically instead of corrupting the
ledger — split into a close leg and an open leg:

```
realized_pnl ← realized_pnl + (p − a) · |q| · sign(q)      # close leg
qty          ← Δ + q          # residual, opposite sign
avg_price    ← p              # open leg opens at the fill price
```

then log at ERROR and emit a `RiskAnomaly{kind="FILL_FLIP", ...}` event to
Block 9. Two `FILL_FLIP` anomalies in one session trip the kill switch
(`RECON_MISMATCH` trigger).

### 4.3 PnL definitions

Per instrument `i`:

```
unrealized_pnl_i = (mark_i − avg_price_i) · qty_i          # 0 when flat
net_pnl_i        = realized_pnl_i + unrealized_pnl_i − fees_paid_i
```

Portfolio, since session start:

```
day_realized_net = Σ_i (realized_pnl_i − fees_paid_i)
day_pnl_net      = day_realized_net + Σ_i unrealized_pnl_i
equity           = capital_base + day_pnl_net
gross_notional   = Σ_i |qty_i| · mark_i  +  Σ open opening orders' (qty_remaining · ref_price)
```

`ref_price` of an open order = its `limit_price`, or the mid at emission for
MARKET orders. `day_pnl_net` is the number checked against the daily loss
limit; `equity` is the sizing base. All counters reset at session start
(§9.4).

### 4.4 Marking rule

The mark for instrument `i` is derived from the latest `BookState`:

1. **Long position (`qty > 0`): mark = best bid. Short (`qty < 0`): mark =
   best ask.** This is the conservative liquidation mark; it makes unrealized
   PnL slightly pessimistic by construction.
2. If the required side of the book is empty, use `BookState.mid`; if both
   sides empty or `BookIntegrity.crossed_book` is true, do **not** update the
   mark (keep the last valid mark).
3. Flat instruments still track `mark_price` (mid) for gross-exposure math on
   in-flight orders, but their `unrealized_pnl` is 0.

**Staleness fallback.** Let `age = now − mark_ts`.

- `age ≤ mark_staleness_ms` (default 2 000 ms): mark is fresh.
- `age > mark_staleness_ms`: keep the last valid mark, set
  `marks_stale = True` on published `PositionState`, and reject new entries in
  that instrument (`R_FEED_HEALTH`).
- `age > feed_stale_reduce_s` (default 10 s) while `qty ≠ 0`: kill switch →
  `REDUCE_ONLY` (§8).
- `age > feed_stale_halt_s` (default 60 s) while `qty ≠ 0`: kill switch →
  `HALTED_FLATTENING`.

The daily-loss check always uses the last valid mark even if stale — a stale
mark never makes the system *more* permissive because entries in stale
instruments are already blocked.

### 4.5 Volatility estimator (used by sizing, §5.2)

Per instrument, sample `mid` from `BookState` on a 1 s grid (take the latest
`BookState` at each whole second; skip if none). Maintain an EWMA variance of
1-second log returns `r_t = ln(mid_t / mid_{t−1})`:

```
σ²_t = λ · σ²_{t−1} + (1 − λ) · r_t²          λ = vol_ewma_lambda (default 0.97)
σ_1s = sqrt(σ²_t)
```

The estimator is **warm** after `vol_warmup_samples` (default 30) samples.
Until warm, sizing uses the stop-distance floor (§5.2 step 2) and the intent
is otherwise processed normally.

---

## 5. Sizing algorithm — conviction-scaled fractional Kelly with hard caps

Runs as step 8 of intent processing (§6), after all state gates pass.

### 5.1 Parameters

| Name | Type | Default | Range | Meaning |
|---|---|---|---|---|
| `kelly_p` | float | 0.55 | (0.5, 1.0) | calibrated hit rate (from Block 8; per-instrument override allowed) |
| `kelly_b` | float | 1.2 | (0, 10] | calibrated payoff ratio avg_win/avg_loss |
| `kelly_multiplier` | float | 0.25 | (0, 1] | fractional-Kelly damping |
| `f_max` | float | 0.005 | (0, 0.02] | hard cap on equity fraction risked per trade |
| `stop_z` | float | 1.5 | [0.5, 5] | stop distance in units of horizon-scaled σ |
| `stop_floor_bps` | float | 10.0 | [1, 100] | minimum stop distance, bps of price |
| `stop_spread_mult` | float | 2.0 | [1, 10] | minimum stop distance in spreads |

`f_kelly = kelly_p − (1 − kelly_p)/kelly_b` is computed at config load; config
validation fails if `f_kelly ≤ 0`.

### 5.2 Algorithm

Inputs: intent (`conviction c ∈ (0,1]`, `horizon_s h`, side), current
`equity`, current `BookState` (mid `m`, spread `s`), warm `σ_1s`, instrument
reference (`lot_size`, `tick_size`, `min_qty`).

1. **Risk fraction.** `f = min(kelly_multiplier · f_kelly · c, f_max)`.
   Risk budget `R = f · equity` (USD).
2. **Stop distance** ($ per unit):
   `d = max( stop_z · σ_1s · sqrt(h) · m,  stop_floor_bps/10⁴ · m,  stop_spread_mult · s )`.
   If σ is not warm, drop the first term.
   (`d` is a sizing/loss-projection quantity; v1 emits no stop order — the
   horizon exit and EOD flattener close positions. `d` is stored on the
   `_OpenOrder` for projected-loss math.)
3. **Raw quantity.** `qty_raw = R / d`.
4. **Reduce-intent clamp.** If the intent side is opposite to the current
   position sign (a *reducing* intent):
   `qty_raw ← min(qty_raw, |position.qty| − in_flight_reducing_qty)` —
   **position flips are forbidden**; an intent can at most flatten. If the
   clamp yields ≤ 0, reject `R_DUPLICATE_INTENT`
   (detail: `"already flattening in-flight"`).
5. **Rounding.** `qty = floor(qty_raw / lot_size) · lot_size` (round **down**,
   never up).
6. **Minimum size.** If `qty < min_qty` (default `min_qty = lot_size`), reject
   `R_MIN_SIZE`.

Cap clamps (position cap, gross exposure) are applied *after* sizing as checks
9–10 (§6) and re-apply steps 5–6 after any clamp.

### 5.3 Limit price (LIMIT intents only)

`TradeIntent` carries no price; the Risk Manager sets one:

- BUY: `limit_price = floor(microprice / tick_size) · tick_size`, then
  `min(limit_price, best_bid + tick_size)` — passive-side biased.
- SELL: `limit_price = ceil(microprice / tick_size) · tick_size`, then
  `max(limit_price, best_ask − tick_size)`.

Block 6 may work the order more aggressively per its tactics; this price is
the risk-approved bound and Block 6 must not cross beyond it plus its own
configured slippage cap.

### 5.4 Worked numeric example

Config: `capital_base=100 000`, `kelly_p=0.55`, `kelly_b=1.2`,
`kelly_multiplier=0.25`, `f_max=0.005`, `stop_z=1.5`, `stop_floor_bps=10`,
`stop_spread_mult=2`, instrument `XYZ`: `lot_size=1`, `tick_size=0.01`,
`max_instrument_notional=20 000`.

State: flat, `day_pnl_net=0` → `equity=100 000`. Book: bid 49.99 / ask 50.01 →
`mid m=50.00`, `spread s=0.02`. Warm `σ_1s = 0.0002` (2.0 bps).

Intent: BUY, `conviction=0.8`, `horizon_s=120`, `entry_type=LIMIT`.

1. `f_kelly = 0.55 − 0.45/1.2 = 0.175`.
   `f = min(0.25 · 0.175 · 0.8, 0.005) = min(0.035, 0.005) = 0.005`.
   `R = 0.005 · 100 000 = $500.00`.
2. `d = max(1.5 · 0.0002 · √120 · 50.00, 0.0010 · 50.00, 2 · 0.02)`
   `= max(0.16432, 0.05, 0.04) = $0.16432/share`.
3. `qty_raw = 500 / 0.16432 = 3 042.9` shares.
4. Not a reducing intent — no clamp.
5. Round down to lot 1 → 3 042.
6. `3 042 ≥ 1` — passes min size.

Check 9 (position cap): notional `3 042 · 50.00 = 152 100 > 20 000` headroom →
clamp to `floor(20 000/50.00 /1)·1 = 400` shares; re-round (no-op), re-check
min size (passes).
Check 11 (per-trade loss): `400 · 0.16432 = $65.73 ≤ 500` ✓.
Result: `OrderRequest{instrument=XYZ, side=BUY, qty=400, type=LIMIT,
limit_price=50.00 (microprice 49.998 → floor 49.99, min(49.99, bid+tick=50.00)
→ 49.99... see below), time_in_force=DAY, reduce_only=False}`.

Limit-price detail for this book: microprice ≈ 49.998 → floor to tick =
49.99 → `min(49.99, 49.99 + 0.01) = 49.99`. Emitted `limit_price = 49.99`.

---

## 6. Pre-trade checks — complete ordered list

Checks run **in this exact order** with **short-circuit on first failure**:
exactly one `IntentRejection{intent_id, rule, detail}` is emitted and
processing stops. Throttle tokens (check 13) are consumed **only if every
earlier check passed**, so rejected intents never burn rate budget.

Definitions used below:
- *Opening intent*: increases `|position|` (same side as position, or flat).
- *Reducing intent*: opposite side of a non-zero position (qty clamped, §5.2.4).

| # | Check | Exact condition to PASS | Parameters (default) | Rejection code |
|---|---|---|---|---|
| 1 | Kill switch | `state == NORMAL`, or `state == REDUCE_ONLY` and the intent is reducing | — | `R_KILL_SWITCH` |
| 2 | Known instrument | `intent.instrument` present in reference config | universe list | `R_UNKNOWN_INSTRUMENT` |
| 3 | Intent freshness | `now − intent.ts ≤ max_intent_age_ms` | `max_intent_age_ms = 500` | `R_STALE_INTENT` |
| 4 | Duplicate / in-flight | `intent.intent_id` not in the session dedupe set, AND no open (non-terminal) **opening** order exists for the instrument (at most one working entry per instrument) | — | `R_DUPLICATE_INTENT` |
| 5 | Session phase | opening: `phase == TRADING`; reducing: `phase ∈ {TRADING, WIND_DOWN}` | phases from Block 7 | `R_SESSION_PHASE` |
| 6 | Intraday feasibility | opening only: `seconds_to_close ≥ intent.horizon_s + flatten_buffer_s` | `flatten_buffer_s = 120` | `R_TIME_TO_CLOSE` |
| 7 | Feed health | `FeedStatus == LIVE`, AND `now − mark_ts ≤ mark_staleness_ms`, AND latest `BookIntegrity.ok` and not `crossed_book` | `mark_staleness_ms = 2000` | `R_FEED_HEALTH` |
| 8 | Price sanity collar | (a) `spread ≤ max_spread_bps/10⁴ · mid`; (b) `|mid − mid_ewma_5s| ≤ max_mid_jump_bps/10⁴ · mid_ewma_5s`; (c) LIMIT only: computed `limit_price` within `max_limit_offset_bps` of mid | `max_spread_bps = 20`, `max_mid_jump_bps = 100`, `max_limit_offset_bps = 50`; `mid_ewma_5s`: EWMA of 1 s mids, λ=0.87 (≈5 s half-life; see note below) | `R_PRICE_COLLAR` |
| — | **Sizing (§5)** runs here | produces `qty`, `d` | §5.1 | `R_MIN_SIZE` |
| 9 | Per-instrument position cap | `(|qty_after| ) · mid + in_flight_opening_notional_i ≤ max_instrument_notional`; if not, clamp `qty` to headroom, re-round down, re-check min size; reject only if headroom < 1 lot | `max_instrument_notional = 20 000` (per-instrument override allowed) | `R_POSITION_CAP` |
| 10 | Gross exposure cap | `gross_notional + qty · mid ≤ max_gross_notional`; clamp-to-headroom semantics identical to #9 | `max_gross_notional = 60 000` | `R_GROSS_EXPOSURE` |
| 11 | Per-trade max loss | `qty · d ≤ max_trade_loss_usd` (via stop distance `d`, §5.2.2); clamp not allowed — reject outright (a clamp here would silently change the risk model) | `max_trade_loss_usd = 500` | `R_TRADE_LOSS` |
| 12 | Daily loss (projected) | `day_pnl_net − qty · d > −daily_loss_limit_usd` (worst-case projection must not breach) | `daily_loss_limit_usd = 2 000` | `R_DAILY_LOSS` |
| 13 | Order-rate throttle | 1 token available in the **global** bucket AND in the **per-instrument** bucket; consume one from each on pass | §6.1 | `R_THROTTLE` |

Note on 8(b): use λ per 1 s step = 0.87 for a 5 s half-life
(`0.87⁵ ≈ 0.50`); the constant is `mid_ewma_lambda` in config.

Rationale for the order: state gates (1–7) are O(1) and independent of size;
price sanity (8) must precede sizing because sizing reads the book; caps
(9–10) can clamp so they follow sizing; loss checks (11–12) use the final
qty; the throttle is last so only fully-approved intents spend tokens.

Reducing intents skip nothing except #6 (feasibility) — they still pass
throttle, collar, etc. Kill-switch flattening orders (§8) bypass this pipeline
entirely (they are generated internally, not from intents) but are journaled
identically.

### 6.1 Token-bucket throttle

Two buckets, refilled lazily on each check (`tokens = min(capacity, tokens +
rate · (now − last_refill_ts))`):

| Bucket | Capacity (burst) | Refill rate | Meaning |
|---|---|---|---|
| Global | `throttle_global_burst = 10` tokens | `throttle_global_rate = 2.0` tokens/s | ≤ 2 orders/s sustained, bursts of 10 |
| Per instrument | `throttle_instrument_burst = 3` | `throttle_instrument_rate = 0.5` tokens/s | ≤ 1 order per 2 s sustained per instrument, bursts of 3 |

Both buckets start full at session start. A pass consumes exactly 1 token from
each. Kill-switch flattening and Block 7-driven flatten orders are **exempt**
(safety actions are never throttled).

---

## 7. Intent-processing and fill-processing flows

### 7.1 Intent flow (numbered)

1. Dequeue `TradeIntent`; record `intent_id` in the dedupe set (even if later
   rejected — a retried intent id is always `R_DUPLICATE_INTENT`).
2. Run checks 1–8 (§6). On first failure: emit `IntentRejection`, journal,
   stop.
3. Run sizing (§5): compute `f, R, d, qty_raw`, apply reduce-clamp, round,
   min-size check.
4. Run checks 9–13 with clamping as specified.
5. Build `OrderRequest` (uuid `request_id`; `type` and `time_in_force`: MARKET
   → IOC, LIMIT → DAY; `limit_price` per §5.3; `reduce_only = True` iff
   reducing intent).
6. Insert `_OpenOrder{opening, stop_distance=d, qty_remaining=qty, ...}` into
   `self._open_orders` **before** publishing (so a fast fill can never race an
   unknown order).
7. Publish `OrderRequest` to Block 6; journal.

### 7.2 Fill flow (numbered)

1. Dequeue `Fill{order_id, intent_id, instrument, qty, price, fee, ts}`.
2. **Dedupe**: key `(order_id, ts, qty, price)`; if seen this session, log
   WARN, drop.
3. Look up `_OpenOrder` by `order_id` (= `request_id`). If unknown → error
   path (§11 row 1): apply to ledger anyway, log ERROR, request
   reconciliation from Block 6.
4. Apply to ledger per §4.2 (cases A/B/C); update `fees_paid`.
5. `open_order.qty_remaining −= fill.qty`; if `≤ 1e-9`, mark locally complete
   (removal still waits for terminal `OrderState`).
6. Recompute `gross_notional`, `day_pnl_net`.
7. Publish `PositionState` for the instrument.
8. Kill-switch evaluation (§8.2): soft/hard daily-loss thresholds against the
   updated `day_pnl_net`.
9. If `state == HALTED_FLATTENING` and all positions are 0 and
   `self._open_orders` is empty → transition to `HALTED_FLAT`.

### 7.3 `OrderState` flow

- `REJECTED` or `CANCELLED`: remove from `_open_orders`, release its reserved
  notional from cap math, journal. A `REJECTED` opening order does **not**
  refund throttle tokens.
- `FILLED`: remove after ledger has consumed the fills (fills and order states
  may interleave; removal on terminal state is unconditional — any residual
  `qty_remaining > 0` at removal is logged at ERROR and triggers a
  reconciliation request).

---

## 8. Kill-switch state machine

### 8.1 States and permitted activity

| State | New entries | Reducing orders | Marking/publishing | Notes |
|---|---|---|---|---|
| `NORMAL` | yes | yes | yes | full pipeline |
| `REDUCE_ONLY` | **no** (`R_KILL_SWITCH`) | yes (clamped) | yes | positions may only shrink |
| `HALTED_FLATTENING` | no | internal flatten orders only; all intents rejected | yes | actively cancelling + flattening |
| `HALTED_FLAT` | no | no (nothing to reduce) | yes | terminal until manual re-arm or next session |

### 8.2 Triggers and transitions

Escalation only; the machine never auto-de-escalates. `→` means "transition
to"; a trigger targeting a state ≤ current state is ignored (logged).

| Trigger | Condition (exact) | Target state |
|---|---|---|
| `DAILY_LOSS_SOFT` | `day_pnl_net ≤ −daily_loss_soft_frac · daily_loss_limit_usd` (default frac 0.8 → −$1 600) | `REDUCE_ONLY` |
| `DAILY_LOSS_HARD` | `day_pnl_net ≤ −daily_loss_limit_usd` (−$2 000) | `HALTED_FLATTENING` |
| `FEED_STALE` | any instrument with `qty ≠ 0` has `FeedStatus ∈ {STALE, GAP}` or mark age > `feed_stale_reduce_s` (10 s), continuously | `REDUCE_ONLY` |
| `FEED_STALE` (escalation) | same, persisting beyond `feed_stale_halt_s` (60 s) | `HALTED_FLATTENING` |
| `FEED_DOWN` | any instrument with `qty ≠ 0` has `FeedStatus == DOWN` for > `feed_down_halt_s` (5 s) | `HALTED_FLATTENING` |
| `RECON_MISMATCH` | `ReconciliationReport.broker_qty ≠ ledger qty` (tolerance 0) for any instrument **and** no open order on that instrument (in-flight fills can explain transient diffs; re-check on next report) — or 2× `FILL_FLIP` anomalies (§4.2) | `HALTED_FLATTENING` |
| `FLATTEN_COMMAND_PASSIVE` | `FlattenCommand{mode=PASSIVE}` from Block 7 | `REDUCE_ONLY` |
| `FLATTEN_COMMAND_AGGRESSIVE` | `FlattenCommand{mode=AGGRESSIVE}` from Block 7 | `HALTED_FLATTENING` |
| `MANUAL` | operator `trip()` call (control socket / CLI) | `HALTED_FLATTENING` |
| `INTERNAL_ERROR` | unhandled exception in the consumer task | `HALTED_FLATTENING` |
| (internal) | in `HALTED_FLATTENING`: all `qty == 0` and no open orders | `HALTED_FLAT` |

Daily-loss triggers are evaluated after every fill and after every mark update
(so pure mark-to-market drawdown trips it too, not only realized losses).

### 8.3 Actions on entering each state

Every transition emits `KillSwitchEvent` (journaled, alerts via Block 9).

- **→ `REDUCE_ONLY`**: pipeline behavior changes per §8.1, and existing
  resting **opening** orders are cancelled: emit one
  `CancelOrderRequest{request_id}` (Block 6 message) per open order with
  `opening=True`. Reducing orders are left working.
- **→ `HALTED_FLATTENING`** (interaction with Block 6, in order):
  1. Emit `KillSwitchEvent` and `CancelAllRequest{reason}` — Block 6 cancels
     every resting order first, so flatten orders can't double-execute
     against our own resting liquidity.
  2. Wait until all `_open_orders` reach terminal `OrderState` **or**
     `cancel_all_timeout_ms` (default 2 000 ms) elapses, whichever is first.
  3. For every instrument with `qty ≠ 0`, emit
     `OrderRequest{side = SELL if qty > 0 else BUY, qty = ceil(|qty| /
     lot_size) · lot_size, type=MARKET, time_in_force=IOC, reduce_only=True,
     intent_id=f"KILL:{trigger}:{instrument}"}`. Quantity rounds **up** to a
     lot so no dust is left; `reduce_only=True` makes Block 6/venue cap the
     execution at the actual position, so the round-up cannot flip. Exempt
     from throttle.
  4. Re-check every `flatten_retry_s` (default 5 s): while any `qty ≠ 0`,
     re-emit flatten orders for the residual (previous flatten IOCs may have
     partially filled). Each retry is journaled; > 5 retries raises a
     critical alert (Block 9) — Block 7's independent FORCE_FLAT remains the
     backstop.
- **→ `HALTED_FLAT`**: stop flatten retries. Only marking, `PositionState`
  publishing, and journaling continue.

### 8.4 Re-arm policy — manual only

There is **no automatic re-arm**. Mechanism:

- `RiskManager.rearm(operator_id: str, note: str) -> bool`, exposed via the
  ops control socket (a UNIX domain socket accepting line-delimited JSON
  commands `{"cmd": "rearm", "operator": ..., "note": ...}`; same socket
  serves `{"cmd": "trip", ...}`). The CLI wrapper is `botctl risk rearm`.
- Allowed **only** from `REDUCE_ONLY` or `HALTED_FLAT`. A re-arm attempt in
  `HALTED_FLATTENING` returns `False` (still flattening) and is journaled.
- If the current session already breached the **hard** daily loss, re-arm is
  refused for the rest of the session (`day_pnl_net` would immediately
  re-trip anyway; the refusal makes the policy explicit).
- On success: state → `NORMAL`, throttle buckets reset to full, a
  `KillSwitchEvent{state=NORMAL, trigger=MANUAL, reason=f"rearm by
  {operator_id}: {note}"}` is journaled. Loss counters are **not** reset —
  re-arming does not grant fresh loss budget.
- Process restart does not re-arm: kill state (state + trigger) is persisted
  to the journal and restored on startup replay (§9.4).

---

## 9. Public API, lifecycle, asyncio model

### 9.1 Class and method signatures

```python
class RiskManager:
    def __init__(
        self,
        config: RiskConfig,                 # parsed + validated (§10)
        bus: EventBus,                      # subscribe/publish typed topics
        clock: Clock,                       # .now() -> float; injected for replay
        instruments: dict[str, InstrumentRef],  # lot_size, tick_size, overrides
    ) -> None: ...

    async def run(self) -> None:
        """Main loop: consume the merged inbound queue until cancelled.
        Also runs the 1 s housekeeping timer (marks, PositionState, staleness,
        flatten retries). Cancellation-safe; trips INTERNAL_ERROR on crash."""

    # --- inbound handlers (called only from the run() task) ---
    def _on_trade_intent(self, m: TradeIntent) -> None: ...
    def _on_fill(self, m: Fill) -> None: ...
    def _on_order_state(self, m: OrderState) -> None: ...
    def _on_book_state(self, m: BookState) -> None: ...
    def _on_book_integrity(self, m: BookIntegrity) -> None: ...
    def _on_feed_status(self, m: FeedStatus) -> None: ...
    def _on_session_phase(self, m: SessionPhase) -> None: ...
    def _on_flatten_command(self, m: FlattenCommand) -> None: ...
    def _on_reconciliation(self, m: ReconciliationReport) -> None: ...

    # --- ops control (thread-safe entry points; enqueue a control message) ---
    def trip(self, reason: str, operator_id: str = "system") -> None: ...
    def rearm(self, operator_id: str, note: str) -> bool: ...

    # --- introspection (read-only, for dashboard/Block 9) ---
    def snapshot(self) -> RiskSnapshot:
        """Frozen copy: kill state, day_pnl_net, equity, gross_notional,
        per-instrument PositionState, open-order count, bucket levels."""
```

Pure helper functions (unit-test targets, no I/O, no clock):

```python
def size_intent(intent: TradeIntent, equity: float, sigma_1s: float | None,
                book: BookState, ref: InstrumentRef, cfg: SizingConfig,
                position_qty: float, in_flight_reducing: float
                ) -> SizingResult | RejectionCode: ...

def apply_fill(pos: _Position, side: Side, qty: float, price: float,
               fee: float) -> FillEffect: ...   # implements §4.2 exactly
```

### 9.2 Asyncio model

- **One consumer task** (`run()`) owns all mutable state. Inbound topics are
  merged into a single `asyncio.Queue` of `(topic, message)` tuples by
  lightweight forwarder tasks (or by the bus itself if it supports fan-in).
  No locks anywhere.
- Ordering guarantee: messages from the same source topic are processed in
  publication order. Cross-topic ordering follows queue arrival.
- The 1 s housekeeping tick is an entry on the same queue (enqueued by a timer
  task), so it can never run concurrently with a handler.
- `trip()`/`rearm()` from the control-socket task enqueue control messages;
  they do not mutate state directly. `rearm()` awaits a response future
  (≤ 1 s timeout) for its `bool`.
- Backpressure: intent queue full → immediately emit
  `IntentRejection{R_QUEUE_OVERFLOW}` from the forwarder. Fill queue full →
  `trip("fill queue overflow")` — losing fills is never acceptable.

### 9.3 Latency budget

Intent → `OrderRequest`/`IntentRejection` in ≤ 5 ms p99 on the target host
(all checks are O(1); no I/O on the hot path except bus publish).

### 9.4 Lifecycle

1. **Startup**: load + validate config (fail fast); subscribe topics; if the
   journal contains events for the current session (crash restart), replay
   own inputs (fills, order states, kill events) to rebuild ledger, dedupe
   sets, and kill state **before** consuming live messages.
2. **Session start** (first `SessionPhase{PRE_OPEN}` of a new trading date):
   reset `realized_pnl`, `fees_paid`, `unrealized`, dedupe sets, throttle
   buckets; kill state resets to `NORMAL` **unless** yesterday ended not-flat
   (any `qty ≠ 0`), in which case start in `HALTED_FLATTENING` and alert.
3. **Shutdown** (task cancelled): publish final `PositionState` for all
   instruments, flush journal, exit. No orders on shutdown — Block 7 owns EOD.

---

## 10. Configuration

Section `risk:` of the versioned YAML config (design §4.4). Complete example
with defaults:

```yaml
risk:
  capital_base_usd: 100000.0        # float, > 0. Sizing/loss base for the session.

  sizing:
    kelly_p: 0.55                   # float, (0.5, 1.0)
    kelly_b: 1.2                    # float, (0, 10]
    kelly_multiplier: 0.25          # float, (0, 1]
    f_max: 0.005                    # float, (0, 0.02]
    stop_z: 1.5                     # float, [0.5, 5]
    stop_floor_bps: 10.0            # float, [1, 100]
    stop_spread_mult: 2.0           # float, [1, 10]
    vol_ewma_lambda: 0.97           # float, (0.8, 0.999)
    vol_warmup_samples: 30          # int, [10, 600]

  limits:
    max_instrument_notional_usd: 20000.0   # float, > 0; per-instrument default
    max_gross_notional_usd: 60000.0        # float, >= max_instrument_notional_usd
    max_trade_loss_usd: 500.0              # float, > 0
    daily_loss_limit_usd: 2000.0           # float, > 0
    daily_loss_soft_frac: 0.8              # float, (0, 1)
    min_qty_lots: 1                        # int, >= 1 (min order size in lots)

  throttle:
    global_burst: 10                # int, [1, 100]
    global_rate_per_s: 2.0          # float, (0, 50]
    instrument_burst: 3             # int, [1, 20]
    instrument_rate_per_s: 0.5      # float, (0, 10]

  sanity:
    max_intent_age_ms: 500          # int, [50, 5000]
    max_spread_bps: 20.0            # float, (0, 500]
    max_mid_jump_bps: 100.0         # float, (0, 1000]
    max_limit_offset_bps: 50.0      # float, (0, 500]
    mid_ewma_lambda: 0.87           # float, (0.5, 0.99); ~5 s half-life at 1 s steps

  timing:
    flatten_buffer_s: 120           # int, [30, 1800]
    mark_staleness_ms: 2000         # int, [500, 30000]
    feed_stale_reduce_s: 10         # int, [2, 120]
    feed_stale_halt_s: 60           # int, > feed_stale_reduce_s
    feed_down_halt_s: 5             # int, [1, 60]
    cancel_all_timeout_ms: 2000     # int, [500, 10000]
    flatten_retry_s: 5              # int, [1, 60]

  overrides:                        # optional per-instrument overrides
    XYZ:
      max_instrument_notional_usd: 20000.0
      kelly_p: 0.55
      kelly_b: 1.2
```

Validation at load (fail fast, refuse to start): every range above;
`f_kelly > 0`; `max_gross_notional_usd ≥ max_instrument_notional_usd`;
`daily_loss_soft_frac · daily_loss_limit_usd < daily_loss_limit_usd` (trivially
true given ranges); every `overrides` key present in the instrument universe.

---

## 11. Error handling

| # | Situation | Detection | Action |
|---|---|---|---|
| 1 | Fill for unknown `order_id` | lookup miss in `_open_orders` (and not a known kill-flatten id) | Apply the fill to the ledger anyway (fills are position truth), log ERROR, emit `RiskAnomaly{kind="UNKNOWN_ORDER_FILL"}`, request an immediate `ReconciliationReport` from Block 6. If the next report mismatches → `RECON_MISMATCH` trip. |
| 2 | Duplicate fill | dedupe key `(order_id, ts, qty, price)` seen | Drop, log WARN, count; > 10/session → alert Block 9. |
| 3 | Out-of-order fills (fill `ts` earlier than last applied fill's `ts` by > 1 s) | ts comparison on apply | Apply in **arrival order** regardless (avg-price accounting is applied sequentially; arrival order is the journal order and therefore the replayable truth). Log WARN with both timestamps. Never reorder or buffer. |
| 4 | Fill crosses zero (position flip) | §4.2 case C | Split close/open legs, ERROR log, `RiskAnomaly{FILL_FLIP}`; 2 per session → trip `RECON_MISMATCH`. |
| 5 | Fill after terminal `OrderState` (late fill) | `_open_orders` entry already removed | Same as row 1 (ledger-apply + reconcile); this is the common benign race, so first occurrence logs WARN not ERROR. |
| 6 | Marking with stale book | `now − mark_ts > mark_staleness_ms` | Keep last valid mark; `marks_stale=True` in `PositionState`; block new entries (`R_FEED_HEALTH`); escalate per §4.4 / §8.2 timers. Never mark from a crossed or empty book. |
| 7 | Crossed book / integrity not ok | `BookIntegrity` | Freeze mark updates for the instrument; entries fail check 7. |
| 8 | `residual qty_remaining > 0` on terminal `FILLED` | §7.3 | ERROR log + reconciliation request. |
| 9 | Intent with `ts` in the future (> 1 s ahead of clock) | freshness check | Reject `R_STALE_INTENT` (detail "clock skew"), alert Block 9 (clock skew is a fail-safe trigger for Block 7). |
| 10 | Config reload (SIGHUP / `botctl config reload`) | ops command | Re-parse and validate the new file; on any validation error keep the old config and alert. Apply **tightening-only** changes immediately (any limit that is ≤ its old value, any rate that is ≤ old). Loosening changes are staged and applied at the next session start. The applied config hash is journaled either way. |
| 11 | Unhandled exception in `run()` | try/except around dispatch | `trip("INTERNAL_ERROR: …")`, journal the traceback, re-raise after emitting `CancelAllRequest` (Block 6 also cancels all on Risk-crash detection per design §4.3 — both paths are armed). |
| 12 | Fill queue overflow | queue put fails | Trip kill switch (`INTERNAL_ERROR`), never drop a fill. |
| 13 | `rearm` while `HALTED_FLATTENING` | §8.4 | Return `False`, journal the attempt. |

---

## 12. Test plan

All tests use the config of §10 verbatim unless a line says otherwise, the
instrument `XYZ` (`lot_size=1`, `tick_size=0.01`), a fake `Clock`, and a book
of bid 49.99 / ask 50.01 (mid 50.00, spread 0.02) with warm `σ_1s = 0.0002`.
Session phase `TRADING`, `seconds_to_close = 10 000`, feed `LIVE`, kill state
`NORMAL`, flat, `day_pnl_net = 0` unless stated.

### 12.1 End-to-end happy path

- **T1 — full intent → checks → OrderRequest.** Intent `{BUY, conviction=0.8,
  horizon_s=120, entry_type=LIMIT}`. Assert every intermediate value of the
  worked example §5.4: `f=0.005`, `R=500.00`, `d=0.16432` (±1e-5),
  `qty_raw=3042.9`, position-cap clamp to 400, and the emitted
  `OrderRequest{side=BUY, qty=400, type=LIMIT, limit_price=49.99,
  time_in_force=DAY, reduce_only=False}`. Assert exactly one global and one
  instrument token consumed, and `_open_orders` has one entry with
  `stop_distance≈0.16432`, `opening=True`.

### 12.2 Every pre-trade check — one pass, one fail each

(The "pass" case for each check is T1 unless noted; each row's fail case
changes only the listed inputs and asserts the exact rejection code and that
**no** `OrderRequest` was emitted and no tokens consumed.)

- **T2 kill switch.** Fail: state `REDUCE_ONLY`, opening intent →
  `R_KILL_SWITCH`. Pass: state `REDUCE_ONLY`, position +400, intent SELL
  (reducing) → order emitted with `reduce_only=True`, qty ≤ 400.
- **T3 unknown instrument.** Fail: intent for `ABC` (not in universe) →
  `R_UNKNOWN_INSTRUMENT`.
- **T4 freshness.** Fail: `intent.ts = now − 0.6` (600 ms) → `R_STALE_INTENT`.
  Pass: 400 ms old.
- **T5 duplicate/in-flight.** Fail (a): resend the same `intent_id` →
  `R_DUPLICATE_INTENT`. Fail (b): T1's order still open (non-terminal), new
  BUY intent on XYZ → `R_DUPLICATE_INTENT`. Pass: after
  `OrderState{FILLED}` removes the open order, a new intent passes check 4.
- **T6 session phase.** Fail: phase `WIND_DOWN`, opening intent →
  `R_SESSION_PHASE`. Pass: phase `WIND_DOWN`, reducing intent (position
  +400, SELL) → approved.
- **T7 intraday feasibility.** Fail: `seconds_to_close = 200`,
  `horizon_s = 120` → `200 < 120 + 120` → `R_TIME_TO_CLOSE`. Pass:
  `seconds_to_close = 240` → `240 ≥ 240` → approved.
- **T8 feed health.** Fail (a): `FeedStatus=STALE` → `R_FEED_HEALTH`.
  Fail (b): last `BookState` 2.5 s old → `R_FEED_HEALTH`. Fail (c):
  `BookIntegrity.crossed_book=True` → `R_FEED_HEALTH`.
- **T9 price collar.** Fail (a): book 49.90/50.10 → spread 40 bps > 20 →
  `R_PRICE_COLLAR`. Fail (b): `mid_ewma_5s = 50.00`, book jumps to
  50.60/50.62 (mid 50.61, jump 122 bps > 100) → `R_PRICE_COLLAR`. Pass: jump
  of 50 bps.
- **T10 min size.** Fail: `capital_base=1 000` (equity 1 000) → `R=5.00`,
  `qty_raw = 5.00/0.16432 = 30.4` → 30 shares — passes; instead use
  instrument override `lot_size=100`: `floor(30.4/100)·100 = 0 < min_qty` →
  `R_MIN_SIZE`. Pass: lot 1.
- **T11 position cap.** Fail: existing position 399 long
  (`399·50 = 19 950`), headroom `50/50.00 = 1.0` share but
  `in_flight_opening_notional = 40` → headroom 0.2 → floor to lot = 0 →
  `R_POSITION_CAP`. Pass (clamp): T1 (flat → clamp 3 042 → 400).
- **T12 gross exposure.** Fail: positions in other instruments totalling
  `59 990` gross; headroom `10/50 = 0.2` shares → `R_GROSS_EXPOSURE`. Pass
  (clamp): gross `45 000` → headroom `15 000` → qty clamped
  `min(400, 300) = 300` and order emitted for 300.
- **T13 per-trade loss.** Fail: `max_trade_loss_usd = 50` →
  `400 · 0.16432 = 65.73 > 50` → `R_TRADE_LOSS` (no clamp). Pass: default 500.
- **T14 daily loss projection.** Fail: `day_pnl_net = −1 950`; projected
  `−1 950 − 65.73 = −2 015.73 ≤ −2 000` → `R_DAILY_LOSS` (and state is
  already `REDUCE_ONLY` from the soft trigger — so run with soft frac 0.99 to
  isolate, or assert `R_KILL_SWITCH`/`R_DAILY_LOSS` per configured frac;
  test both configs). Pass: `day_pnl_net = −1 900`, projected −1 965.73 →
  approved.
- **T15 throttle, per-instrument.** 4 approvable intents on XYZ at t=0
  (distinct ids; complete each prior order with `FILLED` so check 4 passes):
  intents 1–3 approved, 4th → `R_THROTTLE`. At t=+2 s (0.5/s refill → 1
  token) the 5th is approved.
- **T16 throttle, global.** 11 approvable intents across 11 instruments at
  t=0: 10 approved, 11th → `R_THROTTLE`; at t=+0.5 s one more passes
  (2/s refill).
- **T17 rejected intents don't burn tokens.** 50 intents failing check 7
  (feed STALE) → bucket levels unchanged.

### 12.3 PnL accounting (partial fills + fees)

- **T18 — ledger worked example.** Order BUY 500 fills in two parts, then a
  partial exit:
  1. Fill BUY 300 @ 50.00, fee 0.30 → `qty=300, avg=50.00, realized=0,
     fees=0.30`.
  2. Fill BUY 200 @ 50.10, fee 0.20 → `avg = (50.00·300 + 50.10·200)/500 =
     50.04`, `qty=500`, `fees=0.50`.
  3. Fill SELL 400 @ 50.20, fee 0.40 → `realized += (50.20 − 50.04)·400·(+1)
     = +64.00`, `qty=100`, `avg=50.04` (unchanged), `fees=0.90`.
  4. Mark: book 50.15/50.17, long → mark = bid 50.15 →
     `unrealized = (50.15 − 50.04)·100 = +11.00`.
  Assert `PositionState{qty=100, avg_price=50.04, realized_pnl=64.00,
  unrealized_pnl=11.00, fees_paid=0.90}` and
  `day_pnl_net = 64.00 − 0.90 + 11.00 = 74.10`.
- **T19 — short-side symmetry.** SELL 200 @ 50.00 (fee 0.20), mark ask
  49.80 → `unrealized = (49.80 − 50.00)·(−200) = +40.00`. Cover BUY 200 @
  49.90 fee 0.20 → `realized = (49.90 − 50.00)·200·(−1) = +20.00`, flat,
  `avg_price=0.0`.
- **T20 — flip-split fill (§4.2 C).** Position +100 @ 50.00; rogue fill SELL
  150 @ 50.10 fee 0.15 → `realized += (50.10−50.00)·100 = +10.00`,
  `qty = −50`, `avg = 50.10`; assert ERROR log + `RiskAnomaly{FILL_FLIP}`.
  A second flip fill in the same session → kill switch `HALTED_FLATTENING`
  with trigger `RECON_MISMATCH`.
- **T21 — duplicate & unknown fills.** Replay fill (2) of T18 → dropped,
  ledger unchanged. Fill with unknown `order_id` → applied, ERROR +
  reconciliation request (assert both).
- **T22 — stale marking.** No `BookState` for 2.5 s → published
  `PositionState.marks_stale=True`, mark unchanged; entry intent →
  `R_FEED_HEALTH`.

### 12.4 Kill switch

- **T23 — soft daily loss.** Fills bring `day_realized_net` to −1 400; mark
  moves unrealized to −250 → `day_pnl_net = −1 650 ≤ −1 600` →
  `KillSwitchEvent{REDUCE_ONLY, DAILY_LOSS_SOFT}`; opening intent →
  `R_KILL_SWITCH`; reducing intent approved.
- **T24 — hard daily loss on a mark, full flatten sequence.** Position +400 @
  50.04, `day_realized_net = −1 400`; book drops to 48.41/48.43 → mark 48.41
  → `unrealized = (48.41−50.04)·400 = −652.00` → `day_pnl_net = −2 052 ≤
  −2 000`. Assert ordered outputs: (1) `KillSwitchEvent{HALTED_FLATTENING,
  DAILY_LOSS_HARD}`; (2) `CancelAllRequest`; (3) after `cancel_all_timeout_ms`
  (or all terminal `OrderState`s), `OrderRequest{SELL, 400, MARKET, IOC,
  reduce_only=True, intent_id="KILL:DAILY_LOSS_HARD:XYZ"}`. Partial flatten
  fill of 250 → after `flatten_retry_s`, a retry `OrderRequest{SELL, 150,…}`.
  Final fill → all `qty=0`, no open orders → `KillSwitchEvent{HALTED_FLAT}`.
- **T25 — feed escalation.** Position open; `FeedStatus=STALE` for 10 s →
  `REDUCE_ONLY`; persists 60 s → `HALTED_FLATTENING`. Separately:
  `FeedStatus=DOWN` for 5 s → straight to `HALTED_FLATTENING`. Flat book
  (no positions) + STALE → **no** transition (assert).
- **T26 — reconciliation mismatch.** Ledger +400, no open orders,
  `ReconciliationReport{broker_qty=300}` → `HALTED_FLATTENING`
  (`RECON_MISMATCH`), flatten order sized to the **ledger** qty (400) with an
  alert carrying both numbers. With an open order in flight the same report
  does not trip (assert), but the next report after terminal state does.
- **T27 — FlattenCommand.** `{mode=PASSIVE}` → `REDUCE_ONLY`;
  `{mode=AGGRESSIVE}` → `HALTED_FLATTENING` + cancel-all + flatten.
- **T28 — manual trip + re-arm policy.** `trip("operator")` from NORMAL →
  `HALTED_FLATTENING`; `rearm()` during flattening → `False`; after
  `HALTED_FLAT`, `rearm("alice", "checked")` → `True`, state NORMAL,
  buckets full, loss counters unchanged. After a **hard** daily-loss trip,
  `rearm` in the same session → `False`. No transition ever occurs without a
  `KillSwitchEvent` (assert on every kill test).
- **T29 — escalation only.** In `HALTED_FLATTENING`, a `DAILY_LOSS_SOFT`
  trigger is ignored (state unchanged, logged).
- **T30 — flatten orders bypass throttle.** Drain both buckets, then trigger
  T24: flatten `OrderRequest` still emitted.

### 12.5 Determinism

- **T31 — replay.** Journal every input of T1+T18+T24; re-run the block from
  the journal with the same config: byte-identical sequence of emitted
  `OrderRequest`/`IntentRejection`/`PositionState`/`KillSwitchEvent` messages
  (excluding `request_id` uuids, which must come from a seeded generator in
  replay mode — assert equality with the seeded ids).

---

## 13. Acceptance criteria

- [ ] Every `TradeIntent` produces exactly one `OrderRequest` **xor** one
      `IntentRejection`, journaled, with p99 decision latency ≤ 5 ms.
- [ ] All 13 pre-trade checks implemented in the §6 order with short-circuit;
      rejection `detail` always contains observed value and limit.
- [ ] Sizing reproduces §5.4 exactly (T1 numbers to stated tolerances);
      quantities always integer multiples of `lot_size`, rounded down;
      sub-minimum sizes rejected with `R_MIN_SIZE`.
- [ ] Position flips impossible at order level (reduce clamp); flip fills
      handled by close/open split with anomaly escalation (T20).
- [ ] Ledger matches T18/T19 to the cent; fees tracked separately from
      `avg_price`; `day_pnl_net = realized − fees + unrealized`.
- [ ] Marking: long@bid / short@ask, mid fallback, stale-mark freeze and
      `marks_stale` flag per §4.4 (T22).
- [ ] Token buckets at configured rates; only approved intents consume;
      safety orders exempt (T15–T17, T30).
- [ ] Kill-switch machine: 4 states, all §8.2 triggers, escalation-only,
      cancel-all → flatten → retry sequence, `HALTED_FLAT` on confirmed flat
      (T23–T29); every transition emits a `KillSwitchEvent`.
- [ ] Re-arm is manual only, via control socket, refused while flattening and
      after a hard daily-loss breach; never resets loss counters (T28).
- [ ] No broker/API calls and no signal-feature logic anywhere in the block;
      all order traffic goes to Block 6 topics only (code review + import
      linting: no adapter imports).
- [ ] Config validation rejects every out-of-range value in §10; reload is
      tighten-immediately / loosen-next-session (§11 row 10).
- [ ] Error-handling table §11 fully implemented (unknown/duplicate/late/
      out-of-order fills, queue overflow trips, internal-error trip).
- [ ] Crash restart rebuilds ledger, dedupe state, and kill state from the
      journal before processing live input; replay test T31 passes.
- [ ] Full test plan §12 (T1–T31) implemented and green.
