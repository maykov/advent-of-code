# Block 6 — Execution Engine: Implementation Specification

Conforms to `docs/design.md`. Python 3.11, asyncio, single process. All prices are
`float` at instrument tick precision; all quantities are `int` shares/contracts; all
timestamps are `int` nanoseconds since Unix epoch (field suffix `ts` / `_ts`).

---

## 1. Overview and responsibilities

The Execution Engine is the **only** component that sends orders to the broker
(design cross-cutting rule 2, "single-writer"). It:

1. Consumes `OrderRequest` messages from Risk (Block 5) and `FlattenCommand` /
   `KillSwitch` events from Blocks 5/7, and turns each into one or more **child
   orders** at the broker via an `ExecutionAdapter`.
2. Owns the **order lifecycle state machine** (§2) for every child order, idempotently,
   tolerating duplicate / out-of-order / lost broker messages.
3. Implements **execution tactics** (§3): PASSIVE_JOIN, CROSS_AFTER_TIMEOUT,
   SLIPPAGE_CAPPED_MARKET, FLATTEN_AGGRESSIVE.
4. **Reconciles** local order/position state with the broker on a fixed cadence (§5).
5. Emits `Fill`, `OrderState`, and `ExecutionQuality` messages to Blocks 5 and 9 (§6).

### 1.1 What it does NOT do

- **No sizing.** `OrderRequest.qty` is executed as given. The engine never scales,
  splits across intents, or nets quantities against other intents. (Splitting one
  request into sequential child orders for the *same* qty during escalation is
  allowed and is not sizing.)
- **No signal logic.** The engine never decides *whether* to trade, only *how* to
  work an already-approved order.
- **No risk checks.** Position caps, loss limits, price collars are Block 5's job.
  The engine applies only mechanical safety clamps that are part of a tactic's
  definition (slippage caps, tick rounding).
- **No market-data ingestion.** It consumes `BookState` from Block 2 read-only; it
  never talks to market-data APIs.

### 1.2 Fail-safe direction

On any unrecoverable internal error, adapter fatal error, or loss of the Risk
Manager heartbeat for more than `risk_heartbeat_timeout_s`: cancel all resting
orders, stop accepting new `OrderRequest`s (reject with reason `ENGINE_HALTED`),
keep the broker event stream and reconciliation running, and emit a critical alert
to Block 9. Flattening positions after such a halt is triggered by Block 5/7 via
`FlattenCommand`/`KillSwitch`, which the engine must keep honoring even when halted.

---

## 2. Order lifecycle state machine

### 2.1 Identifiers

- **`intent_id: str`** — from `OrderRequest`, groups all child orders of one request.
- **`client_order_id: str`** — engine-generated, deterministic, unique per child
  order attempt, ≤ 32 chars, charset `[A-Za-z0-9-]`. Scheme:

  ```
  client_order_id = f"{run_id}-{intent_seq:06d}-{child_seq:02d}-{attempt:02d}"
  ```

  - `run_id`: 8-char id fixed at engine start, recorded in config/journal
    (e.g. `20260702` + 0-padded restart counter is acceptable: `260702A0`).
  - `intent_seq`: engine-local monotonic counter assigned on first sight of an
    `intent_id` (mapping journaled so replay is deterministic).
  - `child_seq`: 0-based index of the child order within the request (0 for the
    passive order, 1 for the crossing order after escalation, etc.).
  - `attempt`: 0-based resubmission counter. **A `client_order_id` is never reused**,
    including after UNKNOWN resolution; a resubmit always increments `attempt`.
- **`order_id`** in outbound `Fill`/`OrderState` messages **is the
  `client_order_id`** (stable even if the broker never acked). The broker's own id,
  when known, is stored as `broker_order_id` for adapter calls that need it.
- **`exec_id: str`** — broker-unique id per fill report; used for fill idempotency.

### 2.2 States

`OrderStatus = {PENDING_NEW, ACKED, PARTIALLY_FILLED, FILLED, PENDING_CANCEL,
CANCELLED, REJECTED, EXPIRED, UNKNOWN}`

Terminal states: `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`.

Each state has a **rank** used for out-of-order handling:

| rank | states |
|---|---|
| 0 | PENDING_NEW |
| 1 | ACKED |
| 2 | PARTIALLY_FILLED |
| 3 | PENDING_CANCEL, UNKNOWN |
| 4 | FILLED, CANCELLED, REJECTED, EXPIRED |

### 2.3 Legal transitions

| From | To | Trigger |
|---|---|---|
| — | PENDING_NEW | `submit_order()` invoked |
| PENDING_NEW | ACKED | broker ack for this `client_order_id` |
| PENDING_NEW | REJECTED | broker reject |
| PENDING_NEW | PARTIALLY_FILLED | fill report arrives before ack (out-of-order); implies ack |
| PENDING_NEW | FILLED | full fill before ack |
| PENDING_NEW | UNKNOWN | `submit_order()` raised `AmbiguousResultError` (timeout / disconnect mid-call) |
| ACKED | PARTIALLY_FILLED | fill with `cum_qty < qty` |
| ACKED | FILLED | fill(s) reach `cum_qty == qty` |
| ACKED | PENDING_CANCEL | `cancel_order()` invoked |
| ACKED | CANCELLED | unsolicited broker cancel (e.g. venue purge) |
| ACKED | EXPIRED | broker expiry (TIF elapsed, session end) |
| ACKED | REJECTED | late post-ack reject (some venues); treat like cancel with reason |
| PARTIALLY_FILLED | PARTIALLY_FILLED | further partial fill |
| PARTIALLY_FILLED | FILLED | final fill |
| PARTIALLY_FILLED | PENDING_CANCEL | `cancel_order()` invoked |
| PARTIALLY_FILLED | CANCELLED / EXPIRED | broker cancel/expiry of remainder |
| PENDING_CANCEL | CANCELLED | broker cancel ack |
| PENDING_CANCEL | FILLED | fill won the race; cancel reject may follow (drop it) |
| PENDING_CANCEL | PENDING_CANCEL | partial fill while cancel in flight (record fill, stay) |
| PENDING_CANCEL | ACKED / PARTIALLY_FILLED | cancel **rejected** with "too late / unknown cancel" and order still live → revert to prior open state |
| PENDING_CANCEL | UNKNOWN | `cancel_order()` ambiguous AND subsequent status query fails (§9 row 3) |
| UNKNOWN | ACKED / PARTIALLY_FILLED / FILLED / CANCELLED / REJECTED / EXPIRED | resolution protocol outcome (§2.5) |

Any transition not listed is **illegal**: log at ERROR with both states and the
triggering message, do not apply the state change, but **always apply the fill
contents** (§2.4), and schedule an immediate reconciliation pass (§5).

Special case — *fill after terminal CANCELLED*: a fill report for a CANCELLED order
whose `exec_id` is new is a broker race, not corruption. Apply the fill, re-emit
`OrderState` (state `FILLED` if now `cum_qty == qty`, else keep `CANCELLED` with
updated `filled_qty`), and emit a `ReconcileMismatch{kind="LATE_FILL"}` (§5.3).

