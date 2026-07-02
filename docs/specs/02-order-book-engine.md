# Block 2 — Order Book Engine: Implementation Specification

Status: v1. Conforms to `docs/design.md` (§3 Block 2, §4 cross-cutting rules, §5 technology
assumptions). Python 3.11+, asyncio, single process.

---

## 1. Overview and responsibilities

The Order Book Engine maintains, per instrument, a full limit-order-book replica built from
`BookSnapshot` and `BookDelta` events produced by Block 1 (Market Data Gateway). It is the ground
truth for every downstream computation.

**Responsibilities (exhaustive):**

1. Maintain one price-level book (all levels, both sides) per instrument from snapshot + deltas.
2. Verify per-instrument sequence-number continuity of deltas; on a gap, request a re-snapshot
   from Block 1, buffer deltas while recovering, and resynchronize when the snapshot arrives.
3. Detect integrity problems: crossed book, staleness (no feed events), feed-declared outages.
4. Emit `BookState` (top-N depth, mid, microprice, spread) both **event-driven** (on every change
   that affects the top N levels or derived values) and **sampled** (every `sampling_interval_ms`,
   default 100 ms, even if nothing changed).
5. Emit `BookIntegrity` on every integrity-state transition and on every sampling tick while the
   book is not OK.
6. Forward `TradePrint` events unchanged to downstream consumers (Block 3 receives trades "from
   Block 2" per the design diagram), preserving order relative to `BookState` emissions.

**Explicitly NOT responsibilities of this block:**

- Talking to any external API. Block 1 is the only market-data reader (single-writer rule). The
  re-snapshot "request" is a callback into Block 1, not a network call.
- Interpreting trades for signal purposes, computing features, imbalances, or statistics beyond
  mid/microprice/spread. That is Block 3.
- Timestamping or normalizing vendor formats — Block 1 has already done both.
- Archival/journaling — the event bus journals every message (design §2); this block only emits.
- Any order, position, or risk logic.

---

## 2. Data structures

### 2.1 Internal book representation

**Chosen representation:** per instrument, per side, a `sortedcontainers.SortedDict` mapping
`price_ticks: int → size: float`. Bids are keyed by `-price_ticks` so that iteration order is
best-first on both sides; asks are keyed by `+price_ticks`. All prices are converted to integer
ticks (`price_ticks = round(price / tick_size)`) on ingest and back to float
(`price_ticks * tick_size`) on emission.

*Justification.* A price-level book needs three operations per delta: point update/insert/delete
at an arbitrary price, and best-first traversal of the top N. `SortedDict` gives O(log L) insert/
delete, O(1) amortized lookup, and O(N) traversal of the first N keys via `keys()[:N]` — with L
(levels per side) rarely above a few thousand, this is well inside the 10–500 ms reaction budget
and far simpler than a hand-rolled ladder/array. Fixed arrays indexed by tick are faster but
require a bounded price range and waste memory on sparse books; heaps cannot do point deletion
cheaply. Integer-tick keys eliminate float-equality bugs when matching delta prices to existing
levels; crossed-book comparisons are exact integer comparisons. `sortedcontainers` is pure Python
(no build step), battle-tested, and its hot paths are C-speed via list bisection.

Per-instrument state held by the engine (see `_InstrumentState` in §3.4):

| Field | Type | Meaning |
|---|---|---|
| `bids` | `SortedDict[int, float]` | key = `-price_ticks`, value = size > 0 |
| `asks` | `SortedDict[int, float]` | key = `+price_ticks`, value = size > 0 |
| `last_seq` | `int` | seq of last applied snapshot/delta; `-1` = never synced |
| `integrity` | `IntegrityState` enum | see §7.2 state machine |
| `ts_last_feed` | `float` | `ts_local` of last event received from Block 1 for this instrument (any type) |
| `ts_crossed_since` | `float \| None` | local time the book first became crossed, else `None` |
| `recovery_buffer` | `list[BookDelta]` | deltas buffered while `RECOVERING`, ascending seq |
| `ts_snapshot_requested` | `float \| None` | local time of last snapshot request |
| `last_emitted` | `BookState \| None` | last emitted BookState (for tests/inspection only) |

Invariant enforced after every mutation: every stored size is `> 0` (size-0 levels are deleted,
never stored), and `len(bids) <= max_levels_per_side`, same for asks.

### 2.2 Consumed message types (defined by Block 1, restated here verbatim)

All timestamps in this spec are `float` seconds since the Unix epoch, UTC. `ts_exchange` is the
venue's event time; `ts_local` is Block 1's receive time. Prices are `float` in quote currency;
sizes are `float` in base units (shares/contracts). `seq` is the venue sequence number,
monotonically increasing per instrument, gapless in a healthy feed.

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

class Side(str, Enum):
    BID = "BID"
    ASK = "ASK"

@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: float   # quote currency; must be a multiple of tick_size (tolerance 1e-9 * tick_size)
    size: float    # base units; > 0

@dataclass(frozen=True, slots=True)
class BookSnapshot:
    instrument: str
    ts_exchange: float
    ts_local: float
    bids: tuple[PriceLevel, ...]   # best-first (descending price), full depth Block 1 has
    asks: tuple[PriceLevel, ...]   # best-first (ascending price)
    seq: int                       # book is exactly the state after event `seq`

@dataclass(frozen=True, slots=True)
class BookDelta:
    instrument: str
    ts_exchange: float
    ts_local: float
    side: Side
    price: float
    size: float                    # ABSOLUTE new size at level; 0.0 means "remove level"
    seq: int

@dataclass(frozen=True, slots=True)
class TradePrint:
    instrument: str
    ts_exchange: float
    ts_local: float
    price: float
    size: float
    aggressor_side: Side
    seq: int

class FeedState(str, Enum):
    LIVE = "LIVE"; STALE = "STALE"; GAP = "GAP"; DOWN = "DOWN"

@dataclass(frozen=True, slots=True)
class FeedStatus:
    instrument: str
    ts_local: float
    state: FeedState
    detail: str = ""
```

**Delta semantics (contract with Block 1):** `size` is the *absolute* new size at `price` on
`side`. `size > 0` on an absent price = add level; `size > 0` on a present price = replace size;
`size == 0.0` = remove level (removing an absent level is legal and is a no-op, see §4.3).

### 2.3 Emitted message types (owned by this block)

```python
@dataclass(frozen=True, slots=True)
class BookState:
    instrument: str
    ts: float                        # local epoch s. Event-driven: ts_local of the triggering
                                     # event. Sampled: the sampling-tick time.
    ts_exchange: float               # ts_exchange of the last event that changed the book
    bids: tuple[PriceLevel, ...]     # best-first, length = min(depth_n, live bid levels); no padding
    asks: tuple[PriceLevel, ...]     # best-first, length = min(depth_n, live ask levels); no padding
    mid: float | None                # see §2.4; None if either side empty
    microprice: float | None         # see §2.4; None if either side empty
    spread: float | None             # see §2.4; None if either side empty
    seq: int                         # venue seq of last applied snapshot/delta
    trigger: Literal["event", "sample"]

class IntegrityState(str, Enum):
    OK = "OK"                # synced, uncrossed, fresh
    RECOVERING = "RECOVERING"  # gap detected / snapshot pending; book contents untrusted
    CROSSED = "CROSSED"      # best_bid >= best_ask
    STALE = "STALE"          # no feed event for > staleness_warn_ms
    DOWN = "DOWN"            # Block 1 declared DOWN, or staleness > staleness_down_ms

@dataclass(frozen=True, slots=True)
class BookIntegrity:
    instrument: str
    ts: float                        # local time of emission
    ok: bool                         # True iff state == OK
    state: IntegrityState
    crossed_book: bool               # True iff best_bid_ticks >= best_ask_ticks right now
    staleness_ms: float              # (now - ts_last_feed) * 1000.0; >= 0
    last_seq: int                    # -1 if never synced
    detail: str = ""                 # human-readable cause, e.g. "gap: expected 104, got 106"

@dataclass(frozen=True, slots=True)
class SnapshotRequest:               # sent to Block 1 via callback, journaled like any message
    instrument: str
    ts: float
    reason: Literal["gap", "crossed", "timeout", "startup", "feed_status"]
```

**Validation rules (asserted in the constructors' `__post_init__` in debug builds, checked by
tests always):**

- `BookState.bids` strictly descending in price; `asks` strictly ascending; all sizes > 0;
  lengths ≤ `depth_n`.
- `BookState` is emitted **only** when `IntegrityState == OK`; therefore an emitted `BookState`
  is never crossed and `mid/microprice/spread` follow §2.4 exactly.
- `BookIntegrity.staleness_ms >= 0`; `ok == (state == IntegrityState.OK)`.

### 2.4 Derived-value formulas (exact)

Let `Pb` = best bid price, `Sb` = size at best bid, `Pa` = best ask price, `Sa` = size at best
ask. Prices used in formulas are floats reconstructed as `ticks * tick_size`; all arithmetic is
IEEE-754 double; emit unrounded.

| Quantity | Formula | Edge cases |
|---|---|---|
| `mid` | `(Pb + Pa) / 2.0` | If either side is empty → `None`. |
| `spread` | `Pa - Pb` | If either side is empty → `None`. Never negative in an emitted `BookState` (crossed books are not emitted). |
| `microprice` | `(Pb * Sa + Pa * Sb) / (Sb + Sa)` | If either side is empty → `None`. `Sb + Sa > 0` always holds (stored sizes are > 0), so no division by zero. If `Sb == Sa`, microprice equals mid. |

One-sided book (e.g. only bids): the book can still be internally OK; `BookState` is emitted with
the populated side, the other side as an empty tuple, and `mid = microprice = spread = None`.
Empty book on both sides (possible immediately after a snapshot of a halted instrument): emitted
with both tuples empty and all three derived values `None`.

---

## 3. Public API

Module: `bot/order_book/engine.py` (pure core in `bot/order_book/book.py`).

### 3.1 `OrderBook` — per-instrument book (pure, synchronous)

```python
class OrderBook:
    def __init__(self, instrument: str, tick_size: float,
                 max_levels_per_side: int) -> None: ...

    def apply_snapshot(self, snap: BookSnapshot) -> None:
        """Discard current contents, load snapshot levels, set last_seq = snap.seq.
        Raises BookError on invalid input (§7.1 rows I1–I3)."""

    def apply_delta(self, delta: BookDelta) -> bool:
        """Apply one delta whose seq has already been validated by the caller.
        Returns True iff the top depth_n of either side OR any derived value changed.
        Raises BookError on invalid input (§7.1 rows I1–I3)."""

    def top(self, n: int) -> tuple[tuple[PriceLevel, ...], tuple[PriceLevel, ...]]:
        """(bids, asks), best-first, at most n levels each. O(n)."""

    def best_bid_ticks(self) -> int | None: ...
    def best_ask_ticks(self) -> int | None: ...
    def is_crossed(self) -> bool:
        """best_bid_ticks() is not None and best_ask_ticks() is not None
        and best_bid_ticks() >= best_ask_ticks()."""

    def mid(self) -> float | None: ...
    def microprice(self) -> float | None: ...
    def spread(self) -> float | None: ...

    @property
    def last_seq(self) -> int: ...
    def clear(self) -> None:
        """Empty both sides, last_seq = -1."""
```

### 3.2 `OrderBookEngineCore` — deterministic state machine (no asyncio, no wall clock)

This is the replayable heart (cross-cutting rule 1). It never reads the clock; all time comes in
as arguments. Block 8 drives it directly with the simulated clock.

```python
InEvent  = BookSnapshot | BookDelta | TradePrint | FeedStatus
OutEvent = BookState | BookIntegrity | TradePrint

class OrderBookEngineCore:
    def __init__(self, config: OrderBookEngineConfig,
                 request_snapshot: Callable[[SnapshotRequest], None]) -> None: ...

    def handle_event(self, event: InEvent, now: float) -> list[OutEvent]:
        """Process one input event. `now` is the current local time (real or simulated);
        the caller MUST pass event.ts_local for feed events. Returns emissions in order.
        Never raises for malformed feed data (§7); raises only on programmer error."""

    def handle_timer(self, now: float) -> list[OutEvent]:
        """Run one sampling tick and all time-based checks (staleness, crossed grace,
        snapshot timeout) for every configured instrument. Idempotent for a given state."""

    def next_deadline(self, now: float) -> float:
        """Absolute time of the next required handle_timer call:
        min(next sampling tick, earliest pending crossed-grace / snapshot-timeout /
        staleness deadline). Always <= now + sampling_interval_ms/1000."""
```

### 3.3 `OrderBookEngine` — asyncio wrapper

```python
class OrderBookEngine:
    def __init__(self,
                 config: OrderBookEngineConfig,
                 in_queue: asyncio.Queue[InEvent],          # written by Block 1
                 out_queue: asyncio.Queue[OutEvent],        # read by bus fan-out to Blocks 3/5/6/9
                 request_snapshot: Callable[[SnapshotRequest], None],  # provided by Block 1
                 clock: Callable[[], float] = time.time) -> None: ...

    async def run(self) -> None:
        """Main loop; returns after stop(). One task; see §3.4."""

    async def stop(self) -> None:
        """Graceful shutdown: stop consuming, flush pending emissions, return from run()."""
```

### 3.4 Lifecycle and asyncio model

- **One asyncio task** per engine instance handles all instruments. No locks; the core is only
  ever touched from that task.
- `run()` loop (exact):
  1. `deadline = core.next_deadline(clock())`.
  2. `event = await asyncio.wait_for(in_queue.get(), timeout=max(0.0, deadline - clock()))`.
     - On event: `outs = core.handle_event(event, now=event.ts_local)` for feed events
       (`FeedStatus` uses its `ts_local`); then `in_queue.task_done()`.
     - On `TimeoutError`: `outs = core.handle_timer(clock())`.
  3. For each `out` in `outs`: `await out_queue.put(out)` (backpressure — the engine slows down
     rather than drops; see §5.2).
  4. Repeat until `stop()` sets the stop flag; then drain step 3 for any pending `outs` and return.
- **Startup:** constructor builds one `_InstrumentState` per configured instrument in state
  `RECOVERING` and `handle_timer`'s first call issues `SnapshotRequest(reason="startup")` for each.
  No `BookState` is emitted before the first snapshot is applied.
- **Shutdown:** no special book teardown; the journal has everything.

---

## 4. Detailed behavior

All steps below are per instrument unless stated. "Emit X" means append to the returned list of
`handle_event`/`handle_timer` (the wrapper serializes to `out_queue` in that order).

### 4.1 Applying a snapshot (`BookSnapshot`, seq = S)

1. Validate (reject whole snapshot on failure → §7.1 row I1–I3; stay/enter `RECOVERING`, emit
   `BookIntegrity`, re-request with `reason="timeout"` semantics after `resnapshot_min_interval_ms`):
   prices on tick grid, sizes > 0, bids strictly descending, asks strictly ascending, per-side
   level count ≤ `max_levels_per_side`.
2. If state is `OK`/`CROSSED`/`STALE` and `S < last_seq`: drop silently (stale unsolicited
   snapshot), increment counter `snapshots_dropped_stale`. Done.
3. `book.apply_snapshot(snap)` → book replaced, `last_seq = S`. Update `ts_last_feed = ts_local`.
4. If state was `RECOVERING`, drain the recovery buffer (§4.4 step R4).
5. Run the post-change check (§4.5). If not crossed: set state `OK` (emit `BookIntegrity` if that
   is a transition), then emit an event-driven `BookState` (`trigger="event"`).

### 4.2 Sequence checking for deltas (`BookDelta`, seq = D)

On every `BookDelta`:

1. Update `ts_last_feed = ts_local` (a delta proves feed liveness even if we can't apply it).
2. If state is `RECOVERING`: buffer (§4.4 step R2). Done.
3. If `last_seq == -1` (never synced): buffer as in `RECOVERING` and enter recovery (§4.4). Done.
4. If `D <= last_seq`: duplicate/replay — drop, increment `deltas_dropped_dup`. Done.
5. If `D == last_seq + 1`: apply (§4.3).
6. If `D > last_seq + 1`: **gap**. Enter recovery (§4.4) with
   `detail = f"gap: expected {last_seq+1}, got {D}"`, and buffer this delta.

### 4.3 Applying a delta (in-sequence)

1. Validate: `price > 0`, on tick grid (|price/tick_size − round(price/tick_size)| ≤ 1e-9·scale),
   `size >= 0`, finite. On failure → §7.1 row I2 (treated as a gap: enter recovery; a malformed
   delta means we can no longer trust continuity).
2. Convert `pt = round(price / tick_size)`; select side dict.
3. Mutation:
   - `size == 0.0`: `dict.pop(key, None)`. Removing an absent level is a **no-op, not an error**
     (venues send remove-after-trade races); increment `removes_noop`.
   - `size > 0.0`, key absent: insert (add level). If the side now exceeds
     `max_levels_per_side`, evict the worst level (highest ask / lowest bid) and increment
     `levels_evicted` — top-N output is unaffected because `max_levels_per_side >> depth_n`.
   - `size > 0.0`, key present: overwrite (update level).
4. `last_seq = D`.
5. Run the post-change check (§4.5). If it left the state `OK` **and** `apply_delta` returned
   True (top-N or derived values changed): emit event-driven `BookState` with
   `ts = ts_local`, `ts_exchange = delta.ts_exchange`, `trigger="event"`. Deltas deeper than
   `depth_n` that change nothing visible emit nothing (the 100 ms sampler still covers them).

### 4.4 Gap recovery

- **R1 (enter):** set state `RECOVERING` (emit `BookIntegrity`), clear
  `ts_crossed_since`, call `request_snapshot(SnapshotRequest(instrument, now, reason))` unless a
  request is already outstanding and `now - ts_snapshot_requested < resnapshot_min_interval_ms/1000`;
  set `ts_snapshot_requested = now`. `BookState` emission is suppressed from this instant.
- **R2 (buffer):** while `RECOVERING`, every arriving `BookDelta` is appended to
  `recovery_buffer` if `delta.seq > (recovery_buffer[-1].seq if buffer else -1)`; out-of-order or
  duplicate buffered deltas are dropped. If `len(recovery_buffer) > recovery_buffer_max`: clear
  the buffer, increment `recovery_buffer_overflows`, re-request snapshot (`reason="timeout"`).
  `TradePrint`s are **still forwarded** during recovery (§5.1).
- **R3 (timeout):** in `handle_timer`, if `RECOVERING` and
  `now - ts_snapshot_requested > snapshot_timeout_ms/1000`: re-request (`reason="timeout"`),
  update `ts_snapshot_requested`. Unlimited retries; escalation to DOWN comes only from
  `FeedStatus` or the staleness clock.
- **R4 (resync, on snapshot seq = S):** after `apply_snapshot`:
  1. Discard buffered deltas with `seq <= S`.
  2. Walk the remainder in ascending seq: while the next has `seq == last_seq + 1`, apply via
     §4.3 steps 1–4 (no per-delta emission).
  3. If a buffered delta remains with `seq > last_seq + 1` (gap *within* the buffer): stay
     `RECOVERING`, keep only the post-gap tail in the buffer, re-request snapshot
     (`reason="gap"`). Done — wait for the next snapshot.
  4. Otherwise: clear the buffer, run §4.5; if uncrossed → state `OK`, emit `BookIntegrity(ok=True)`
     then one event-driven `BookState` reflecting the fully resynced book.

### 4.5 Crossed-book detection (post-change check)

Run after every applied snapshot or delta, and at every timer tick:

1. `crossed = book.is_crossed()` (integer-tick comparison, `best_bid >= best_ask`).
2. Not crossed: if state was `CROSSED`, transition back to `OK`, emit `BookIntegrity(ok=True)`,
   emit one event-driven `BookState`; set `ts_crossed_since = None`.
3. Crossed and `ts_crossed_since is None`: set `ts_crossed_since = now`, state `CROSSED`, emit
   `BookIntegrity(ok=False, crossed_book=True)`. `BookState` suppressed. (Transient crosses happen
   when a venue updates the two sides in consecutive deltas; the grace window absorbs them.)
4. Crossed and `now - ts_crossed_since > crossed_grace_ms/1000` (checked in `handle_timer`): the
   book is presumed corrupt → enter recovery (§4.4 R1, `reason="crossed"`).

### 4.6 Staleness detection

In every `handle_timer(now)` call, per instrument, compute
`staleness_ms = (now - ts_last_feed) * 1000.0`:

1. `state == OK` and `staleness_ms > staleness_warn_ms` → state `STALE`, emit
   `BookIntegrity(ok=False, state=STALE)`. Sampled `BookState` emission stops (the world may have
   moved; a stale book must not feed Block 3 as fresh truth).
2. `state == STALE` and `staleness_ms > staleness_down_ms` → state `DOWN`, emit `BookIntegrity`.
3. `state in {STALE, DOWN}` and any feed event arrives for the instrument → the event updates
   `ts_last_feed`; if the event is applicable (in-sequence delta / valid snapshot) the instrument
   returns to `OK` via the normal paths above (a `STALE→OK` transition emits
   `BookIntegrity(ok=True)` and one event-driven `BookState`); an out-of-sequence delta after
   staleness goes through §4.2 step 6 into recovery as usual.
4. `FeedStatus` handling: `GAP` → enter recovery (§4.4, `reason="feed_status"`); `DOWN` → state
   `DOWN`, clear book (`book.clear()`), clear buffer, emit `BookIntegrity`; `LIVE` while `DOWN`
   → enter recovery (request snapshot); `STALE` → informational only (own clock governs;
   increment `feed_stale_notices`).

### 4.7 Sampled emission (the 100 ms clock)

- Global tick grid, shared by all instruments: tick times are exact multiples of
  `sampling_interval_ms` on the local epoch clock:
  `next_tick(now) = (floor(now / dt) + 1) * dt` where `dt = sampling_interval_ms / 1000`.
- `handle_timer(now)` fires all ticks with `tick_ts <= now` that have not fired yet (catch-up
  after a stall executes the missed ticks' *checks* but emits at most one sampled `BookState` per
  instrument per call, with `ts` = the latest tick time — no flood after a GC pause).
- At each tick, per instrument, **exactly this**:
  1. Run staleness (§4.6), crossed-grace (§4.5.4), snapshot-timeout (§4.4 R3) checks.
  2. If state is `OK`: emit `BookState` with `trigger="sample"`, `ts = tick_ts`,
     `ts_exchange` = ts_exchange of the last book-changing event, current `seq`.
     **This happens even if nothing changed since the last emission** — Block 3's rolling windows
     require an unconditional regular clock; consecutive sampled states may be identical except
     for `ts`.
  3. If state is not `OK`: emit `BookIntegrity` (current state, fresh `staleness_ms`) instead of
     a `BookState`, so downstream health monitors see a heartbeat while degraded.
- Event-driven and sampled emissions are independent: a change at t=99 ms emits an event-driven
  state, and the t=100 ms tick still emits a sampled one.

### 4.8 TradePrint pass-through

`handle_event(TradePrint)` updates `ts_last_feed` and returns `[trade]` unchanged, in all
integrity states (trades are venue-confirmed facts and Block 3/9 need them even while the book
recovers). No reordering: the pass-through preserves arrival order relative to `BookState`
emissions triggered by neighboring events.

---

## 5. Interaction contracts

### 5.1 Upstream (Block 1)

- **Consumes:** `BookSnapshot`, `BookDelta`, `TradePrint`, `FeedStatus` from a single bounded
  `asyncio.Queue` written only by Block 1.
- **Order guarantee assumed:** per instrument, Block 1 delivers events in `ts_local` order (its
  receive order). Venue `seq` may still have duplicates or gaps — this block owns seq validation.
  Cross-instrument interleaving is arbitrary.
- **Snapshot requests:** via the injected `request_snapshot` callable (Block 1 provides it; it
  enqueues work on Block 1's side and returns immediately — non-blocking, never raises). Block 1
  responds with a `BookSnapshot` on the normal input queue. `SnapshotRequest` is journaled.
- This block never blocks Block 1 except through the bounded input queue's natural backpressure.

### 5.2 Downstream (Blocks 3, 5, 6, 9)

- **Delivery:** the engine `await`s `out_queue.put(...)`; the bus fans out to subscribers and
  journals every message. Within the process this yields **exactly-once, in-order** delivery per
  message. Across a process crash the guarantee degrades to **at-most-once** for messages not yet
  journaled — acceptable because (a) the journal + replay reconstructs state (design rule 1), and
  (b) downstream blocks fail toward flat/halted on silence (design rule 3). Consumers MUST
  tolerate a missing tick and MUST NOT rely on at-least-once.
- **Ordering guarantee (per instrument):** `BookState.seq` is non-decreasing;
  `BookIntegrity` transitions are emitted before the first `BookState` they gate (an `ok=True`
  integrity precedes the first post-recovery `BookState`); no `BookState` is ever emitted while
  `ok=False` was the last emitted integrity state.
- **No conflation:** the engine does not drop or merge emissions; if `out_queue` is full it
  backpressures (and the resulting input backlog eventually trips staleness upstream —
  fail-safe direction).
- Blocks 3/5/6 subscribe to `BookState` (+ `TradePrint` for Block 3); Block 9 subscribes to
  everything including `BookIntegrity` and `SnapshotRequest`.

---

## 6. Configuration

Config lives in the versioned run config (design rule 4), section `order_book_engine`, plus the
shared `instruments` section. Loaded into:

```python
@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    symbol: str
    tick_size: float          # > 0

@dataclass(frozen=True, slots=True)
class OrderBookEngineConfig:
    instruments: tuple[InstrumentSpec, ...]
    depth_n: int = 10
    sampling_interval_ms: int = 100
    staleness_warn_ms: int = 1_000
    staleness_down_ms: int = 5_000
    crossed_grace_ms: int = 50
    snapshot_timeout_ms: int = 2_000
    resnapshot_min_interval_ms: int = 500
    recovery_buffer_max: int = 10_000
    max_levels_per_side: int = 5_000
    out_queue_maxsize: int = 10_000   # used by the process wiring, not the core
```

Complete example (`config/run.yaml` excerpt):

```yaml
order_book_engine:
  depth_n: 10                    # int, levels per side in BookState. Range [1, 50]. Default 10.
  sampling_interval_ms: 100      # int, sampled-emission period. Range [10, 1000]. Default 100.
  staleness_warn_ms: 1000        # int, OK->STALE threshold. Range [100, 60000]. Default 1000.
  staleness_down_ms: 5000        # int, STALE->DOWN threshold. Must be > staleness_warn_ms.
                                 # Range [500, 300000]. Default 5000.
  crossed_grace_ms: 50           # int, tolerated crossed duration before re-snapshot.
                                 # Range [0, 1000]. Default 50.
  snapshot_timeout_ms: 2000      # int, re-request snapshot if none arrives. Range [200, 30000].
  resnapshot_min_interval_ms: 500  # int, min gap between requests. Range [100, 10000].
  recovery_buffer_max: 10000     # int, max buffered deltas per instrument. Range [100, 1_000_000].
  max_levels_per_side: 5000      # int, hard memory cap per side. Range [depth_n, 100_000].
  out_queue_maxsize: 10000       # int, bounded output queue. Range [100, 1_000_000].

instruments:                     # shared section; Block 2 uses symbol + tick_size
  - symbol: XYZ
    tick_size: 0.01              # float > 0, quote currency
  - symbol: ABC
    tick_size: 0.05
```

Validation at startup (fail fast, refuse to start): all ranges above; `staleness_down_ms >
staleness_warn_ms`; `sampling_interval_ms <= staleness_warn_ms`; every `tick_size > 0`; at least
one instrument; symbols unique. There are no runtime-mutable parameters in v1.

---

## 7. Error handling

### 7.1 Failure-mode table

| # | Failure mode | Detection | Response | Emitted |
|---|---|---|---|---|
| G1 | Delta seq gap (`D > last_seq+1`) | §4.2 step 6 | Enter recovery: buffer delta, request snapshot, suppress BookState | `BookIntegrity(RECOVERING)`, `SnapshotRequest("gap")` |
| G2 | Duplicate/old delta (`D <= last_seq`) | §4.2 step 4 | Drop; count `deltas_dropped_dup` | none |
| G3 | Gap inside recovery buffer after snapshot | §4.4 R4.3 | Keep post-gap tail, re-request | `SnapshotRequest("gap")` |
| G4 | Snapshot timeout while RECOVERING | §4.4 R3 | Re-request (rate-limited), stay RECOVERING | `SnapshotRequest("timeout")` |
| G5 | Recovery buffer overflow | §4.4 R2 | Clear buffer, re-request | `SnapshotRequest("timeout")` |
| C1 | Crossed book, within grace | §4.5.3 | Suppress BookState, wait | `BookIntegrity(CROSSED)` |
| C2 | Crossed book, grace expired | §4.5.4 | Enter recovery | `BookIntegrity(RECOVERING)`, `SnapshotRequest("crossed")` |
| S1 | No feed events > `staleness_warn_ms` | §4.6.1 | Stop sampled BookState; keep book | `BookIntegrity(STALE)` each tick |
| S2 | No feed events > `staleness_down_ms` | §4.6.2 | Stay silent, wait for feed | `BookIntegrity(DOWN)` each tick |
| F1 | `FeedStatus GAP` | §4.6.4 | Enter recovery | as G1 |
| F2 | `FeedStatus DOWN` | §4.6.4 | Clear book + buffer, state DOWN | `BookIntegrity(DOWN)` |
| F3 | `FeedStatus LIVE` after DOWN | §4.6.4 | Enter recovery (fresh snapshot) | `SnapshotRequest("feed_status")` |
| I1 | Malformed snapshot (unsorted, size ≤ 0, off-grid price, too many levels) | §4.1.1 | Reject snapshot, stay RECOVERING, re-request after rate limit; count `snapshots_rejected` | `BookIntegrity(RECOVERING)` |
| I2 | Malformed delta (non-finite, price ≤ 0, off-grid, size < 0) | §4.3.1 | Treat as gap (continuity broken) → recovery | as G1, `detail="malformed delta"` |
| I3 | Event for unconfigured instrument | instrument lookup | Drop; count `events_unknown_instrument`; log once per symbol | none |
| I4 | Remove of absent level (`size==0`, no such price) | §4.3.3 | No-op; count `removes_noop` | none |
| M1 | Side exceeds `max_levels_per_side` | §4.3.3 | Evict worst level; count `levels_evicted` | none |
| Q1 | `out_queue` full | `put` blocks | Backpressure (never drop); input backlog → upstream staleness handles escalation | none directly |

All counters are plain ints on the core, exposed via `core.counters: dict[str, int]` and scraped
by Block 9; they are not bus messages.

### 7.2 `BookIntegrity` state machine

States: `OK, RECOVERING, CROSSED, STALE, DOWN`. Initial: `RECOVERING` (startup).

```
RECOVERING --valid snapshot + buffer drained, uncrossed--> OK
RECOVERING --snapshot leaves book crossed----------------> CROSSED
OK         --seq gap / malformed delta / FeedStatus GAP--> RECOVERING
OK         --post-change crossed-------------------------> CROSSED
OK         --staleness > warn----------------------------> STALE
CROSSED    --uncrossing delta----------------------------> OK
CROSSED    --grace expired-------------------------------> RECOVERING
CROSSED    --seq gap-------------------------------------> RECOVERING
STALE      --applicable feed event-----------------------> OK (or RECOVERING/CROSSED per event)
STALE      --staleness > down----------------------------> DOWN
ANY        --FeedStatus DOWN-----------------------------> DOWN  (book cleared)
DOWN       --FeedStatus LIVE or any feed event-----------> RECOVERING
```

Emission rule: one `BookIntegrity` on **every** transition, plus one per sampling tick while the
state is not `OK` (heartbeat). `crossed_book` and `staleness_ms` are always the instantaneous
values, whatever the state.

---

## 8. Performance budget

Targets on the reference host (4-core x86-64, CPython 3.11), per instrument, book with ≤ 5 000
levels/side:

| Operation | Complexity | Latency target (p99) |
|---|---|---|
| `apply_delta` (mutation + seq check + crossed check) | O(log L) | ≤ 20 µs |
| Event-driven emission (top-N copy + mid/microprice/spread + dataclass) | O(N) | ≤ 30 µs |
| Sampling tick, all instruments (U ≤ 50) | O(U · N) | ≤ 2 ms |
| `apply_snapshot` (5 000 levels/side) | O(L log L) | ≤ 10 ms |

End-to-end budget contribution: ≤ 0.1 ms of the system's 10–500 ms reaction budget under normal
load.

**Memory bound per instrument:**
`2 × max_levels_per_side` SortedDict entries (int key + float value ≈ 100 B amortized) ≈ 1 MB,
plus `recovery_buffer_max` deltas (≈ 120 B slotted dataclass) ≈ 1.2 MB worst case during
recovery. Hard bound ≈ **2.5 MB/instrument**; steady state ≈ 1 MB.

**How to measure:** `benchmarks/bench_order_book.py` (pytest-benchmark): (a) synthetic stream of
1e6 uniformly random deltas over a 5 000-level book, report ns/op for `apply_delta` and full
`handle_event`; (b) `tracemalloc` snapshot after loading a full book to verify the memory bound;
(c) replay one archived full trading day through `OrderBookEngineCore` and assert wall time
< 5 % of session length. The benchmark runs in CI as a non-blocking report; regressions > 25 %
fail review.

---

## 9. Test plan

Framework: `pytest`; all core tests are synchronous (drive `OrderBookEngineCore.handle_event` /
`handle_timer` with explicit `now` values); asyncio wrapper tests use `pytest-asyncio`.
`request_snapshot` is a recording stub. Instrument `XYZ`, `tick_size=0.01`, `depth_n=5`,
`sampling_interval_ms=100`, `crossed_grace_ms=50`, `staleness_warn_ms=1000`,
`staleness_down_ms=5000`, `snapshot_timeout_ms=2000`. Float assertions use
`pytest.approx(rel=1e-12)`.

### 9.1 Unit — `OrderBook`

| # | Test | Assertion |
|---|---|---|
| U1 | Snapshot load | levels sorted best-first, `last_seq` set, `top(n)` correct |
| U2 | Add / update / remove level | dict contents, `apply_delta` return flag True only when top-5 or derived values change |
| U3 | `size=0` on absent level | no-op, returns False, `removes_noop` incremented |
| U4 | One-sided and empty books | `mid/microprice/spread` all `None`; `is_crossed()` False |
| U5 | Off-grid price, size<0, non-finite | raises `BookError` |
| U6 | Eviction at `max_levels_per_side` | worst level evicted, best levels intact |
| U7 | Crossed detection | `is_crossed()` True iff best_bid_ticks ≥ best_ask_ticks (test equal-price case) |
| U8 | Microprice equals mid when `Sb == Sa` | exact equality |

### 9.2 Worked numeric example — snapshot + deltas (test `test_worked_example`)

Feed `handle_event` in order at `now = ts_local` (state starts `RECOVERING`; startup
`SnapshotRequest` already consumed by a `handle_timer(0.0)` call):

**Event 1 — `BookSnapshot(seq=100, ts_local=10.000)`**
bids: (10.00, 500), (9.99, 300), (9.98, 200); asks: (10.02, 400), (10.03, 250), (10.05, 600).

Expect emissions, in order: `BookIntegrity(ok=True, state=OK, last_seq=100)`, then
`BookState(trigger="event", seq=100)` with:
- `bids = ((10.00,500),(9.99,300),(9.98,200))`, `asks = ((10.02,400),(10.03,250),(10.05,600))`
- `mid = (10.00+10.02)/2 = 10.01`
- `spread = 0.02`
- `microprice = (10.00·400 + 10.02·500)/(500+400) = 9010/900 = 10.011111111111111`

**Event 2 — `BookDelta(seq=101, side=BID, price=10.01, size=100, ts_local=10.050)`** (new best bid)

Expect one `BookState(trigger="event", seq=101)`:
- `bids = ((10.01,100),(10.00,500),(9.99,300),(9.98,200))`
- `mid = (10.01+10.02)/2 = 10.015`, `spread = 0.01` (approx)
- `microprice = (10.01·400 + 10.02·100)/(100+400) = 5006/500 = 10.012`

**Event 3 — `BookDelta(seq=102, side=ASK, price=10.02, size=0, ts_local=10.080)`** (remove best ask)

Expect one `BookState(seq=102)`:
- `asks = ((10.03,250),(10.05,600))`
- `mid = (10.01+10.03)/2 = 10.02`, `spread = 0.02` (approx)
- `microprice = (10.01·250 + 10.03·100)/(100+250) = 3505.5/350 = 10.015714285714286`

**Event 4 — `handle_timer(now=10.100)`** (sampling tick)

Expect one `BookState(trigger="sample", ts=10.100, seq=102)` — identical levels/derived values to
Event 3's output.

**Event 5 — `BookDelta(seq=103, side=BID, price=9.99, size=350, ts_local=10.120)`** (update depth level)

Expect one `BookState(seq=103)` with `bids = ((10.01,100),(10.00,500),(9.99,350),(9.98,200))`;
`mid/microprice/spread` unchanged from Event 3 (best levels untouched).

**Event 6 — `handle_timer(now=10.200)`**: one `BookState(trigger="sample", ts=10.200, seq=103)`
even though nothing changed since Event 5's emission.

### 9.3 Gap recovery (test `test_gap_recovery`)

Continue from §9.2 (`last_seq=103`):

1. `BookDelta(seq=105, side=ASK, price=10.04, size=50)` → expect: no `BookState`;
   `BookIntegrity(ok=False, state=RECOVERING, detail~"expected 104, got 105")`;
   `request_snapshot` called once with `reason="gap"`; delta buffered.
2. `BookDelta(seq=106, side=BID, price=10.00, size=450)` → buffered, no emissions,
   no second snapshot request (rate limit).
3. `handle_timer` during recovery → `BookIntegrity(RECOVERING)` heartbeat, **no** `BookState`.
4. `BookSnapshot(seq=106)` arrives: bids (10.01, 100), (10.00, 450); asks (10.03, 250),
   (10.04, 50), (10.05, 600) → buffered 105 and 106 discarded (`seq <= 106`); buffer empty.
   Expect `BookIntegrity(ok=True)` then `BookState(trigger="event", seq=106)` with
   `mid = 10.02`, `microprice = (10.01·250 + 10.03·100)/350 = 10.015714285714286`.
5. Variant A (buffered replay): snapshot arrives with `seq=104` instead → buffered 105, 106
   applied in order; final `BookState.seq == 106` and book reflects both deltas.
6. Variant B (gap in buffer): buffered deltas are 105 and 107; snapshot `seq=105` → 105
   discarded; next buffered seq 107 ≠ `last_seq + 1` (= 106) → stays `RECOVERING`, second
   `request_snapshot(reason="gap")`, buffer == [107].
7. Variant C (timeout): no snapshot for `snapshot_timeout_ms` → `handle_timer` triggers
   re-request `reason="timeout"`; a request is never issued more often than
   `resnapshot_min_interval_ms`.

### 9.4 Crossed book (test `test_crossed_book`)

From a clean book (best bid 10.01, best ask 10.03, `last_seq=200`, `now=20.000`):

1. `BookDelta(seq=201, side=BID, price=10.03, size=50, ts_local=20.000)` → bid 10.03 ≥ ask 10.03:
   expect `BookIntegrity(ok=False, state=CROSSED, crossed_book=True)`, **no** `BookState`.
2. `BookDelta(seq=202, side=ASK, price=10.03, size=0, ts_local=20.020)` (inside 50 ms grace) →
   book uncrosses (new best ask 10.04, say): expect `BookIntegrity(ok=True)` then
   `BookState(seq=202)`. No snapshot request was made.
3. Variant: after step 1, no uncrossing event; `handle_timer(now=20.060)` (grace 50 ms expired) →
   `BookIntegrity(RECOVERING)` + `request_snapshot(reason="crossed")`.
4. Equal-price cross (`best_bid == best_ask`) is detected as crossed (locked book counts as
   crossed in v1).

### 9.5 Staleness, feed status, pass-through, sampler

| # | Test | Scenario → assertion |
|---|---|---|
| T1 | `test_staleness` | Last event at t=30.0; `handle_timer(31.1)` → `BookIntegrity(STALE, staleness_ms≈1100)`, no sampled `BookState`; `handle_timer(35.1)` → `DOWN`; delta `seq=last+1` at t=36.0 → back to `OK`, `BookState` emitted |
| T2 | `test_feed_down` | `FeedStatus(DOWN)` → book cleared, `BookIntegrity(DOWN)`; `FeedStatus(LIVE)` → `SnapshotRequest("feed_status")`, `RECOVERING` |
| T3 | `test_trade_passthrough` | `TradePrint` in states OK and RECOVERING → returned unchanged, in order, and refreshes `ts_last_feed` |
| T4 | `test_sampler_grid` | Ticks land on exact multiples of 0.1 s; `handle_timer(now=10.35)` after last tick 10.1 → exactly one sampled `BookState` with `ts=10.3` (catch-up, no flood) |
| T5 | `test_duplicate_delta` | `seq <= last_seq` dropped, counter incremented, no emission |
| T6 | `test_unknown_instrument` | Event for symbol not in config → dropped, counter incremented |
| T7 | `test_emission_ordering` | For every scenario above, assert: `BookState.seq` non-decreasing; an `ok=True` integrity precedes the first post-recovery `BookState`; no `BookState` while last emitted integrity has `ok=False` |
| T8 | `test_malformed` | Off-grid delta price (10.0151) → recovery entered (I2); malformed snapshot rejected (I1), state stays RECOVERING |

### 9.6 Integration

| # | Test | Scenario |
|---|---|---|
| A1 | `test_asyncio_wrapper` | Feed 1 000 events through `in_queue` with a fake clock; assert `out_queue` contents byte-identical to driving the core directly (determinism of the wrapper) |
| A2 | `test_replay_determinism` | Run the same journaled input stream twice through fresh cores → identical emission lists (field-for-field) |
| A3 | `test_backpressure` | `out_queue(maxsize=1)` with a slow consumer → no emission lost, order preserved |
| A4 | `test_full_day_replay` | Replay an archived day (Block 8 fixture) → zero integrity violations of §2.3 validation rules, benchmark within §8 targets |

---

## 10. Acceptance criteria checklist

- [ ] `OrderBook`, `OrderBookEngineCore`, `OrderBookEngine` implemented with the exact signatures
      in §3; all public functions carry Python 3.11 type hints; `mypy --strict` clean.
- [ ] Book representation is per-side `SortedDict[int, float]` keyed by (negated) integer ticks;
      no floats used as dict keys anywhere.
- [ ] `mid`, `microprice`, `spread` match §2.4 exactly, including `None` for one-sided/empty
      books; worked example §9.2 passes with the exact numbers listed.
- [ ] `size == 0` delta removes a level; removal of an absent level is a counted no-op.
- [ ] Gap detection per §4.2; recovery per §4.4 including buffered-delta replay, buffer-gap
      re-request, timeout re-request, and rate limiting — tests §9.3 (all variants) pass.
- [ ] Crossed-book handling per §4.5 with grace window; locked book treated as crossed; tests
      §9.4 pass.
- [ ] Staleness per §4.6 with warn/down thresholds; `FeedStatus` handling per §4.6.4; tests T1–T2
      pass.
- [ ] Sampled emission on the exact global 100 ms grid, unconditional when `OK`, integrity
      heartbeat when not `OK`, catch-up without flooding; test T4 passes.
- [ ] `BookState` is never emitted while the instrument's integrity state is not `OK`; emission
      ordering guarantees of §5.2 hold (test T7).
- [ ] `TradePrint` pass-through in all states, order preserved (test T3).
- [ ] `BookIntegrity` emitted on every transition of the §7.2 state machine and every tick while
      not `OK`.
- [ ] All failure modes G1–Q1 in §7.1 behave as specified; every counter listed exists on
      `core.counters`.
- [ ] Config parsed and validated per §6; engine refuses to start on any out-of-range value.
- [ ] Core is clock-free and deterministic: A2 replay test passes; asyncio wrapper adds no
      nondeterminism (A1).
- [ ] Performance benchmarks meet §8 targets on the reference host; memory bound verified with
      `tracemalloc`.
- [ ] No network, file, or clock access from `OrderBookEngineCore`; the only side effects are the
      returned emissions and the `request_snapshot` callback.
