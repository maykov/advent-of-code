# Block 7 — Session Scheduler & EOD Flattener

Status: specification, v1. Conforms to `docs/design.md` (§3 Block 7, §4 cross-cutting rules, §5 technology assumptions).

---

## 1. Overview

Block 7 owns **the trading calendar and the clock**. It is the sole authority on what phase
the trading session is in, and it is the component that **guarantees the day-trading
contract**: the book is flat before the exchange close, every day, no exceptions.

### 1.1 Responsibilities

1. Load and validate the exchange calendar; compute today's session (or the fact that there
   is none).
2. Publish the `SessionPhase` stream: `PRE_OPEN → OPEN_AUCTION → TRADING → WIND_DOWN →
   FORCE_FLAT → CLOSED`, with exact, deterministic transition times.
3. Enforce the escalating EOD flatten protocol: stop new entries, passive flatten,
   aggressive force-flat, verify flat against both the internal ledger (Block 5
   `PositionState`) and the broker reconciliation (Block 6).
4. Detect wall-clock vs. feed-heartbeat skew and degrade fail-safe (early `WIND_DOWN`,
   then early `FORCE_FLAT`).
5. Emit `EodReport{flat_confirmed, residual_positions}` to Block 9; a non-flat EOD is a
   critical alert.

### 1.2 Non-responsibilities

- Does **not** send orders to the broker (single-writer rule: only Block 6 does). It sends
  `FlattenCommand` messages; Blocks 5/6 act on them.
- Does **not** size, price, or route flattening orders — that is Block 6 tactic logic.
- Does **not** consume market data, features, signals, or PnL. No `BookState`, no
  `FeatureVector`, no `TradeIntent` inputs — ever.
- Does **not** maintain the position ledger; it only *reads* `PositionState` and broker
  reconciliation snapshots for verification.
- Does **not** decide holidays itself; the calendar file is the single source of truth.

### 1.3 Independence principle (normative)

Block 7 must keep working when every other online block is misbehaving. Therefore:

- **Inputs are limited to:** wall clock, monotonic clock, the calendar file, feed
  heartbeats (for skew detection only), `PositionState` from Block 5, and broker
  reconciliation from Block 6 (both used only for *verification*, never for phase logic).
- Phase transitions are a **pure function of time and calendar**. If Block 5 or Block 6 is
  down, phases still advance on schedule and `FlattenCommand`s are still emitted.
- No imports from Blocks 2–5 code. Only the shared message-type module and the event bus.
- Absence of verification data is treated as **not flat** (fail-safe direction, design §4.3).
- The implementation must fit in one module plus tests. Resist adding features here.

---

## 2. Session phase state machine

### 2.1 Phases

| Phase | Meaning | Consumers' obligations (informative) |
|---|---|---|
| `PRE_OPEN` | Before the opening auction. | Block 4 suppresses signals; Block 5 rejects orders. |
| `OPEN_AUCTION` | Opening auction + settle-in window. | Block 4 suppresses signals (no-trade zone). |
| `TRADING` | Normal continuous trading. | All blocks operate normally. |
| `WIND_DOWN` | No **new entries**; existing positions may be worked off. Passive flatten begins mid-phase. | Block 5 rejects position-increasing orders. |
| `FORCE_FLAT` | Cancel everything, market-order flat. | Block 5 rejects all non-flattening orders; Block 6 executes aggressive flatten. |
| `CLOSED` | Session over (or no session today). | Nothing trades. |

### 2.2 Transition times

Let `open` and `close` be today's session bounds from the calendar (§3), converted to UTC.
All offsets come from config (§8); defaults shown.

| Transition | Time | Default |
|---|---|---|
| `CLOSED → PRE_OPEN` | `open − pre_open_lead_min` | `open − 30 min` |
| `PRE_OPEN → OPEN_AUCTION` | `open` | `09:30` local |
| `OPEN_AUCTION → TRADING` | `open + open_auction_duration_min` | `open + 5 min` |
| `TRADING → WIND_DOWN` | `close − stop_new_entries_min` (**T−15**) | `close − 15 min` |
| *(within WIND_DOWN)* passive flatten starts | `close − passive_flatten_min` (**T−10**) | `close − 10 min` |
| `WIND_DOWN → FORCE_FLAT` | `close − force_flat_min` (**T−5**) | `close − 5 min` |
| `FORCE_FLAT → CLOSED` | `close` | `16:00` local |

Notes:

- The passive-flatten start (T−10) is **not** a phase boundary; it is an event inside
  `WIND_DOWN` (§5 step 2). There are three deadlines but only two late-phase boundaries.
- Transitions are one-directional along the chain, with one exception: skew detection
  (§4) may jump `TRADING → WIND_DOWN` (and schedule an early `FORCE_FLAT`) ahead of
  schedule. **No transition ever moves backwards.** Once `WIND_DOWN` or later is entered
  for a session date, earlier phases are unreachable for that date.
- Config validation (§8) must enforce `stop_new_entries_min > passive_flatten_min >
  force_flat_min > 0`, so the deadlines are strictly ordered.

### 2.3 Half days

A half day is a calendar entry with an early `close` (§3). **All close-relative offsets
apply to the early close unchanged.** Example (defaults, close 13:00): `WIND_DOWN` 12:45,
passive flatten 12:50, `FORCE_FLAT` 12:55, `CLOSED` 13:00. Open-relative times are
unaffected. `SessionPhase.is_half_day` is `True` all day.