### 2.4 Idempotency and out-of-order rules

Broker event handling, in order, for each inbound event keyed by `client_order_id`:

1. **Unknown `client_order_id`** (not in the live-order table): if it parses as one
   of ours from this `run_id`, log ERROR and trigger reconciliation; otherwise it
   belongs to another session — emit `ReconcileMismatch{kind="FOREIGN_ORDER"}` and,
   if it is open, cancel it (fail-safe: no unmanaged orders).
2. **Fills are applied by `exec_id`.** Maintain `seen_exec_ids: set[str]` per order.
   Duplicate `exec_id` → drop silently (count metric `dup_fills`). New `exec_id` →
   append fill, update `filled_qty`, `avg_fill_price`, emit `Fill`. Fills are applied
   even when the state transition they imply is illegal or the state is terminal.
3. **Status messages regress only via explicit rules.** If the event implies a state
   with rank lower than the current rank and no rule in §2.3 permits it (the only
   permitted regressions are PENDING_CANCEL→ACKED/PARTIALLY_FILLED on cancel-reject,
   and UNKNOWN→anything), drop it as stale/duplicate (metric `stale_status`).
4. **`cum_qty` monotonicity.** A status carrying `cum_qty` lower than local
   `filled_qty` is stale → drop. Higher than local → fills were lost; synthesize a
   correcting `Fill` with `exec_id=f"{client_order_id}-recon-{n}"`, qty = difference,
   price = broker-reported `avg_price` back-out (if unavailable, current arrival mid;
   flag `synthetic=True`), and emit `ReconcileMismatch{kind="CUM_QTY_GAP"}`.

Every emitted `OrderState` and `Fill` is journaled before being published (rule 1,
determinism).

### 2.5 UNKNOWN resolution protocol

Entered when `submit_order` (or `cancel_order`, see §9) ends ambiguously.

1. Set state UNKNOWN, emit `OrderState{state=UNKNOWN, reason="SUBMIT_TIMEOUT"}`.
   Risk must treat UNKNOWN as full exposure (worst case: fully filled).
2. Query the broker: `get_order(client_order_id)` (fall back to scanning
   `get_open_orders()` + recent executions if the adapter lacks point lookup).
   Up to `unknown_probe_attempts` (default 3) tries, backoff per §8 retry policy.
3. If found → adopt the broker's state and cum qty via §2.4 rules.
4. If not found after all probes → send `cancel_order(client_order_id)` anyway
   ("cancel the ghost": harmless if the order never reached the broker), wait
   `unknown_grace_ms` (default 2000) for any event, re-probe once.
5. Still nothing → transition UNKNOWN → REJECTED with reason
   `UNKNOWN_RESOLVED_ABSENT`. The `client_order_id` is retired forever. If the
   owning tactic still needs the quantity, it submits a **new** child order with
   `attempt+1`.
6. Every UNKNOWN entry/exit triggers an immediate reconciliation pass (§5).

---

## 3. Execution tactics

Each `OrderRequest` is handled by exactly one **tactic instance** that owns the
request until every child order is terminal. Tactic instances for the same
instrument run strictly sequentially in that instrument's executor task (§7.3);
tactic decisions read the instrument's **latest** `BookState` (a last-value cell,
never a queue).

Common definitions:

- `arrival: BookState` — latest book state at the moment the `OrderRequest` is
  dequeued by the instrument executor. `arrival_mid = arrival.mid`. If no book state
  is available or `FeedStatus` for the instrument is not LIVE, reject the request
  with `OrderState{state=REJECTED, reason="NO_MARKET_DATA"}` — **except** for
  FLATTEN_AGGRESSIVE, which proceeds using the last known book (fail-safe).
- `tick` — instrument tick size from reference data.
- `round_passive(p, side)` — BUY: floor to tick; SELL: ceil to tick.
- `cap_price(side, mid, cap_bps)`:
  - BUY: `round_passive(mid * (1 + cap_bps / 10_000), BUY)`  (floor to tick)
  - SELL: `round_passive(mid * (1 - cap_bps / 10_000), SELL)` (ceil to tick)
- "Marketable limit at price P" = limit order, TIF `IOC`, limit price P.

### 3.1 Tactic selection rule

Evaluated in order; first match wins:

| # | Condition | Tactic |
|---|---|---|
| 1 | Source is `KillSwitch`, or `FlattenCommand{mode=AGGRESSIVE}` | FLATTEN_AGGRESSIVE |
| 2 | Source is `FlattenCommand{mode=PASSIVE}` | PASSIVE_JOIN per position (qty = position to flatten, from the accompanying Risk `OrderRequest`s), with escalation deadline `min(deadline_ts, arrival + passive_timeout_ms)` |
| 3 | `OrderRequest.type == MARKET` | SLIPPAGE_CAPPED_MARKET |
| 4 | `OrderRequest.type == LIMIT` and `time_in_force == IOC` | single marketable limit at `min(limit_price, cap_price(...))` for BUY / `max(...)` for SELL, no retry |
| 5 | `OrderRequest.type == LIMIT` (TIF `DAY`) | PASSIVE_JOIN → CROSS_AFTER_TIMEOUT |

For rows 3–5, if `OrderRequest.limit_price` is set it acts as a **hard bound** the
tactic must never cross (BUY: never pay above it; SELL: never sell below it), even
where the slippage cap alone would allow it.

### 3.2 PASSIVE_JOIN

Goal: earn the spread / avoid taker fees; escalate if unfilled.

Parameters: `passive_timeout_ms` (T), `reprice_min_interval_ms`,
`reprice_max_count`, `passive_cap_bps`.

Algorithm (BUY; SELL is mirrored):

1. On start, read `arrival`. Compute `join_px = arrival.best_bid`
   (= `arrival.bids[0].price`). Clamp: `join_px = min(join_px,
   cap_price(BUY, arrival_mid, passive_cap_bps), request.limit_price or +inf)`.
   If `join_px >= arrival.best_ask`, do not cross passively: set
   `join_px = arrival.best_ask - tick`.
2. Submit child order `child_seq=0`: LIMIT DAY, `qty = remaining`, price `join_px`.
   Record `deadline_ts = arrival.ts + T * 1e6`.
3. Wait on: broker events for the child, book-state updates, timer.
4. **Reprice condition** — on each book update, reprice iff ALL of:
   a. current `best_bid > our_price` (someone bid better; note our own resting order
      is part of the book, so `best_bid < our_price` is impossible while we rest), and
   b. at least `reprice_min_interval_ms` since the last submit/replace, and
   c. `reprice_count < reprice_max_count`, and
   d. new price `min(best_bid, cap, request.limit_price or +inf) > our_price`
      (the clamp still allows improvement).
   Reprice = cancel current child (→ PENDING_CANCEL), await terminal state, then
   submit a new child (`child_seq` unchanged, `attempt+1`) for the *remaining*
   unfilled qty at the new clamped join price, still under the original `deadline_ts`.
   If the cancel resolves as FILLED, stop — done.