### 2.4 Phase resolution — pure function

Implement one pure function and use it everywhere (startup, every tick, tests):

```python
def phase_for(now_utc: datetime, session: Session | None, cfg: SchedulerConfig) -> Phase:
    """Map a UTC instant to the scheduled phase. Pure; no I/O; no side effects.

    session is None => CLOSED (holiday, weekend, or no calendar coverage).
    Boundary rule: an instant exactly on a boundary belongs to the LATER phase
    (e.g. now == close - 15min  =>  WIND_DOWN).
    """
```

Skew-triggered early transitions (§4) are applied as an override *on top of* this function
(the scheduler keeps a `min_phase` floor per session date), never by editing the function.

### 2.5 Start / restart mid-session

On `start()` (whether first start of the day or a crash restart):

1. Load and validate the calendar; resolve today's `Session | None`.
2. Compute `phase = phase_for(now, session, cfg)` and broadcast it immediately with
   `reason="STARTUP_RESOLVE"` — consumers must never have to wait for the next boundary
   to learn the phase.
3. Fire any deadline actions that are already due but session-scoped state doesn't show as
   done. Because the process just started, it has no memory, so: if
   `now >= passive_flatten` time and phase is `WIND_DOWN`, emit `FlattenCommand(PASSIVE)`
   immediately; if phase is `FORCE_FLAT`, emit `FlattenCommand(AGGRESSIVE)` immediately
   and enter the aggressive retry loop (§5 step 3). Re-issuing a flatten that was already
   satisfied is harmless (Blocks 5/6 treat flattening an already-flat book as a no-op);
   re-issuing one that was lost in the crash is exactly the point.
4. If phase is `CLOSED` and `now >= close` and no `EodReport` has been journaled for
   today's session date (check the journal), run the terminal check (§5 step 4) and emit
   the report.

---

## 3. Exchange calendar

### 3.1 File format

One YAML file per venue per year (or multi-year), path set in config. Format:

```yaml
calendar_version: 1            # int, must equal 1
venue: XNYS                    # str, informational
timezone: America/New_York     # IANA tz-database name; resolved via zoneinfo.ZoneInfo
valid_from: 2026-01-01         # ISO date, inclusive
valid_to: 2026-12-31           # ISO date, inclusive
weekend: [SAT, SUN]            # days with no session; subset of MON..SUN
default_session:
  open:  "09:30"               # local wall time, HH:MM, 24h
  close: "16:00"
holidays:                      # full-day closures (dates with no session)
  - 2026-01-01                 # New Year's Day
  - 2026-07-03                 # Independence Day (observed)
  - 2026-11-26                 # Thanksgiving
  - 2026-12-25                 # Christmas
half_days:                     # early closes; open = default open
  - { date: 2026-11-27, close: "13:00" }
  - { date: 2026-12-24, close: "13:00" }
```

### 3.2 Timezone and DST rules (normative)

- `timezone` **must** be an IANA tz-database name resolved with
  `zoneinfo.ZoneInfo(name)` (Python 3.11 stdlib). Fixed offsets like `"UTC-5"` or
  abbreviations like `"EST"` are invalid — they break under DST.
- Session times in the file are **local wall times**. Convert to UTC per date:
  `datetime.combine(d, time, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)`, with
  `fold=0`.
- **All internal arithmetic, comparisons, journaling, and message timestamps are
  tz-aware UTC `datetime`s.** Local time exists only at the calendar-conversion boundary
  and in log formatting.
- DST transition days need no special code: converting each date's wall time through
  `ZoneInfo` yields the correct UTC instant on either side of a transition (e.g. 16:00
  New York = 21:00 UTC on 2026-03-06 but 20:00 UTC on 2026-03-09, after the 2026-03-08
  spring-forward). Validation must reject any session date whose `open` or `close` wall
  time is *nonexistent* or *ambiguous* on that date (detect via
  `dt.replace(fold=1).utcoffset() != dt.utcoffset()` for ambiguity, and round-trip
  UTC→local mismatch for nonexistence). US exchange times never hit this; the check is a
  guard against exotic calendars.

### 3.3 Validation on load

Loading must fail (raise `CalendarError` with a message naming the offending field) if any
of the following holds:

1. File missing, unreadable, not valid YAML, or not a mapping.
2. `calendar_version != 1`; any required key missing; any unknown top-level key present
   (strict schema — typos must not pass silently).
3. `timezone` not resolvable by `ZoneInfo` (`ZoneInfoNotFoundError`).
4. Any time not `HH:MM`; `close <= open` (default session or any half day);
   half-day `close >= default close`; half-day `close <= open`.
5. Any date outside `[valid_from, valid_to]`; duplicate dates within or across
   `holidays`/`half_days`; a date that is both holiday and half day; a holiday or half
   day that falls on a `weekend` day (contradiction).
6. `valid_to < valid_from`; validity window shorter than 7 days (almost certainly a typo).
7. Nonexistent/ambiguous wall time per §3.2 for any session date in the window.

### 3.4 Missing-day fail-safe (normative)

`session_for(d)` returns:

- `None` if `d` is a weekend day or holiday **within** `[valid_from, valid_to]` → normal
  `CLOSED` day, `reason="NO_SESSION"`, INFO log.