5. **Timeout**: when `now >= deadline_ts` and `remaining > 0`: cancel the resting
   child, await terminal, then run CROSS_AFTER_TIMEOUT (§3.3) for `remaining` with
   `child_seq=1`.
6. Exit when `remaining == 0` (emit `ExecutionQuality`, §6.4) or when a child is
   REJECTED with a fatal reason (emit `ExecutionQuality` for any partial fills and
   `OrderState` REJECTED for the remainder; Risk decides what to do next).

### 3.3 CROSS_AFTER_TIMEOUT

Goal: take liquidity now, but never worse than the cap.

Parameters: `cross_cap_bps`, `cross_max_attempts`, `cross_reattempt_delay_ms`.

Algorithm (BUY):

1. Read the current book `b`. Compute
   `px = min(cap_price(BUY, arrival_mid, cross_cap_bps), request.limit_price or +inf)`.
   **The cap is anchored to the original request's `arrival_mid`**, not the current
   mid — this is the total-slippage budget for the intent.
2. If `b.best_ask > px` (the cap can't reach the touch): do not send. Wait
   `cross_reattempt_delay_ms`, re-read the book, retry step 1–2 up to
   `cross_max_attempts` total. If still unreachable, emit
   `OrderState{state=EXPIRED, reason="CAP_UNREACHABLE"}` for the remaining qty and
   finish (Risk is informed via that message and the `ExecutionQuality` record).
3. Submit marketable limit: LIMIT **IOC**, qty = remaining, price `px`.
4. On terminal state: if `remaining > 0` (IOC partially filled or expired unfilled
   because liquidity vanished), go to step 1 (counts as one attempt).
5. Finish when `remaining == 0` or attempts exhausted (then emit EXPIRED /
   `CAP_UNREACHABLE` as in step 2).

### 3.4 SLIPPAGE_CAPPED_MARKET

Identical to CROSS_AFTER_TIMEOUT except: it starts immediately at request arrival
(`child_seq=0`), uses `market_cap_bps`, and `arrival_mid` is captured at request
dequeue. A `MARKET` request is thus never sent as a true market order — always a
marketable limit at `cap_price(side, arrival_mid, market_cap_bps)`.

### 3.5 FLATTEN_AGGRESSIVE (kill-switch / EOD)

Triggered by `KillSwitch` or `FlattenCommand{mode=AGGRESSIVE}`. Preempts everything:
running tactics are aborted (their resting orders are included in the cancel-all),
and the input `OrderRequest` queue is drained with `REJECTED reason="FLATTENING"`
(kill-switch) or held (EOD flatten, resumed only by Block 7).

Parameters: `flatten_cap_bps_initial`, `flatten_cap_bps_step`,
`flatten_cycle_ms_initial`, `flatten_cycle_backoff`, `flatten_cycle_ms_max`,
`flatten_alert_after_cycles`.

Algorithm:

1. **Cancel-all**: for every locally open order (rank < 4), call `cancel_order`.
   In parallel, call `adapter.get_open_orders()` and cancel anything there that is
   ours-but-unknown or foreign. Wait until no local order is non-terminal or
   `flatten_cancel_grace_ms` (default 3000) elapsed (leftovers become UNKNOWN and
   run §2.5 concurrently).
2. **Position snapshot**: `positions = adapter.get_positions()` (broker truth, not
   the local ledger — fail-safe). Retry per §8 on retryable errors; on repeated
   failure use Risk's last `PositionState` and set `degraded=True` in the alert.
3. `cycle = 0`. While any position ≠ 0:
   a. For each instrument with `pos != 0`: side = SELL if `pos > 0` else BUY,
      qty = `abs(pos)`, `cap = flatten_cap_bps_initial + cycle * flatten_cap_bps_step`,
      price = `cap_price(side, current_mid, cap)` using the **current** mid (each
      cycle re-anchors — getting flat dominates slippage). Submit marketable limit
      IOC. No `limit_price` bound applies.
   b. Await terminal states of all submitted orders (per-cycle timeout =
      current cycle delay).
   c. Re-snapshot positions from the broker.
   d. `cycle += 1`; sleep
      `min(flatten_cycle_ms_initial * flatten_cycle_backoff**cycle, flatten_cycle_ms_max)`.
   e. If `cycle >= flatten_alert_after_cycles`, emit a CRITICAL alert to Block 9
      each cycle (`EodReport` residuals are Block 7's to publish; the engine feeds
      it the reconciliation snapshot).
4. Loop has **no attempt limit**; it ends only when broker-reported positions are
   all zero or the process is stopped. On becoming flat, emit a final
   reconciliation report (§5.3) with `flat_confirmed=True` to Blocks 5, 7, 9.

---

## 4. ExecutionAdapter interface and paper-trading adapter

### 4.1 Error taxonomy

```python
class AdapterError(Exception):
    """Base. .retryable: bool, .code: str, .detail: str"""

class RetryableAdapterError(AdapterError):
    """Transient: network timeout on idempotent read, HTTP 5xx, disconnect,
    rate limit. .retry_after_s: float | None (set for rate limits)."""

class FatalAdapterError(AdapterError):
    """Permanent: auth failure, unknown instrument, malformed order,
    permission denied. Never retried; order (if any) -> REJECTED."""

class AmbiguousResultError(AdapterError):
    """A state-changing call (submit/cancel) timed out or the connection
    dropped mid-call: the broker MAY have processed it. Triggers §2.5."""
```

Mapping guidance for implementers: HTTP 408/timeout on submit → Ambiguous;
429 → Retryable with `retry_after_s`; 4xx other than 408/429 → Fatal;
5xx on submit → Ambiguous (the request may have been applied); 5xx on reads →
Retryable; websocket closed → Retryable for the stream, Ambiguous for any call
in flight.

### 4.2 Interface

Every broker implementation (paper and real) satisfies:

```python
class BrokerEventType(str, Enum):
    ACK = "ACK"; REJECT = "REJECT"; FILL = "FILL"
    CANCELLED = "CANCELLED"; CANCEL_REJECT = "CANCEL_REJECT"
    EXPIRED = "EXPIRED"; STATUS = "STATUS"       # STATUS: snapshot w/ cum_qty
    STREAM_UP = "STREAM_UP"; STREAM_DOWN = "STREAM_DOWN"

@dataclass(frozen=True, slots=True)
class BrokerEvent:
    type: BrokerEventType
    client_order_id: str | None      # None only for STREAM_UP/STREAM_DOWN
    broker_order_id: str | None
    ts: int                          # broker/exchange ts, ns
    ts_local: int                    # local receive ts, ns
    exec_id: str | None = None       # FILL only
    fill_qty: int | None = None      # FILL only
    fill_price: float | None = None  # FILL only
    fee: float | None = None         # FILL only
    liquidity: Literal["MAKER", "TAKER"] | None = None
    cum_qty: int | None = None       # STATUS/FILL if broker provides
    state: str | None = None         # STATUS only, broker state string
    reason: str | None = None        # REJECT/CANCEL_REJECT/EXPIRED code

@dataclass(frozen=True, slots=True)
class BrokerOrder:
    client_order_id: str
    instrument: str
    side: Literal["BUY", "SELL"]
    qty: int
    order_type: Literal["LIMIT"]     # engine only ever sends limits (§3.4)
    limit_price: float
    time_in_force: Literal["DAY", "IOC"]

@dataclass(frozen=True, slots=True)
class BrokerPosition:
    instrument: str
    qty: int                          # signed
    avg_price: float

@dataclass(frozen=True, slots=True)
class BrokerOpenOrder:
    client_order_id: str
    broker_order_id: str
    instrument: str
    side: str
    qty: int
    cum_qty: int
    limit_price: float
    state: str

class ExecutionAdapter(Protocol):
    async def connect(self) -> None:
        """Open sessions. Raises FatalAdapterError on auth/config failure.
        Must be idempotent (safe to call after a drop)."""

    async def close(self) -> None:
        """Graceful shutdown. Never raises."""

    async def submit_order(self, order: BrokerOrder, *, timeout_s: float) -> None:
        """Transmit a new order. Returns when the broker has ACCEPTED THE
        MESSAGE (transport-level), not when acked — acks arrive on stream_events.
        Raises FatalAdapterError (definitely not placed),
        AmbiguousResultError (timeout_s elapsed / connection dropped: may be
        placed). MUST be idempotent on client_order_id at the broker where the
        venue supports it; if not, the engine's never-reuse rule (§2.1) is the
        only guard and the adapter must document this."""

    async def cancel_order(self, client_order_id: str, instrument: str,
                           *, timeout_s: float) -> None:
        """Request cancel. Same return/raise semantics as submit_order.
        Cancelling an unknown/terminal order must surface as a CANCEL_REJECT
        event, not an exception."""

    async def get_order(self, client_order_id: str,
                        *, timeout_s: float) -> BrokerOpenOrder | None:
        """Point lookup incl. terminal orders of the current session; None if
        the broker has never seen the id. Raises RetryableAdapterError only."""

    async def get_open_orders(self, *, timeout_s: float) -> list[BrokerOpenOrder]:
        """All live orders on the account. Raises RetryableAdapterError only."""

    async def get_positions(self, *, timeout_s: float) -> list[BrokerPosition]:
        """All nonzero positions. Raises RetryableAdapterError only."""

    def stream_events(self) -> AsyncIterator[BrokerEvent]:
        """Broker push events. On disconnect, yields STREAM_DOWN, reconnects
        internally with its own backoff, yields STREAM_UP, then replays any
        missed events if the venue supports it (else the engine's
        reconciliation covers the gap, see §9 row 4). Never terminates except
        after close()."""
```

Timeout behavior: every request/response method takes an explicit `timeout_s`
(engine passes values from config §8). The adapter must enforce it and raise the
taxonomy error — it must never hang past `timeout_s + 1s`.

### 4.3 Paper-trading adapter (`PaperExecutionAdapter`)

Implements `ExecutionAdapter` against live `BookState`/`TradePrint` streams
(paper stage of the promotion pipeline) or replayed ones (inside Block 8). It is
deliberately **pessimistic** so paper results under-promise.

Constructor:

```python
class PaperExecutionAdapter(ExecutionAdapter):
    def __init__(self, market: MarketView, cfg: PaperConfig, clock: Clock): ...
```

`MarketView` provides `latest_book(instrument) -> BookState` and an async stream of
`BookState` and `TradePrint`. `Clock` is the engine's injected clock (wall in live,
simulated in replay — determinism rule 1).