- **`CalendarCoverageError` if `d` is outside `[valid_from, valid_to]`.** The scheduler
  must then: stay/enter `CLOSED` with `reason="CALENDAR_MISSING"`, emit a **critical
  alert** to Block 9, and **refuse to ever publish `TRADING` (or any non-CLOSED phase)
  that day**. No calendar coverage ⇒ no trading. This is checked once at startup and at
  every local midnight rollover.

---

## 4. Clock integrity

### 4.1 Clock sources

- **Wall clock** — `Clock.now_utc()` (system UTC time). Used only to compare against
  calendar-derived UTC boundaries.
- **Monotonic clock** — `Clock.monotonic()` (`time.monotonic()`). Used for the tick
  loop, retry cadence, intervals, and heartbeat ageing. Never use wall-clock deltas for
  intervals: NTP steps would corrupt them.

### 4.2 Skew detection rule (normative)

Block 1 publishes `FeedStatus` heartbeats carrying `ts_exchange` (exchange stamp) and
`ts_local` (local receive stamp). Block 7 subscribes to this stream *solely* for skew
detection and records, per heartbeat: `(ts_exchange, mono_received = Clock.monotonic())`.

On every scheduler tick, if at least one heartbeat has been received:

```
age_s          = Clock.monotonic() - mono_received          # how stale the sample is
estimated_now  = ts_exchange + age_s                        # exchange time extrapolated
skew_s         = | Clock.now_utc() - estimated_now |
```

- **Trigger:** `skew_s > skew.threshold_s` (default **2.0 s**) on
  `skew.confirm_ticks` (default **3**) *consecutive* ticks. Consecutive confirmation
  suppresses one-off jitter.