#### 4.3.1 Simulated latency

- `submit_latency_ms` (default 20): delay between `submit_order()` returning and the
  order becoming active in the simulator; the ACK event carries
  `ts = receive_ts + submit_latency_ms`.
- `cancel_latency_ms` (default 20): same for cancels. Fills that the model produces
  *during* the cancel latency window still happen (models the cancel race).
- Latencies are constants (not random) for replay determinism.

#### 4.3.2 Fill model — marketable orders (order is executable on arrival)

An arriving BUY order with `limit_price >= best_ask` (mirror for SELL) **walks the
book** at activation time:

1. Take the latest `BookState` for the instrument at activation (`receive + latency`).
2. Iterate ask levels `(p_i, s_i)` from best upward while `p_i <= limit_price` and
   `remaining > 0`: fill `min(remaining, s_i)` at price `p_i`; one `FILL` event per
   level, `exec_id = f"paper-{n}"` (global counter), `liquidity="TAKER"`.
3. If `remaining > 0` after the walk:
   - TIF `IOC`: emit `EXPIRED` for the remainder (cum_qty preserved).
   - TIF `DAY`: the remainder **rests** at `limit_price` and follows §4.3.3, with
     `queue_ahead = 0` (we just cleared that price).
4. No self-impact model beyond consuming displayed size for this order only
   (the real book stream refreshes state; conservative enough for v1 sizes —
   Block 8's cost model adds impact separately).

#### 4.3.3 Fill model — passive resting orders

A resting BUY at price `P` (mirror for SELL) is filled by **trade prints** and by
the market crossing it:

- **Queue position**: at rest time, `queue_ahead = displayed bid size at P` in the
  latest `BookState` (if `P` is not a displayed level, `queue_ahead = 0`).
  Pessimistic: we assume we join the back of the entire displayed queue, and
  `queue_ahead` is **never reduced by cancellations** — only by trades.
- On each subsequent `TradePrint{price=tp, size=ts_, aggressor_side}` for the
  instrument:
  - If `tp < P` (trade **through** our price): the market traded below our bid, so
    our bid must have been consumed first — fill `min(remaining, ts_)` at **our**
    price `P` (not `tp`), and set `queue_ahead = 0`.
  - Else if `tp == P` and `aggressor_side == SELL` (a seller hit the bid at our
    level): `consumed = min(ts_, queue_ahead)`; `queue_ahead -= consumed`;
    `overflow = ts_ - consumed`; fill `min(remaining, overflow)` at `P`.
  - Else (trade above `P`, or `tp == P` with aggressor BUY): no effect.
- On each `BookState` where `best_ask <= P` (book crossed our resting bid, e.g. a
  gap down through us): fill the remainder by walking asks from `best_ask` up to
  `P` per §4.3.2 step 2 at those ask prices (better-or-equal to `P`), TAKER-priced
  but MAKER-flagged fee (we were resting).
- All passive fills carry `liquidity="MAKER"`.
- Partial fills emit one `FILL` per event that produced quantity; `cum_qty` is
  included on every FILL.

Cancels: effective after `cancel_latency_ms`; any fills produced before the
effective instant stand; then `CANCELLED` for the remainder (or `CANCEL_REJECT`
with reason `"TOO_LATE"` if already fully filled).

DAY orders receive `EXPIRED` at session close (from the `SessionPhase` feed).

#### 4.3.4 Fee model

```
fee = qty * price * fee_bps / 10_000 + qty * fee_per_share
```

with `taker_fee_bps` (default 1.0), `maker_fee_bps` (default 0.0; may be negative
for rebates, allowed range −1.0..5.0), `fee_per_share` (default 0.0),
`min_fee_per_order` (default 0.0, applied to the sum of an order's fills at
terminal state as an adjustment on the last fill). Fees are reported on each FILL.

#### 4.3.5 Paper reconciliation endpoints

`get_positions` / `get_open_orders` / `get_order` answer from the simulator's own
ledger (which is authoritative for paper). `PaperConfig.chaos` (default off) can
inject: dropped acks, duplicate fills, delayed cancels — used by the test plan (§10)
to exercise §2.4/§2.5.

---

## 5. Reconciliation

### 5.1 Cadence

- Every `reconcile_interval_s` (default 30) during phases PRE_OPEN..WIND_DOWN.
- Every `reconcile_interval_flat_s` (default 5) during FORCE_FLAT and while a
  FLATTEN_AGGRESSIVE loop is running.
- Immediately (out of band) after: any UNKNOWN entry or resolution, `STREAM_UP`
  after a `STREAM_DOWN`, any illegal-transition log, and engine start.
- Runs as its own asyncio task; never blocks order flow. Skips a round if the
  previous round is still in flight (metric `recon_skipped`).

### 5.2 Comparison rules

One round:

1. `broker_orders = await adapter.get_open_orders()`;
   `broker_pos = await adapter.get_positions()` (retry per §8; if both calls fail
   the round is aborted and an alert is raised after 3 consecutive failed rounds).
2. **Open orders** — compare against local orders with rank < 4:
   - Local open, absent at broker → the order died silently: mark UNKNOWN, run §2.5.
     (Grace: skip orders in PENDING_NEW younger than `submit_timeout_ms`.)
   - Broker open, absent locally → emit mismatch `kind="FOREIGN_ORDER"` and cancel
     it (fail-safe; includes orphans from a crashed previous run).
   - Both present but `broker.cum_qty > local.filled_qty` → synthesize fill per
     §2.4 rule 4, mismatch `kind="CUM_QTY_GAP"`.
   - Both present, broker state terminal-equivalent while local open → apply the
     terminal transition, mismatch `kind="STATE_DRIFT"`.
3. **Positions** — compare `broker_pos` to the engine's own fill-derived position
   counter per instrument (the engine keeps a shadow position from its emitted
   `Fill`s purely for this check; the *authoritative* ledger is Block 5's):
   - Any difference in signed qty (tolerance: exactly 0 shares) → mismatch
     `kind="POSITION_DRIFT"` with both values. The engine **adopts the broker
     number into its shadow counter** after emitting; it never trades to "fix"
     drift on its own — Risk decides (it may issue corrective `OrderRequest`s).

### 5.3 Emission on mismatch

```python
@dataclass(frozen=True, slots=True)
class ReconcileMismatch:
    ts: int
    kind: Literal["FOREIGN_ORDER", "CUM_QTY_GAP", "STATE_DRIFT",
                  "POSITION_DRIFT", "LATE_FILL"]
    instrument: str
    client_order_id: str | None
    local: str          # JSON of local view
    broker: str         # JSON of broker view
    action_taken: str   # e.g. "CANCELLED_FOREIGN", "SYNTH_FILL", "ADOPTED_BROKER_QTY"

@dataclass(frozen=True, slots=True)
class ReconcileReport:
    ts: int
    mismatches: tuple[ReconcileMismatch, ...]   # empty tuple == clean round
    open_orders_local: int
    open_orders_broker: int
    positions_broker: tuple[BrokerPosition, ...]
    flat_confirmed: bool                        # all broker positions zero
```

Every `ReconcileMismatch` is published individually on the bus **to Block 5 (Risk)
and Block 9** the moment it is detected; the `ReconcileReport` is published to
Blocks 5, 7 (it feeds the EOD flat verification), and 9 at the end of each round.
POSITION_DRIFT and FOREIGN_ORDER are WARN-level alerts; two consecutive rounds with
the same POSITION_DRIFT is CRITICAL.

---

## 6. Data structures (bus messages)

All dataclasses `@dataclass(frozen=True, slots=True)`. Fields required by
`docs/design.md` come first; extra fields are additive and optional for consumers.

### 6.1 Fill

```python
@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str          # == client_order_id
    intent_id: str
    instrument: str
    qty: int               # this fill's quantity, > 0
    price: float
    fee: float             # signed; negative = rebate
    ts: int                # exchange/broker fill ts, ns
    side: Literal["BUY", "SELL"]
    exec_id: str
    liquidity: Literal["MAKER", "TAKER"]
    synthetic: bool = False    # True for reconciliation-synthesized fills
    ts_local: int = 0
```

### 6.2 OrderState

```python
class OrderStatus(str, Enum):
    PENDING_NEW = "PENDING_NEW"; ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"; FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"; CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"; EXPIRED = "EXPIRED"; UNKNOWN = "UNKNOWN"

@dataclass(frozen=True, slots=True)
class OrderState:
    order_id: str
    state: OrderStatus
    reason: str | None     # reject/cancel/expiry code, else None
    intent_id: str
    instrument: str
    side: Literal["BUY", "SELL"]
    qty: int               # order quantity
    filled_qty: int
    remaining_qty: int     # qty - filled_qty (0 if terminal non-filled remainder void)
    avg_fill_price: float | None
    limit_price: float | None
    tactic: str            # Tactic enum value
    ts: int
```

Emitted on **every** state change and on every fill application.

### 6.3 ExecutionQuality

Emitted once per `intent_id` when its tactic instance finishes (all children
terminal), even if nothing filled.

```python
@dataclass(frozen=True, slots=True)
class ExecutionQuality:
    intent_id: str
    arrival_mid: float         # mid at OrderRequest dequeue (§3)
    avg_fill_price: float | None   # qty-weighted across all fills; None if no fill
    slippage_bps: float | None
    latency_ms: float          # (last fill ts_local - request dequeue ts) / 1e6;
                               # if no fill: (finish ts - dequeue ts) / 1e6
    instrument: str
    side: Literal["BUY", "SELL"]
    requested_qty: int
    filled_qty: int
    fees: float
    n_child_orders: int
    tactic: str
    ts: int
```

### 6.4 slippage_bps formula

```
sign = +1 if side == BUY else -1
slippage_bps = sign * (avg_fill_price - arrival_mid) / arrival_mid * 10_000
```

Positive = execution cost (paid up as buyer / sold down as seller); negative =
price improvement. `None` if `filled_qty == 0`. `arrival_mid` is always the mid of
the `BookState` current at request dequeue — reprices and escalations do **not**
re-anchor it (FLATTEN_AGGRESSIVE cycles are the sole exception for *pricing*, §3.5,
but their quality records still report against each flatten-request's own arrival mid).

---

## 7. Public API and asyncio model

### 7.1 Classes

```python
class ExecutionEngine:
    def __init__(
        self,
        adapter: ExecutionAdapter,
        bus: EventBus,                 # subscribe/publish typed topics
        cfg: ExecutionConfig,          # §8
        clock: Clock,                  # .now_ns() -> int; injectable for replay
        journal: Journal,              # append(msg) before publish
        instruments: RefData,          # tick size, lot size per instrument
    ) -> None: ...

    async def start(self) -> None:
        """connect adapter -> initial reconciliation (cancels orphans, §5.2)
        -> spawn tasks -> subscribe bus topics. Idempotent."""

    async def stop(self) -> None:
        """Cancel all resting orders, drain event stream for
        cfg.stop_drain_ms, final reconcile round, adapter.close().
        Does NOT flatten positions (that is Block 7's command to give)."""
```

Internal (unit-tested, not bus-facing):
`OrderLifecycle` (state machine of §2, pure, no I/O),
`TacticRunner` subclasses `PassiveJoin`, `CrossAfterTimeout`,
`SlippageCappedMarket`, `FlattenAggressive` (each `async def run(ctx) -> None`),
`InstrumentExecutor` (§7.3), `Reconciler` (§5).

### 7.2 Bus topics

Consumes: `order_requests: OrderRequest`, `flatten_commands: FlattenCommand`,
`kill_switch: KillSwitch`, `book_states: BookState`, `feed_status: FeedStatus`,
`session_phase: SessionPhase`.
Publishes: `fills: Fill`, `order_states: OrderState`,
`execution_quality: ExecutionQuality`, `reconcile: ReconcileMismatch | ReconcileReport`.

### 7.3 Task layout and ordering guarantees

- **One `InstrumentExecutor` task per instrument**, created lazily on first
  `OrderRequest` for that instrument. Each has a **bounded** input queue
  `asyncio.Queue[OrderRequest](maxsize=cfg.per_instrument_queue_max)` (default 8).
  Requests for one instrument are executed **strictly FIFO**: a tactic instance
  runs to completion before the next request is dequeued (v1: no concurrent working
  of two requests in one instrument; Risk's throttles make this acceptable).
  Queue full → immediately emit `OrderState{state=REJECTED, reason="QUEUE_FULL"}`
  (never block Risk, never drop silently).
- **Book states** go into a per-instrument **last-value cell** (`dict[str, BookState]`
  + per-instrument `asyncio.Event` for wakeups), never a queue — tactics always see
  the freshest book and can't fall behind.
- **One broker-event pump task** reads `adapter.stream_events()` and routes each
  event to the owning order's lifecycle (lookup by `client_order_id`), applying
  §2.4 rules; per-order application is synchronous within the pump, so events for
  one order are applied in arrival order.
- **One reconciler task** (§5). **One flatten controller** task slot: KillSwitch /
  AGGRESSIVE FlattenCommand sets a global `flattening` flag checked by every
  executor before submitting; the controller runs §3.5.
- All outbound messages: `journal.append()` then `bus.publish()` (in that order).
- Backpressure on publish: bus topics are bounded; `fills`/`order_states` publishes
  use `await` (block the pump rather than lose a fill) — sized so this never
  happens in practice (§8).

Shutdown ordering in `stop()`: stop dequeuing requests → cancel resting orders →
drain events `stop_drain_ms` → final reconcile → cancel tasks → `adapter.close()`.

---

## 8. Configuration

Complete example (`execution.toml`); loader validates every range and fails fast.

```toml
[execution]
run_id_prefix        = "260702"   # str, 6 chars, engine appends restart letter+digit
risk_heartbeat_timeout_s = 5.0    # float, 1.0..30.0, default 5.0
stop_drain_ms        = 2000       # int ms, 0..10000, default 2000
per_instrument_queue_max = 8      # int, 1..64, default 8
max_orders_per_sec   = 10.0       # float, global token bucket, 0.5..100, default 10

[execution.timeouts]              # all int ms unless noted
submit_timeout_ms    = 2000       # 200..10000, default 2000 (-> Ambiguous, §2.5)
cancel_timeout_ms    = 2000       # 200..10000, default 2000
query_timeout_ms     = 3000       # 500..15000, default 3000
unknown_probe_attempts = 3        # int, 1..10, default 3
unknown_grace_ms     = 2000       # 500..10000, default 2000

[execution.retry]                 # for RetryableAdapterError on reads/cancels
base_delay_ms        = 250        # 50..2000, default 250
multiplier           = 2.0        # 1.0..4.0, default 2.0
max_delay_ms         = 2000       # 500..30000, default 2000
max_attempts         = 5          # 1..10, default 5; then escalate (alert / §2.5)

[execution.tactics.passive_join]
passive_timeout_ms   = 2000       # T; 200..60000, default 2000
reprice_min_interval_ms = 150     # 50..5000, default 150
reprice_max_count    = 5          # 0..20, default 5
passive_cap_bps      = 5.0        # 0.0..100.0, default 5.0

[execution.tactics.cross]
cross_cap_bps        = 8.0        # 0.0..100.0, default 8.0 (anchored to arrival mid)
cross_max_attempts   = 3          # 1..10, default 3
cross_reattempt_delay_ms = 200    # 50..5000, default 200

[execution.tactics.market]
market_cap_bps       = 10.0       # 0.0..100.0, default 10.0

[execution.tactics.flatten]
flatten_cap_bps_initial = 20.0    # 5.0..200.0, default 20.0
flatten_cap_bps_step = 10.0       # 0.0..100.0, default 10.0 (per cycle)
flatten_cancel_grace_ms = 3000    # 500..10000, default 3000
flatten_cycle_ms_initial = 250    # 100..2000, default 250
flatten_cycle_backoff = 2.0       # 1.0..4.0, default 2.0
flatten_cycle_ms_max = 2000       # 500..10000, default 2000
flatten_alert_after_cycles = 10   # 1..100, default 10

[execution.reconcile]
reconcile_interval_s = 30.0       # 5.0..300.0, default 30.0
reconcile_interval_flat_s = 5.0   # 1.0..60.0, default 5.0

[execution.paper]                 # PaperExecutionAdapter only
submit_latency_ms    = 20         # 0..500, default 20
cancel_latency_ms    = 20         # 0..500, default 20
taker_fee_bps        = 1.0        # 0.0..20.0, default 1.0
maker_fee_bps        = 0.0        # -1.0..5.0, default 0.0
fee_per_share        = 0.0        # 0.0..0.05, default 0.0
min_fee_per_order    = 0.0        # 0.0..5.0, default 0.0
chaos                = false      # bool, default false (tests only)
```

Constraint checks at load: `passive_cap_bps <= cross_cap_bps <= market_cap_bps
<= flatten_cap_bps_initial`; `reprice_min_interval_ms < passive_timeout_ms`.

---

## 9. Error handling table

| # | Failure | Detection | Handling |
|---|---|---|---|
| 1 | **Broker timeout on new order** (state unknown) | `submit_order` raises `AmbiguousResultError` | UNKNOWN resolution protocol §2.5: mark UNKNOWN (Risk reserves worst-case exposure) → probe `get_order` ×`unknown_probe_attempts` → if absent, cancel-the-ghost, wait `unknown_grace_ms`, re-probe → adopt broker state or REJECTED `UNKNOWN_RESOLVED_ABSENT`. Never resubmit the same `client_order_id`. |
| 2 | **Broker reject** | REJECT event / FatalAdapterError | Map reason. Retryable reject codes (venue throttle, "too many requests"): resubmit with `attempt+1` per §8 retry policy, max `max_attempts`. Fatal codes (invalid price/size, no permission, halted instrument): `OrderState REJECTED` with broker reason; tactic ends for the remainder; no retry. Unknown codes are treated as **fatal** (fail-safe). |
| 3 | **Cancel race with fill** | Fill event while PENDING_CANCEL, and/or CANCEL_REJECT "too late" | Fills always win: apply per §2.4 rule 2. Full fill → FILLED; drop the subsequent CANCEL_REJECT. Partial fill then CANCELLED → CANCELLED with `filled_qty` recorded. CANCEL_REJECT while order still live per broker → revert PENDING_CANCEL → prior open state; tactic decides whether to re-cancel. Fill *after* CANCELLED → §2.3 special case (apply, LATE_FILL mismatch). |
| 4 | **Websocket drop mid-order** | STREAM_DOWN from adapter | Freeze escalations/reprices for orders on that stream (no new submits for affected instruments except FLATTEN_AGGRESSIVE). Calls in flight resolve as Ambiguous → §2.5 after reconnect. On STREAM_UP: immediate out-of-band reconciliation round (§5.1) to recover events missed during the gap; synthesized fills cover gaps (§2.4 rule 4). If down > `risk_heartbeat_timeout_s`, cancel all resting orders via REST path (fail-safe rule 3). |
| 5 | **Rate limit (HTTP 429 / venue throttle)** | RetryableAdapterError with `retry_after_s` | Pause the global token bucket for `retry_after_s` (or `base_delay_ms` backoff if unset); the triggering call retries per §8. Repeated 429 within 10 s → halve `max_orders_per_sec` for 60 s and alert Block 9. FLATTEN_AGGRESSIVE ignores the bucket but still honors `retry_after_s`. |
| 6 | **Cancel timeout** | `cancel_order` raises Ambiguous | Re-send cancel (idempotent) up to `max_attempts`, then `get_order` probe; if still ambiguous → UNKNOWN + §2.5. |
| 7 | **Reconciliation read failures** | 3 consecutive failed rounds | CRITICAL alert; engine keeps trading only if the event stream is up; if stream is also down → cancel-all + halt (§1.2). |
| 8 | **Illegal transition / corrupt event** | §2.3 catch-all | Log ERROR, apply fills only, immediate reconcile round. |

---

## 10. Test plan

Framework: `pytest` + `pytest-asyncio`; fake `Clock` (manual advance); scripted
`FakeAdapter` (records calls, emits scripted `BrokerEvent`s); `PaperExecutionAdapter`
driven by scripted `BookState`/`TradePrint` sequences. Numbers below are the exact
expected assertions.

**T1 — Happy path, full lifecycle (SLIPPAGE_CAPPED_MARKET).**
Book: bids `[100.00×400]`, asks `[100.02×300, 100.03×500]`, tick 0.01,
mid = 100.01. Request: BUY 600 MARKET, `market_cap_bps=2.0`.
Expect: cap price = floor(100.01 × 1.0002) = floor(100.030002) → **100.03**; one
child LIMIT IOC 600 @ 100.03; scripted events ACK → FILL 300 @ 100.02
(exec_id E1) → FILL 300 @ 100.03 (E2). Assert `OrderState` sequence
PENDING_NEW → ACKED → PARTIALLY_FILLED(filled 300) → FILLED(filled 600,
avg 100.025); two `Fill` messages; `ExecutionQuality{arrival_mid=100.01,
avg_fill_price=100.025, slippage_bps=+1.4999 (assert ≈1.50, rel tol 1e-6),
filled_qty=600, n_child_orders=1}`.

**T2 — Passive → escalation, tick by tick (fake clock, ms).**
Config: T=2000, `reprice_min_interval_ms=150`, `passive_cap_bps=5`,
`cross_cap_bps=8`. Request BUY 500 LIMIT DAY, no `limit_price` bound.
- t=0: book bid 100.00×400 / ask 100.02; arrival_mid=100.01. Child A
  (`...-00-00`) LIMIT DAY 500 @ 100.00 submitted; PENDING_NEW.
- t=10: ACK → ACKED.
- t=400: book update, best_bid 100.01 (someone bid better). Reprice fires
  (conditions a–d hold): cancel A → PENDING_CANCEL; t=420 CANCELLED (0 filled);
  child B (`...-00-01`) 500 @ 100.01 submitted, t=430 ACKED.
- t=900: FILL 200 @ 100.01 on B → PARTIALLY_FILLED.
- t=2000: deadline (anchored at t=0). Cancel B; t=2020 CANCELLED (filled 200).
- t=2025: escalation. Current book ask 100.02×250, 100.03×600. Cap price =
  floor(100.01 × 1.0008) = floor(100.090008) → 100.09; but assert engine sends
  child C (`...-01-00`) LIMIT IOC 300 @ **100.09**? No — assert price is
  `min(cap, ...)` = 100.09 with no request bound, and best_ask 100.02 ≤ 100.09 so
  it sends; events FILL 250 @ 100.02, FILL 50 @ 100.03 → FILLED.
- Assert totals: filled 500, avg = (200×100.01 + 250×100.02 + 50×100.03)/500
  = **100.017**; slippage_bps = (100.017−100.01)/100.01×1e4 = **+0.69993**
  (assert ≈0.70); `n_child_orders=3`; quality emitted once at t of last fill.

**T3 — Paper adapter marketable walk (worked example).**
Paper config: `submit_latency_ms=20`, `taker_fee_bps=1.0`. Book at t=100ms:
asks `[50.10×200, 50.11×300, 50.12×1000]`. Submit BUY 600 LIMIT IOC @ 50.11 at
t=100. Activation t=120. Expect exactly: FILL 200 @ 50.10
(fee = 200×50.10×0.0001 = **1.002**), FILL 300 @ 50.11 (fee **1.5033**), then
EXPIRED for remaining 100 (limit 50.11 < 50.12). cum_qty on second fill = 500.

**T4 — Paper adapter passive queue model (worked example).**
Book: bid 50.05×400. Rest BUY 300 @ 50.05 → `queue_ahead=400`.
- TradePrint 50.05×250 aggressor SELL → queue_ahead 150, no fill.
- TradePrint 50.05×300 aggressor SELL → consumes 150 queue, overflow 150 → FILL
  150 @ 50.05, MAKER, remaining 150, queue_ahead 0.
- TradePrint 50.04×500 (through) → FILL 150 @ **50.05** (our price, not 50.04).
- Assert: two fills, both MAKER, fees at `maker_fee_bps=0` → 0.0; a duplicated
  re-delivery of the second print (same simulated event) produces no third fill.
- Variant: TradePrint 50.05×250 aggressor **BUY** → assert no queue consumption.

**T5 — Idempotency and out-of-order.** Scripted: FILL (E1, 100 @ 10.00) arrives
*before* ACK → assert PENDING_NEW → PARTIALLY_FILLED directly, later ACK dropped as
stale (rank 1 < 2, `stale_status` metric +1). Re-deliver E1 → dropped,
`dup_fills` +1, no duplicate `Fill` on bus.

**T6 — Cancel race with fill.** ACKED order, engine sends cancel → PENDING_CANCEL;
scripted FILL completes qty → FILLED; then CANCEL_REJECT "too late" → dropped;
assert exactly one terminal `OrderState{FILLED}` and no CANCELLED emission.

**T7 — UNKNOWN resolution, both branches.** (a) `submit_order` raises Ambiguous;
`get_order` probe 2 returns live order cum 0 → assert UNKNOWN → ACKED, and
Risk-facing UNKNOWN `OrderState` was emitted first. (b) all probes return None →
assert cancel-the-ghost call made, then REJECTED `UNKNOWN_RESOLVED_ABSENT`, and the
retried child uses `attempt+1` id; original id never reused (assert on FakeAdapter
call log).

**T8 — Reconciliation mismatch (worked example).** Local shadow position AAPL +300
(from 3 fills ×100). FakeAdapter `get_positions` returns +500 (broker saw a 200-lot
fill we never received); `get_open_orders` returns one order of ours with
`cum_qty=500` vs local 300. Run one round. Assert: synthesized `Fill{qty=200,
synthetic=True, exec_id endswith "-recon-1"}` published; `ReconcileMismatch
{kind="CUM_QTY_GAP"}` then `{kind="POSITION_DRIFT", local="300", broker="500",
action_taken="ADOPTED_BROKER_QTY"}` published to the `reconcile` topic (Risk
subscribed); shadow now 500; `ReconcileReport.flat_confirmed is False`.

**T9 — FLATTEN_AGGRESSIVE loop.** Positions: XYZ +400. KillSwitch at t=0 with one
resting order live. Assert order of adapter calls: cancel(resting) →
get_open_orders → get_positions → submit SELL 400 IOC at
`cap_price(SELL, mid, 20bps)`. Script only 250 filling; cycle 1: assert re-snapshot,
new SELL 150 at cap 30 bps against the *new* mid, after 250 ms×2⁰... assert sleep
sequence 250, 500, 1000, 2000, 2000 ms via fake clock; alert emitted at cycle 10;
loop exits when `get_positions` returns empty; final `ReconcileReport
{flat_confirmed=True}`.

**T10 — Queue-full backpressure.** Fill an instrument queue (maxsize 8) with a slow
tactic; 9th request → immediate `OrderState{REJECTED, reason="QUEUE_FULL"}` and no
adapter call. **T11 — Per-instrument FIFO**: two requests for one instrument →
second tactic starts only after first's quality record; two instruments → interleaved
(assert overlapping adapter-call timestamps).

**T12 — Websocket drop.** STREAM_DOWN mid-PASSIVE_JOIN → assert no reprice submits
during the gap; STREAM_UP → out-of-band reconcile call within 100 ms (fake clock);
missed fill recovered via CUM_QTY_GAP synthesis (reuse T8 machinery).

**T13 — Cap unreachable.** CROSS_AFTER_TIMEOUT with ask pinned above cap for all
`cross_max_attempts` → assert no submit, three book re-reads spaced
`cross_reattempt_delay_ms`, final `OrderState{EXPIRED, reason="CAP_UNREACHABLE"}`.

**T14 — Config validation.** `passive_cap_bps=20 > cross_cap_bps=8` → loader raises
before engine start; each range bound tested at ±1 step.

**T15 — Replay determinism.** Run T2 twice from the journaled inputs with the fake
clock; assert byte-identical journal output (rule 1).

---

## 11. Acceptance criteria checklist

- [ ] Engine is the only code path that calls `ExecutionAdapter.submit_order` /
      `cancel_order` (enforced by an import-linter/grep CI check).
- [ ] Engine performs no sizing and no signal logic: `OrderRequest.qty` is executed
      exactly; total filled ≤ requested in every test; no code path alters qty
      except splitting the *same* remainder across children.
- [ ] All §2.3 legal transitions implemented; all illegal ones logged + reconciled;
      T5/T6 pass.
- [ ] `client_order_id` scheme implemented, ≤ 32 chars, never reused (T7b), stable
      under replay (T15).
- [ ] Duplicate fills (`exec_id`) and stale statuses (rank/cum_qty rules) never
      produce duplicate bus messages (T5).
- [ ] Four tactics implemented per §3 with exact price formulas; T1, T2, T9, T13
      pass with the stated numeric assertions.
- [ ] Slippage cap for CROSS_AFTER_TIMEOUT anchored to original arrival mid; FLATTEN
      re-anchors per cycle (asserted in T2/T9).
- [ ] `ExecutionAdapter` protocol implemented by both paper and real adapters;
      adapter test suite (taxonomy mapping, timeout enforcement ≤ timeout+1 s) green.
- [ ] Paper adapter fill model matches T3/T4 exactly (walk, queue_ahead, through-price
      fills at our price, MAKER/TAKER flags, fee arithmetic).
- [ ] UNKNOWN resolution protocol matches §2.5 (T7a/T7b).
- [ ] Reconciliation runs at configured cadences plus all out-of-band triggers;
      mismatch messages reach Risk (T8: assert on Risk-subscribed topic).
- [ ] `Fill` / `OrderState` / `ExecutionQuality` contain every field required by
      `docs/design.md`; `slippage_bps` matches §6.4 to 1e-9 relative tolerance.
- [ ] Per-instrument FIFO and bounded queues with QUEUE_FULL rejection (T10/T11).
- [ ] KillSwitch → cancel-all + flatten loop with backoff runs until broker-confirmed
      flat, unbounded attempts, alert after `flatten_alert_after_cycles` (T9).
- [ ] Every outbound message journaled before publish; T15 replay is byte-identical.
- [ ] Config loader validates all ranges and cross-field constraints (T14).
- [ ] Engine halts fail-safe (cancel-all, reject new, keep reconciling) on Risk
      heartbeat loss and on stream-down > timeout (T12 + halt test).
- [ ] Full test plan T1–T15 automated and green in CI.