- Heartbeats older than `skew.max_heartbeat_age_s` (default **30 s**) are ignored for
  skew (that is a feed-health problem, Block 1/9's job, not a clock problem). If phase is
  `TRADING` and no usable heartbeat has been seen for `skew.max_heartbeat_age_s`, log a
  WARNING; do not change phase (feed loss handling belongs to Block 5's kill switch).
- Skew evaluation is active only in `OPEN_AUCTION`, `TRADING`, and `WIND_DOWN`.

### 4.3 Fail-safe response (normative)

On confirmed skew while in `OPEN_AUCTION` or `TRADING`:

1. Immediately transition to `WIND_DOWN` with `reason="CLOCK_SKEW"` (this sets the phase
   floor; the schedule can never pull it back to `TRADING`).
2. Immediately emit `FlattenCommand(mode=PASSIVE, reason="CLOCK_SKEW")` — do not wait for
   the scheduled T−10.
3. Schedule `FORCE_FLAT` at `mono_now + skew.forceflat_delay_s` (default **120 s**) **or**
   the regularly scheduled `FORCE_FLAT` time, whichever is *earlier*.
4. Emit a critical alert to Block 9 with both clock readings and the computed skew.

On confirmed skew while already in `WIND_DOWN`: apply steps 3–4 only. In `FORCE_FLAT` or
`CLOSED`: log; no action. Skew never *delays* anything and never re-opens trading — once
skew-triggered, the session stays degraded until `CLOSED` even if skew disappears.

---

## 5. Escalating flatten protocol (normative)

All step times below are the scheduled defaults; under skew they compress per §4.3. Let
`T = close`.

**Step 1 — T−15:00: stop new entries.** Broadcast
`SessionPhase{phase=WIND_DOWN}`. No `FlattenCommand` yet. Block 5 is responsible for
rejecting position-increasing orders on seeing `WIND_DOWN` (its spec); Block 7 does not
verify this.

**Step 2 — T−10:00: passive flatten.** Emit
`FlattenCommand{mode=PASSIVE, deadline_ts=T−5:00, attempt=1}`. Then run the
**verification loop** every `flatten.passive_retry_interval_s` (default **10 s**):

- Read the latest `PositionState` snapshot (Block 5). *Ledger-flat* ⇔ every instrument
  qty == 0 **and** the snapshot is fresher than `flatten.position_max_age_s`
  (default **20 s**). A stale or absent snapshot is **not flat**.
- If not ledger-flat, re-emit `FlattenCommand{mode=PASSIVE, ...}` with `attempt`
  incremented and the same `deadline_ts`. (Re-emission is idempotent for Blocks 5/6;
  it covers lost/dropped commands.)
- If ledger-flat: stop re-emitting, but keep checking each interval until T−5 (a late
  fill could re-open a position). Phase remains `WIND_DOWN` regardless.

**Step 3 — T−5:00: aggressive force-flat.** Broadcast
`SessionPhase{phase=FORCE_FLAT}` and emit
`FlattenCommand{mode=AGGRESSIVE, deadline_ts=T, attempt=1}` — even if the ledger already
looks flat (belt and suspenders; an aggressive flatten of a flat book is a no-op that
also cancels stray resting orders). Semantics for Blocks 5/6: cancel **all** resting
orders, market-order every position to zero, reject anything else. Then every
`flatten.aggressive_retry_interval_s` (default **15 s**):

- Check ledger-flat as in step 2. If not flat, re-emit `AGGRESSIVE` with `attempt += 1`.
- Additionally request a broker reconciliation snapshot from Block 6
  (`BrokerReconSource.snapshot`, §7) so the terminal check has fresh data.

**Step 4 — T (close): terminal check and `EodReport`.** At `close`, broadcast
`SessionPhase{phase=CLOSED}` and evaluate:

- `ledger_flat` — per step 2's definition.
- `broker_flat` — the most recent broker reconciliation from Block 6 shows zero
  positions **and** is fresher than `flatten.recon_max_age_s` (default **30 s**). If
  Block 6 is unreachable or the snapshot is stale ⇒ `broker_flat = False`.
- `flat_confirmed = ledger_flat and broker_flat`. **Both** sources must agree on flat;
  either one non-flat, stale, or missing ⇒ not confirmed.

Emit `EodReport{session_date, flat_confirmed, residual_positions, ledger_flat,
broker_flat, broker_recon_ts, ts}` to Block 9 and the journal. `residual_positions`
lists every nonzero (instrument, qty, source ∈ {LEDGER, BROKER, BOTH}).

**Step 5 — non-flat at close: critical path.** If `flat_confirmed` is `False`:

1. Emit a **critical alert** (Block 9 alert channel, severity CRITICAL, human-pager
   class): `"EOD NOT FLAT"` with the residual positions. This alert is unconditional and
   must not be rate-limited away.
2. Keep re-emitting `FlattenCommand{mode=AGGRESSIVE}` and re-requesting broker recon
   every `flatten.post_close_retry_interval_s` (default **30 s**) until either flat is
   confirmed or `close + flatten.post_close_grace_min` (default **10 min**) is reached.
   (Some venues accept order cancels/closing trades briefly after the bell; if not,
   attempts fail harmlessly at Block 6.)
3. If flat is confirmed during the grace window, emit a **corrected** `EodReport` with
   `flat_confirmed=True` (both reports stay in the journal; Block 9 keeps the history).
4. If still not confirmed at the end of the grace window: emit a final `EodReport`
   (still `False`), emit a second CRITICAL alert `"EOD NOT FLAT — HUMAN ACTION
   REQUIRED"`, log the full residual detail, and stop retrying. **Resolution is now a
   human operator's job** (broker terminal / phone). The scheduler stays in `CLOSED` and
   does nothing further until the next session date.

---

## 6. Data structures

All messages are frozen dataclasses in the shared message module; all `datetime`s are
tz-aware UTC. These match the shapes in `docs/design.md` §3 Block 7, extended with fields
needed for ordering and audit.

```python
from __future__ import annotations
import enum
from dataclasses import dataclass, field
from datetime import date, datetime


class Phase(enum.Enum):
    PRE_OPEN = "PRE_OPEN"
    OPEN_AUCTION = "OPEN_AUCTION"
    TRADING = "TRADING"
    WIND_DOWN = "WIND_DOWN"
    FORCE_FLAT = "FORCE_FLAT"
    CLOSED = "CLOSED"


class FlattenMode(enum.Enum):
    PASSIVE = "PASSIVE"        # work out of positions with limit orders
    AGGRESSIVE = "AGGRESSIVE"  # cancel all resting orders; market-order flat


@dataclass(frozen=True, slots=True)
class SessionPhase:
    phase: Phase
    ts: datetime                    # UTC instant of this broadcast
    seconds_to_close: float         # >= 0.0; float("inf") when no session today
    session_date: date | None       # None when no session (holiday/weekend/missing)
    is_half_day: bool
    seq: int                        # per-process monotonically increasing, starts at 1
    reason: str                     # "SCHEDULED" | "CLOCK_SKEW" | "STARTUP_RESOLVE"
                                    # | "NO_SESSION" | "CALENDAR_MISSING" | "HEARTBEAT"


@dataclass(frozen=True, slots=True)
class FlattenCommand:
    mode: FlattenMode
    deadline_ts: datetime           # when the next escalation (or close) hits
    ts: datetime
    attempt: int                    # 1 on first emission, +1 per re-emission at same mode
    reason: str                     # "SCHEDULED" | "CLOCK_SKEW" | "STARTUP_RESOLVE"
                                    # | "POST_CLOSE"


@dataclass(frozen=True, slots=True)
class ResidualPosition:
    instrument: str
    qty: float                      # signed; != 0
    source: str                     # "LEDGER" | "BROKER" | "BOTH"


@dataclass(frozen=True, slots=True)
class EodReport:
    session_date: date
    flat_confirmed: bool
    residual_positions: tuple[ResidualPosition, ...]
    ledger_flat: bool
    broker_flat: bool
    broker_recon_ts: datetime | None   # None if Block 6 never answered
    ts: datetime
```

### 6.1 Broadcast guarantees (normative)

- The event bus (design §2/§5: in-process bounded `asyncio.Queue` per consumer,
  fan-out) must deliver **every `SessionPhase` transition to every subscribed consumer,
  in emission order**. Publishing uses a *blocking* `await queue.put(...)` — phase
  messages are low-rate and must never be dropped; if a consumer's queue is full the
  scheduler blocks on that consumer and logs a WARNING naming it after
  `bus.put_warn_after_s` (default 1.0 s).
- `seq` increases by exactly 1 per broadcast within a process run; consumers detect gaps
  (impossible under the blocking-put rule, but the check is cheap) and restarts
  (`seq` reset to 1).
- **Heartbeat re-broadcast:** every `phase_heartbeat_s` (default **30 s**) the current
  phase is re-published with fresh `ts`/`seconds_to_close`, `reason="HEARTBEAT"`, and the
  next `seq`. Late-joining or restarted consumers therefore converge within 30 s even if
  they missed the transition. Consumers must treat phase messages idempotently (act on
  phase value, not on message arrival).
- Every `SessionPhase`, `FlattenCommand`, and `EodReport` is journaled (design §4.1)
  before being put on the bus.
- `FlattenCommand` routing: delivered to both Block 5 and Block 6 subscribers.
  `EodReport` to Block 9.

---

## 7. Public API and asyncio model

Module: `bot/session/scheduler.py` (plus `bot/session/calendar.py`, `bot/session/clock.py`).

```python
from typing import Protocol
from datetime import date, datetime


class Clock(Protocol):
    """All time access goes through this. Production: SystemClock. Tests: FakeClock."""
    def now_utc(self) -> datetime: ...          # tz-aware UTC wall clock
    def monotonic(self) -> float: ...           # time.monotonic()
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now_utc(self) -> datetime: ...          # datetime.now(timezone.utc)
    def monotonic(self) -> float: ...           # time.monotonic()
    async def sleep(self, seconds: float) -> None: ...   # asyncio.sleep


@dataclass(frozen=True, slots=True)
class Session:
    session_date: date
    open_utc: datetime
    close_utc: datetime
    is_half_day: bool


class ExchangeCalendar:
    @classmethod
    def load(cls, path: str) -> "ExchangeCalendar":
        """Parse + validate per §3.3. Raises CalendarError on any violation."""
    def session_for(self, d: date) -> Session | None:
        """None = no session (weekend/holiday). Raises CalendarCoverageError
        if d is outside [valid_from, valid_to]."""


class PositionSource(Protocol):
    """Adapter over the Block 5 PositionState stream (bus subscriber that caches
    the latest snapshot). Non-blocking read."""
    def latest(self) -> tuple[dict[str, float], datetime] | None:
        """({instrument: signed_qty}, snapshot_ts) or None if never received."""


class BrokerReconSource(Protocol):
    """Adapter over Block 6 broker reconciliation (request/response)."""
    async def snapshot(self, timeout_s: float) -> tuple[dict[str, float], datetime] | None:
        """({instrument: signed_qty}, recon_ts) or None on timeout/unreachable.
        Must not raise; failures return None."""


class HeartbeatSource(Protocol):
    """Adapter over Block 1 FeedStatus heartbeats."""
    def latest(self) -> tuple[datetime, float] | None:
        """(ts_exchange, mono_received) of the newest heartbeat, or None."""


class SessionScheduler:
    def __init__(
        self,
        config: SchedulerConfig,
        bus: EventBus,                     # shared bus abstraction (design §2)
        clock: Clock,
        positions: PositionSource,
        broker_recon: BrokerReconSource,
        heartbeats: HeartbeatSource,
    ) -> None: ...

    async def start(self) -> None:
        """Load calendar, resolve current phase (§2.5), broadcast it, then run the
        tick loop until stop(). Raises CalendarError / ConfigError before any
        broadcast if validation fails (process must not come up half-configured)."""

    async def stop(self) -> None:
        """Graceful shutdown: cancel the tick task, flush pending journal writes.
        Does NOT flatten (stopping the scheduler is an operator action; the
        operator owns the consequences and gets a WARNING log saying so)."""

    @property
    def current_phase(self) -> SessionPhase:
        """Latest broadcast SessionPhase (for pull-style consumers/tests)."""

    def phase_for(self, now_utc: datetime, session: Session | None) -> Phase:
        """Pure schedule mapping per §2.4 (exposed for tests and Block 8 replay)."""
```

### 7.1 Tick loop (normative)

One asyncio task; no other concurrency inside the block (broker-recon requests are
awaited inline with `flatten.recon_timeout_s`).

- **Tick interval:** `tick_interval_ms`, default **250 ms** (range 50–1000). Every tick:
  (1) midnight rollover check → re-resolve session; (2) skew evaluation (§4);
  (3) compute target phase = max(scheduled phase, skew floor); if it differs from
  current, broadcast the transition(s) — if more than one boundary was crossed (long GC
  pause, laptop sleep), broadcast **each intermediate phase in order**, oldest first, so
  consumers see every transition; (4) run due flatten-protocol actions (§5);
  (5) heartbeat re-broadcast if due.
- **Drift correction:** fixed-rate schedule on the monotonic clock —
  `next_tick = t0 + n * interval`; each iteration `await
  clock.sleep(max(0.0, next_tick - clock.monotonic()))`; increment `n` past any missed
  ticks rather than sleeping a fixed interval after work (no cumulative drift; late
  ticks don't cause a burst of catch-up ticks).
- Worst-case action lateness is one tick (≤ 1 s at the max interval); all deadline
  comparisons are `now >= deadline`, so actions fire on the first tick at/after the
  deadline.

### 7.2 Lifecycle

Construction is side-effect-free. `start()` is awaited by the process supervisor after
the bus exists but requires no other block to be up (adapters return `None` until their
sources speak). `stop()` is idempotent. There is no pause/resume.

---

## 8. Configuration

Section `scheduler:` of the run config (design §4.4: config as code, journaled with the
run). Loading validates every field against the ranges below and the cross-field rules;
any violation ⇒ `ConfigError` at startup, process refuses to start.

```yaml
scheduler:
  calendar_path: config/calendar/xnys-2026.yaml   # str, file must exist
  timezone: America/New_York    # str, IANA name; MUST equal the calendar's timezone
                                #   (cross-check at load; mismatch => ConfigError)
  tick_interval_ms: 250         # int, [50, 1000]
  phase_heartbeat_s: 30         # int, [5, 300]

  offsets:
    pre_open_lead_min: 30           # int, [5, 120]
    open_auction_duration_min: 5    # int, [0, 30]
    stop_new_entries_min: 15        # int, [5, 60]    (T-X)
    passive_flatten_min: 10         # int, [3, 55]    (T-Y)
    force_flat_min: 5               # int, [1, 30]    (T-Z)
    # cross-field: stop_new_entries_min > passive_flatten_min > force_flat_min
    # cross-field: on the shortest session in the calendar window,
    #   open + open_auction_duration < close - stop_new_entries_min
    #   (i.e. TRADING is non-empty even on half days)

  skew:
    threshold_s: 2.0            # float, [0.5, 30.0]
    confirm_ticks: 3            # int, [1, 20]
    forceflat_delay_s: 120      # int, [10, 600]
    max_heartbeat_age_s: 30     # int, [5, 300]

  flatten:
    passive_retry_interval_s: 10      # int, [2, 60]
    aggressive_retry_interval_s: 15   # int, [2, 60]
    post_close_retry_interval_s: 30   # int, [5, 120]
    post_close_grace_min: 10          # int, [1, 60]
    position_max_age_s: 20            # int, [5, 120]
    recon_max_age_s: 30               # int, [5, 120]
    recon_timeout_s: 5.0              # float, [0.5, 30.0]

  bus:
    put_warn_after_s: 1.0       # float, [0.1, 10.0]
```

`SchedulerConfig` is a frozen dataclass mirroring this tree, with `from_mapping(...)`
performing all validation and raising `ConfigError` with the full dotted path of the bad
field (e.g. `scheduler.offsets.passive_flatten_min`).

---

## 9. Error handling

| # | Failure | Detection | Response |
|---|---|---|---|
| 1 | Calendar file missing/corrupt/invalid at startup | `ExchangeCalendar.load` raises `CalendarError` | `start()` re-raises; **process does not come up**; no phase is ever broadcast. Fail-safe: nothing trades without a valid calendar. |
| 2 | Today outside calendar coverage | `CalendarCoverageError` from `session_for` | `CLOSED` all day, `reason="CALENDAR_MISSING"`, CRITICAL alert to Block 9 at startup and at each midnight rollover (§3.4). Never `TRADING`. |
| 3 | Timezone misconfig (unknown IANA name, or config tz ≠ calendar tz) | `ZoneInfoNotFoundError` / cross-check in config load | `ConfigError`/`CalendarError` at startup; process refuses to start. |
| 4 | Offsets out of range or mis-ordered | Config validation §8 | `ConfigError` at startup; process refuses to start. |
| 5 | Process restart during `FORCE_FLAT` | §2.5 startup resolution | Phase re-resolved to `FORCE_FLAT` from clock+calendar alone; immediate re-broadcast + immediate `FlattenCommand(AGGRESSIVE, reason="STARTUP_RESOLVE", attempt=1)`; aggressive retry loop resumes. No persisted state needed. |
| 6 | Restart after close, before `EodReport` written | §2.5 step 4 (journal check) | Run terminal check late; emit `EodReport`; if not flat and inside grace window, run step 5 of §5. |
| 7 | Block 6 unreachable during flatten / recon timeout | `BrokerReconSource.snapshot` returns `None` | `broker_flat=False` ⇒ `flat_confirmed=False` (fail-safe: unverifiable ≠ flat). Keep re-requesting each retry interval; WARNING per failure, CRITICAL via the normal §5 step 5 path at close. |
| 8 | `PositionState` absent or older than `position_max_age_s` | Freshness check §5 step 2 | Treated as **not flat**; passive/aggressive re-emission continues; WARNING log naming the staleness. |
| 9 | Feed heartbeats absent (no skew data) | `HeartbeatSource.latest()` `None`/stale | Skew check skipped; WARNING if in `TRADING` (§4.2). Phase logic unaffected — clock+calendar alone still enforce EOD. |
| 10 | Confirmed clock skew | §4.2 rule | Early `WIND_DOWN` → early `FORCE_FLAT` per §4.3; CRITICAL alert. |
| 11 | Consumer queue full on phase broadcast | Blocking put exceeds `put_warn_after_s` | Scheduler blocks (guarantee §6.1) and logs WARNING with the consumer name; slow consumers are an ops problem, dropped phases are not acceptable. |
| 12 | Tick loop overslept across ≥ 1 boundary (GC, machine sleep) | `phase_for` on next tick differs by > 1 step | Broadcast every intermediate phase in order, then fire all due flatten actions (§7.1). |
| 13 | Still not flat at `close + post_close_grace_min` | §5 step 5 | Final `EodReport{flat_confirmed=False}`, second CRITICAL "HUMAN ACTION REQUIRED" alert, stop retrying, remain `CLOSED`. |

---

## 10. Test plan

All tests inject `FakeClock` and in-memory fakes for `PositionSource`,
`BrokerReconSource`, `HeartbeatSource`, and the bus. **No test may touch the system
clock or `asyncio.sleep`.**

### 10.1 FakeClock interface

```python
class FakeClock:
    """Deterministic clock. Wall and monotonic advance together via advance();
    skew_wall() moves the wall clock alone to simulate NTP steps / skew."""

    def __init__(self, start_utc: datetime) -> None: ...
    def now_utc(self) -> datetime: ...
    def monotonic(self) -> float: ...                 # starts at 0.0
    async def sleep(self, seconds: float) -> None:
        """Suspend the caller until advance() has moved the clock past its wakeup
        time. Never blocks the test: pending sleeps are stored as (wakeup, future)."""
    def advance(self, seconds: float) -> None:
        """Move BOTH clocks forward, waking sleepers in wakeup order, one at a
        time, running the event loop between wakeups (deterministic ordering)."""
    def skew_wall(self, seconds: float) -> None:
        """Shift now_utc() by `seconds` WITHOUT moving monotonic (skew injection)."""
    async def run_until(self, target_utc: datetime) -> None:
        """advance() in tick-sized steps until now_utc() >= target_utc."""
```

Test heartbeat fake: `FakeHeartbeats.emit(ts_exchange)` records
`(ts_exchange, clock.monotonic())`. Position fake: `FakePositions.set({"AAPL": 100})`
stamps `clock.now_utc()`. Recon fake: `FakeRecon.set(positions | None)`; `snapshot()`
returns it stamped, or `None`.

Calendar fixture for all tests (written to a tmp file):
`timezone: America/New_York`, default session 09:30–16:00, `valid_from: 2026-01-01`,
`valid_to: 2026-12-31`, holidays `[2026-07-03]`, half_days
`[{date: 2026-11-27, close: "13:00"}]`, weekend `[SAT, SUN]`. Default config §8.

### 10.2 Tests

**T1 — Full-day timeline, normal day (2026-07-02, Thu; EDT = UTC−4).**
Start FakeClock at `2026-07-02 08:00:00 ET = 12:00:00Z`. `run_until` end of day and
assert the exact broadcast sequence (phase, ET time, UTC time):

| seq event | ET | UTC |
|---|---|---|
| `CLOSED` (startup resolve) | 08:00:00 | 12:00:00Z |
| `PRE_OPEN` | 09:00:00 | 13:00:00Z |
| `OPEN_AUCTION` | 09:30:00 | 13:30:00Z |
| `TRADING` | 09:35:00 | 13:35:00Z |
| `WIND_DOWN` | 15:45:00 | 19:45:00Z |
| `FlattenCommand{PASSIVE, deadline=19:55:00Z}` | 15:50:00 | 19:50:00Z |
| `FORCE_FLAT` + `FlattenCommand{AGGRESSIVE, deadline=20:00:00Z}` | 15:55:00 | 19:55:00Z |
| `CLOSED` + `EodReport` | 16:00:00 | 20:00:00Z |

Positions flat throughout; recon returns `{}`. Assert `flat_confirmed=True`,
`seconds_to_close` correct at each transition (e.g. 900.0 at `WIND_DOWN`), `seq`
strictly `1, 2, 3, ...` with heartbeat re-broadcasts interleaved every 30 s, timestamps
exact to the tick (≤ 250 ms after the boundary; with the fake clock, exactly on it).

**T2 — Half day (2026-11-27, Fri; EST = UTC−5, close 13:00 ET).**
Same as T1 with: `WIND_DOWN` 12:45:00 ET = 17:45:00Z, `PASSIVE` 12:50 (17:50Z),
`FORCE_FLAT` + `AGGRESSIVE` 12:55 (17:55Z), `CLOSED` 13:00 (18:00Z).
`is_half_day=True` on every `SessionPhase`. Note the UTC offset differs from T1 (EST vs
EDT) — this also exercises §3.2 DST conversion.

**T3 — Mid-session starts.** Three sub-cases, all on 2026-07-02:
(a) start at 15:47:00 ET → first broadcast is `WIND_DOWN`
(`reason="STARTUP_RESOLVE"`, `seconds_to_close=780.0`), **no** flatten yet; `PASSIVE`
fires at 15:50:00.
(b) start at 15:52:00 ET → `WIND_DOWN` broadcast **and** immediate
`FlattenCommand{PASSIVE}` (T−10 already past).
(c) start at 15:57:00 ET with `FakePositions.set({"AAPL": 100})` → `FORCE_FLAT`
broadcast and immediate `FlattenCommand{AGGRESSIVE, reason="STARTUP_RESOLVE"}`; retries
at 15:57:15, 15:57:30, … until positions set to `{}`.

**T4 — Skew-triggered early wind-down.** 2026-07-02, in `TRADING` at 14:00:00 ET.
Heartbeats emitted every 1 s matching wall clock (skew ≈ 0). At 14:00:00 call
`clock.skew_wall(+5.0)` (wall now 5 s ahead of feed time). With `threshold_s=2.0`,
`confirm_ticks=3`, tick 250 ms: skew confirmed on the 3rd consecutive tick →
at wall-clock 14:00:05.75 ET (3 ticks after the step) assert, in order:
`SessionPhase{WIND_DOWN, reason="CLOCK_SKEW"}`, `FlattenCommand{PASSIVE,
reason="CLOCK_SKEW"}`, CRITICAL alert. Then `FORCE_FLAT` +
`FlattenCommand{AGGRESSIVE}` exactly `forceflat_delay_s=120` s later (≈ 14:02:05.75 ET,
monotonic-based — assert via monotonic delta, not wall). Also assert: two ticks of
skew followed by one clean tick does **not** trigger (consecutive rule); skew
disappearing after trigger does **not** restore `TRADING`.

**T5 — Flatten-verification failure at close.** 2026-07-02.
`FakePositions.set({"AAPL": 100.0})` from 15:40 onward; `FakeRecon.set({"AAPL": 100.0})`.
Assert: `PASSIVE` at 19:50:00Z then re-emitted every 10 s (19:50:10, 19:50:20, …,
attempt increments); `AGGRESSIVE` at 19:55:00Z re-emitted every 15 s; at 20:00:00Z
`EodReport{flat_confirmed=False, ledger_flat=False, broker_flat=False,
residual_positions=[("AAPL", 100.0, "BOTH")]}` + CRITICAL alert; post-close `AGGRESSIVE`
at 20:00:30Z, 20:01:00Z, …; at 20:04:00Z set both fakes flat → corrected
`EodReport{flat_confirmed=True}` on the next retry check and retries stop. Variant
T5b: never flat → final report at 20:10:00Z, "HUMAN ACTION REQUIRED" alert, no further
commands afterwards.

**T6 — Broker recon unreachable.** As T1 but `FakeRecon.set(None)` (snapshot → `None`)
and ledger flat. Assert `EodReport{flat_confirmed=False, ledger_flat=True,
broker_flat=False, broker_recon_ts=None}` and the critical path of §5 step 5 runs.
Variant: recon returns data stamped 40 s old (> `recon_max_age_s=30`) → same outcome.

**T7 — Stale ledger.** In step 2, `FakePositions` last stamped 25 s ago
(> `position_max_age_s=20`) with qty 0 → treated as not flat; `PASSIVE` re-emitted.

**T8 — Calendar fail-safes.** (a) Start on 2026-07-03 (holiday) → `CLOSED` all day,
`reason="NO_SESSION"`, no alert. (b) Start on 2027-01-04 (outside `valid_to`) →
`CLOSED`, `reason="CALENDAR_MISSING"`, CRITICAL alert, and asserting over a simulated
full day that no non-CLOSED phase is ever broadcast. (c) Corrupt file (truncated YAML),
unknown tz, `close <= open`, duplicate date, half-day close ≥ default close: each makes
`ExchangeCalendar.load` raise `CalendarError` naming the field, and `start()` propagate
it before any broadcast.

**T9 — Config validation.** `passive_flatten_min=20 > stop_new_entries_min=15` →
`ConfigError`; each range bound in §8 checked with one below-min and one above-max case;
config tz ≠ calendar tz → error.

**T10 — Missed-tick catch-up.** In `TRADING` at 15:40 ET, `advance(1800)` in one call
(simulated 30-min stall). Next tick must broadcast `WIND_DOWN`, then `FORCE_FLAT`, then
`CLOSED` in that order (three messages, consecutive `seq`), fire `PASSIVE` and
`AGGRESSIVE` once each, and run the terminal check.

**T11 — Tick drift.** With real `SystemClock` semantics simulated on FakeClock: after
1000 ticks where each tick's work consumes 100 ms, ticks still fire at
`t0 + n·0.25 s` (fixed-rate schedule, §7.1) — assert no cumulative drift.

**T12 — Broadcast/heartbeat guarantees.** Two subscribed fake consumers; run T1;
assert both saw identical ordered sequences; heartbeat re-broadcasts every 30 s carry
current phase and increasing `seq`; a consumer subscribing at 12:00 ET receives the
current phase within `phase_heartbeat_s`.

**T13 — DST conversion.** Using the calendar fixture, assert
`session_for(2026-03-06).close_utc == 21:00Z` and
`session_for(2026-03-09).close_utc == 20:00Z` (US spring-forward on 2026-03-08).

---

## 11. Acceptance criteria

- [ ] `ExchangeCalendar.load` enforces every rule in §3.3; all T8/T13 cases pass.
- [ ] Today outside calendar coverage ⇒ `CLOSED` + `reason="CALENDAR_MISSING"` +
      CRITICAL alert; no non-CLOSED phase possible that day.
- [ ] `phase_for` is pure, matches the §2.2 table exactly (boundary instants belong to
      the later phase), and is exported for Block 8 replay.
- [ ] Full-day and half-day timelines produce exactly the T1/T2 sequences and timestamps.
- [ ] Startup at any instant resolves the correct phase and re-fires any already-due
      flatten actions (T3 a–c), with no persisted state required.
- [ ] Skew rule implemented exactly per §4.2 (threshold, consecutive-tick confirmation,
      heartbeat ageing on the monotonic clock); response per §4.3; T4 passes.
- [ ] Flatten protocol follows §5 steps 1–5: correct commands, deadlines, retry cadences,
      dual-source flat verification with freshness rules, corrected reports, post-close
      grace, and human-escalation alert (T5, T5b, T6, T7).
- [ ] Missing/stale `PositionState` or broker recon is treated as **not flat**; Block 6
      unreachable ⇒ `flat_confirmed=False` (T6, T7).
- [ ] Dataclasses match §6 exactly; all timestamps tz-aware UTC; every message journaled
      before bus publish.
- [ ] Broadcast guarantees hold: blocking puts, per-broadcast `seq`, intermediate phases
      on catch-up, 30 s heartbeat re-broadcast (T10, T12).
- [ ] Tick loop uses the monotonic fixed-rate schedule with drift correction (T11); all
      intervals/retries measured on the monotonic clock.
- [ ] All config fields validated with the §8 ranges and cross-field rules (T9); invalid
      config or calendar prevents startup before any broadcast.
- [ ] Zero imports from Blocks 2–5 implementation code; inputs limited to §1.3's list.
- [ ] Entire test suite runs on `FakeClock` (§10.1) with no real sleeping; suite
      completes in seconds.
