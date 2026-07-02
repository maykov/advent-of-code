# Block 1 — Market Data Gateway: Implementation Specification

Status: v1.0 — implementable. Conforms to `docs/design.md` (Block 1 section, §4 cross-cutting rules, §5 technology assumptions).

---

## 1. Overview and responsibilities

The Market Data Gateway (MDG) is the **only** component in the system that talks to external
market-data APIs (design §4.2, single-writer rule). It runs as a set of asyncio tasks inside the
single v1 process.

### 1.1 Responsibilities

1. Connect to each configured venue's market-data API (websocket for streams, REST for snapshots
   and reference data) through a venue-specific `MarketDataAdapter`.
2. Subscribe to L2 order-book deltas, book snapshots, trade prints, and session status for the
   configured instrument universe (including the benchmark instrument used by Block 9).
3. Normalize vendor messages into the internal event types `BookSnapshot`, `BookDelta`,
   `TradePrint` (adapter's job) and stamp each with a local receive timestamp (core's job).
4. Detect per-instrument sequence gaps and drive snapshot recovery.
5. Detect staleness, disconnects, and clock skew; publish `FeedStatus` transitions.
6. Reconnect automatically with exponential backoff and full resubscription + re-snapshot.
7. Archive the raw wire stream verbatim (for Block 8 replay and adapter debugging) and, at end of
   session, compact normalized events to Parquet.
8. Publish all normalized events onto the internal event bus in per-instrument sequence order.

### 1.2 Explicit non-responsibilities

The MDG does **not**:

- Maintain an order-book replica or apply deltas to snapshots (Block 2).
- Detect crossed books or validate book *consistency* beyond per-message field validation
  (Block 2 emits `BookIntegrity`).
- Compute features, mid, microprice, or any derived quantity (Blocks 2–3).
- Send orders, receive fills, or talk to any order API (Block 6).
- Decide what the system does in response to feed problems (Blocks 5 and 7 react to
  `FeedStatus`; the MDG only reports and recovers).
- Publish `SessionPhase` (Block 7). The MDG forwards venue *session status* messages only as
  `FeedStatus.detail` context; it is not the trading-calendar authority.
- Write to the event journal directly — the bus layer journals every published message
  (design §2); the MDG's own archive is the *raw* wire stream, a separate artifact.

---

## 2. Data structures

All types live in `mdg/events.py`. Rules that apply to every event type:

- Timestamps are `int` nanoseconds since the Unix epoch, UTC. Suffix conventions:
  `ts_exchange` = venue-assigned event time; `ts_local` = `time.time_ns()` captured by the MDG
  core at the moment the raw frame is received from the transport, **before** parsing.
- `instrument` is the internal symbol (config key), not the vendor symbol. Adapters map
  vendor → internal via the `symbol_map` in config.
- `seq` is a per-instrument, per-connection monotonically increasing `int ≥ 0` produced by the
  adapter (mapped from the venue's own sequencing scheme; see §5.1 requirement A4). `seq`
  ordering is meaningful **only within one instrument**.
- Prices and sizes are `float` (binary64). Prices are in the instrument's quote currency; sizes
  in base units (shares/contracts). Adapters must round prices to the instrument's `tick_size`
  and sizes to `lot_size` before constructing events.
- All dataclasses are `@dataclass(frozen=True, slots=True)`. Construction-time validation is in
  `__post_init__` and raises `ValueError`; the MDG treats a `ValueError` from event construction
  as a *malformed vendor message* (error table §8, row E6).

```python
from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: float          # > 0, multiple of tick_size (adapter-rounded)
    size: float           # > 0 in snapshots; levels with size 0 must be omitted from snapshots

    def __post_init__(self) -> None:
        if not (self.price > 0.0):
            raise ValueError(f"price must be > 0, got {self.price}")
        if not (self.size > 0.0):
            raise ValueError(f"size must be > 0, got {self.size}")


class Side(str, Enum):
    BID = "bid"
    ASK = "ask"


class AggressorSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"   # venue does not disclose aggressor


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    instrument: str                     # internal symbol, non-empty
    ts_exchange: int                    # ns since epoch UTC; 0 if venue omits (never negative)
    ts_local: int                       # ns since epoch UTC; > 0 always (stamped by MDG core)
    bids: tuple[PriceLevel, ...]        # sorted by price DESCENDING, no duplicate prices
    asks: tuple[PriceLevel, ...]        # sorted by price ASCENDING, no duplicate prices
    seq: int                            # >= 0; deltas with seq <= this are already included

    def __post_init__(self) -> None:
        if not self.instrument:
            raise ValueError("instrument must be non-empty")
        if self.ts_exchange < 0 or self.ts_local <= 0 or self.seq < 0:
            raise ValueError("bad ts/seq")
        for lvls, desc in ((self.bids, True), (self.asks, False)):
            prices = [l.price for l in lvls]
            ordered = sorted(prices, reverse=desc)
            if prices != ordered or len(set(prices)) != len(prices):
                raise ValueError("levels must be sorted and unique")


@dataclass(frozen=True, slots=True)
class BookDelta:
    instrument: str
    ts_exchange: int                    # ns; 0 if venue omits
    ts_local: int                       # ns; > 0
    side: Side
    price: float                        # > 0
    size: float                         # NEW ABSOLUTE size at this level; 0.0 means level removed
    seq: int                            # >= 0

    def __post_init__(self) -> None:
        if not self.instrument:
            raise ValueError("instrument must be non-empty")
        if self.ts_exchange < 0 or self.ts_local <= 0 or self.seq < 0:
            raise ValueError("bad ts/seq")
        if not (self.price > 0.0) or self.size < 0.0:
            raise ValueError("bad price/size")


@dataclass(frozen=True, slots=True)
class TradePrint:
    instrument: str
    ts_exchange: int                    # ns; 0 if venue omits
    ts_local: int                       # ns; > 0
    price: float                        # > 0
    size: float                         # > 0
    aggressor_side: AggressorSide
    seq: int                            # >= 0; shares the instrument's seq space with deltas

    def __post_init__(self) -> None:
        if not self.instrument:
            raise ValueError("instrument must be non-empty")
        if self.ts_exchange < 0 or self.ts_local <= 0 or self.seq < 0:
            raise ValueError("bad ts/seq")
        if not (self.price > 0.0) or not (self.size > 0.0):
            raise ValueError("bad price/size")


class FeedState(str, Enum):
    LIVE = "LIVE"     # connected, sequenced, fresh
    STALE = "STALE"   # connected, but no message for stale_after_ms (or clock skew breach)
    GAP = "GAP"       # sequence gap detected; snapshot recovery in progress
    DOWN = "DOWN"     # transport disconnected or subscription failed; reconnecting


@dataclass(frozen=True, slots=True)
class FeedStatus:
    instrument: str                     # internal symbol, or "*" for venue-wide transitions
    state: FeedState
    detail: str                         # machine-parseable "code: human text", codes in §7.3
    ts_local: int                       # ns; when the transition was decided

    def __post_init__(self) -> None:
        if not self.instrument or self.ts_local <= 0:
            raise ValueError("bad instrument/ts_local")
```

Semantics that Block 2 relies on (normative):

- A `BookSnapshot` is the complete visible book at `seq`. Deltas with `seq <= snapshot.seq` must
  be discarded by consumers; the MDG already filters these during recovery (§4.5) but Block 2
  must also enforce it (defense in depth).
- `BookDelta.size` is the **absolute** new size at `(side, price)` — never an increment.
- Between two `FeedStatus(state=GAP)` and the next `BookSnapshot` for the same instrument, no
  `BookDelta`/`TradePrint` for that instrument is published (the MDG buffers; §4.5).
- Events for one instrument are published in strictly increasing `seq` order, with exactly one
  exception: a recovery `BookSnapshot` restarts the sequence baseline.

---

## 3. Public API

Module layout:

```
mdg/
  events.py        # §2 dataclasses
  adapter.py       # MarketDataAdapter ABC, RawFrame, AdapterEvent (§5)
  gateway.py       # MarketDataGateway, InstrumentFeedState machine
  archive.py       # RawArchiver, ParquetCompactor
  config.py        # GatewayConfig loader + validation
  sim_adapter.py   # SimulatedFeedAdapter (§5.2)
```

### 3.1 Bus interface consumed

```python
from typing import Protocol, Union

GatewayEvent = Union[BookSnapshot, BookDelta, TradePrint, FeedStatus]

class EventPublisher(Protocol):
    def publish(self, event: GatewayEvent) -> None:
        """Non-blocking. Enqueue onto the in-process bus; the bus journals it.
        Raises BusFull if the bounded queue is full (MDG handles per §8 row E8)."""
```

### 3.2 Gateway class

```python
class MarketDataGateway:
    def __init__(
        self,
        config: GatewayConfig,                       # parsed + validated from TOML (§6)
        adapters: dict[str, MarketDataAdapter],      # venue name -> constructed adapter
        bus: EventPublisher,
        clock: Callable[[], int] = time.time_ns,     # injectable for tests
    ) -> None: ...

    async def start(self) -> None:
        """Idempotent. Spawns, per venue: a connection task (§4.1) and a staleness
        watchdog task (§4.6); plus one shared archiver thread (§4.8).
        Returns once all tasks are spawned (NOT once feeds are LIVE).
        Initial FeedStatus DOWN('startup: connecting') is published for every
        instrument before this returns."""

    async def stop(self) -> None:
        """Graceful shutdown, must complete within config.gateway.stop_timeout_s:
        1. cancel watchdogs; 2. adapter.disconnect() per venue; 3. cancel connection
        tasks; 4. flush + close archive files; 5. publish FeedStatus DOWN
        ('shutdown: gateway stopped') for every instrument. Idempotent."""

    def feed_state(self, instrument: str) -> FeedState:
        """Current state; raises KeyError for unknown instrument. Thread-safe read."""

    def stats(self) -> dict[str, int]:
        """Monotonic counters for Block 9 scraping: messages_received, events_published,
        gaps_detected, reconnects, malformed_dropped, archive_bytes_written,
        archive_frames_dropped, bus_publish_failures."""
```

### 3.3 Concurrency model (normative)

- Pure asyncio on the process's single event loop, **plus exactly one** daemon thread for archive
  file I/O (§4.8). No other threads.
- Per venue: `_run_connection(venue)` task (owns the adapter, does receive→normalize→sequence→
  publish inline, no intermediate queues on the hot path) and `_watchdog(venue)` task (500 ms
  tick). One additional task per in-flight snapshot request (§4.5), at most one per instrument.
- Per-instrument state (`last_seq`, `FeedState`, gap buffer, skew EWMA) lives in a
  `dict[str, _InstrumentState]` touched only from the event loop — no locks needed.
- The archive thread communicates via a bounded `queue.Queue[RawFrame]`
  (maxsize = `archive.queue_max_frames`); the event loop side uses `put_nowait` only.

---

## 4. Detailed behavior

### 4.1 Connection task — normal operation

Per venue, `_run_connection` loops forever until cancelled:

1. Publish `FeedStatus(instrument="*", DOWN, "connect: attempting", ts_local=clock())` if not
   already DOWN.
2. `await adapter.connect()`; on failure go to §4.2 backoff, continue loop.
3. `await adapter.subscribe(instruments)` for all instruments of this venue; on failure:
   `await adapter.disconnect()`, go to §4.2 backoff, continue loop.
4. For each instrument: request an initial snapshot (§4.5 steps 3–6, entered directly from
   startup; state goes DOWN → GAP(`"startup: awaiting initial snapshot"`) → LIVE).
5. Loop: `async for raw, event in adapter.stream():`
   a. `ts_local = clock()` — captured immediately, used for this frame's stamping.
   b. Hand `raw` (a `RawFrame`, §5.1) to the archiver queue (`put_nowait`; on `queue.Full`
      increment `archive_frames_dropped` and continue — archiving must never block the hot path).
   c. If `event is None` (heartbeat / admin frame): update `last_msg_ts_local` for the venue and
      continue.
   d. If parsing raised (adapter yields `AdapterParseError` via exception): §8 row E6.
   e. Stamp: rebuild the event with `ts_local` set (adapter leaves `ts_local=0` sentinel;
      the core uses `dataclasses.replace(event, ts_local=ts_local)`).
   f. Update clock-skew estimate (§4.7).
   g. Run the sequence check (§4.4). Outcome ∈ {publish, drop-duplicate, enter-gap-recovery,
      buffer-during-recovery}.
   h. On publish: `bus.publish(event)`; if instrument was STALE, transition STALE → LIVE with
      `FeedStatus("recovered: message flow resumed")`.
   i. Update `last_msg_ts_local` (per venue and per instrument).
6. When `adapter.stream()` raises `ConnectionLost` or ends: publish
   `FeedStatus("*", DOWN, "disconnect: <reason>")`, mark all venue instruments DOWN, clear their
   gap buffers, then go to §4.2 backoff and continue the loop from step 2.

### 4.2 Reconnect with exponential backoff

State per venue: `attempt: int` (starts 0), `connected_since: int | None`.

1. `delay_s = min(backoff_base_s * backoff_factor ** attempt, backoff_max_s)`.
2. Apply jitter: `delay_s *= rng.uniform(1 - backoff_jitter, 1 + backoff_jitter)` where `rng` is
   `random.Random(config.gateway.rng_seed)` created once at gateway construction (deterministic
   in replay).
3. `await asyncio.sleep(delay_s)`; `attempt += 1`.
4. On successful connect + subscribe, record `connected_since = clock()`. `attempt` resets to 0
   only after the connection has stayed up for `backoff_reset_after_s` (checked by the watchdog),
   so a connect-crash flap keeps escalating delay.
5. If `attempt > max_reconnect_attempts` (0 = unlimited): publish
   `FeedStatus("*", DOWN, "fatal: reconnect attempts exhausted")` and stop the connection task.
   The gateway stays up; other venues are unaffected. Blocks 5/7 own the reaction.

After every reconnect the full sequence — subscribe, re-snapshot every instrument — is repeated;
`last_seq` is reset to `None` (a fresh connection is a fresh sequence space, requirement A4).

### 4.3 Timestamping and the two clocks

- `ts_local`: `clock()` (defaults to `time.time_ns()`) taken in §4.1 step 5a, i.e. one call per
  raw frame; all events parsed from one frame share it.
- `ts_exchange`: adapter converts the venue's event time to ns UTC. If the venue provides only
  millisecond precision, multiply by 10**6. If the venue omits event time, set 0 — consumers
  must treat `ts_exchange == 0` as "unknown" and fall back to `ts_local`.
- The MDG never adjusts, reorders by, or corrects timestamps. Skew is *measured and reported*
  (§4.7); acting on it is Block 7's job (design §4.3).

### 4.4 Sequence-gap detection

Per instrument, `last_seq: int | None` (None until first snapshot after (re)connect/recovery).
For each incoming `BookDelta` or `TradePrint` with sequence `s` (they share one seq space):

1. If state is GAP: append event to `gap_buffer` (bounded, `gap_buffer_max_events`; on overflow
   see §4.5 step 8) and return.
2. If `last_seq is None`: buffer as in step 1 (recovery in flight) and return.
3. If `s == last_seq + 1`: `last_seq = s`; **publish**.
4. If `s <= last_seq`: duplicate/stale — increment `duplicates_dropped`, **drop silently**.
5. If `s > last_seq + 1`: gap of `s - last_seq - 1` messages → transition to GAP, publish
   `FeedStatus(instrument, GAP, f"gap: expected {last_seq+1}, got {s}")`, put this event into
   `gap_buffer`, and start snapshot recovery (§4.5).

`BookSnapshot` events from the stream (some venues push periodic snapshots) are handled as in
§4.5 step 6 regardless of state: they can only move the baseline forward.

### 4.5 Snapshot recovery

Triggered by: startup (per instrument), reconnect, sequence gap, or gap-buffer overflow. At most
one recovery task per instrument at a time (a trigger while one is in flight only extends it).

1. State is GAP (set by the trigger). Downstream contract: nothing is published for this
   instrument until step 7.
2. Incoming deltas/trades keep accumulating in `gap_buffer` (ordered by arrival).
3. Spawn recovery task: `snap = await adapter.request_snapshot(instrument)` with timeout
   `snapshot_timeout_ms`.
4. On timeout or adapter error: retry up to `snapshot_max_retries` times with the §4.2 backoff
   formula (same constants, separate per-instrument attempt counter). If retries are exhausted:
   publish `FeedStatus(instrument, DOWN, "snapshot: recovery failed")` and force a venue
   reconnect (cancel `adapter.stream()` via `adapter.disconnect()`; §4.1 step 6 takes over) —
   a venue that cannot serve snapshots is treated as down.
5. On success: stamp `snap.ts_local = clock()` (the REST response arrival time).
6. Set `last_seq = snap.seq`. Publish `snap`.
7. Drain `gap_buffer` in order: drop every event with `seq <= snap.seq`; for the rest, replay
   them through §4.4 steps 3–5 (a residual hole in the buffered tail triggers a fresh recovery —
   go to step 1). If the drain completes without a new gap: transition GAP → LIVE, publish
   `FeedStatus(instrument, LIVE, f"recovered: snapshot seq={snap.seq}, replayed {n} buffered")`.
8. Gap-buffer overflow (buffer full while in GAP): clear the buffer, increment
   `gap_buffer_overflows`, and restart recovery from step 3 (the next snapshot will supersede
   everything discarded). Publish `FeedStatus(instrument, GAP, "gap: buffer overflow, re-snapshotting")`.

### 4.6 Staleness watchdog

Per venue task, ticking every 500 ms:

1. For each instrument in state LIVE: if `clock() - last_msg_ts_local > stale_after_ms * 10**6`,
   transition LIVE → STALE, publish `FeedStatus(instrument, STALE, f"stale: no message for {x} ms")`.
   (STALE → LIVE happens on the next published message, §4.1 step 5h.)
2. If **all** instruments of a venue have been STALE for `dead_after_ms`, assume a half-open
   TCP connection: force reconnect (as §4.5 step 4's disconnect). This is the only place the
   watchdog acts rather than reports.
3. Reset the venue's backoff `attempt` to 0 if `connected_since` is older than
   `backoff_reset_after_s` (§4.2 step 4).

STALE is a *report*, not an error: illiquid instruments legitimately go quiet. Tune
`stale_after_ms` per deployment; Block 2 folds STALE into `BookIntegrity.staleness_ms`.

### 4.7 Clock-skew measurement

Per instrument, over events with `ts_exchange > 0`:

1. `raw_skew_ns = ts_local - ts_exchange` (positive = local clock ahead of exchange event time;
   includes network latency, so it is an upper bound on true skew).
2. EWMA: `skew_ewma = raw_skew_ns` on first sample, else
   `skew_ewma += skew_alpha * (raw_skew_ns - skew_ewma)` with `skew_alpha = 0.05`.
3. If `abs(skew_ewma) > max_clock_skew_ms * 10**6` and no skew alert was published in the last
   `skew_alert_interval_s`: publish `FeedStatus(instrument, <current state>, f"clock_skew: ewma={skew_ewma//10**6} ms")`
   — note the state field is *unchanged*; only `detail` carries the alert. Block 7 pattern-matches
   the `clock_skew:` code to trigger early force-flat (design §4.3).

### 4.8 Raw-feed archiving

**What is archived:** every raw wire frame, verbatim, before parsing — so a vendor-parsing bug
can be replayed and fixed against ground truth. Normalized events are *additionally* journaled
by the bus (design §2); the post-session compactor (below) converts the journal's Block 1 events
to Parquet for Block 8.

**Frame record.** The archiver thread pops `RawFrame(venue, ts_local, payload: bytes)` from its
queue and writes one NDJSON line per frame:

```json
{"v":1,"venue":"sim","ts_local":1750000000123456789,"payload_b64":"eyJ0eXBlIjoi..."}
```

`payload_b64` is standard base64 of the exact bytes received. `"v"` is the record-format version.

**File format & rotation.** Files are gzip-compressed NDJSON, flushed every
`archive.flush_interval_s`. A file is closed and a new one opened when either (a) uncompressed
bytes written ≥ `archive.rotate_max_mb` × 2²⁰, or (b) the top of a UTC hour passes.

**Naming.** `{archive.dir}/{venue}/{YYYY-MM-DD}/raw-{YYYYMMDD}T{HHMMSS}Z-{nnn}.ndjson.gz`
where the timestamp is the file-open time (UTC) and `nnn` is a 3-digit within-day counter
starting 000. Files are never appended to after close; a crash leaves at most one unclosed file,
which is still readable line-by-line (gzip flush boundaries align with record boundaries).

**Compaction.** `ParquetCompactor.compact(journal_path, out_dir)` is a standalone function run
after session close (invoked by ops or Block 7's post-close hook — not by the MDG's hot path).
It reads the day's journal, selects Block 1 events, and writes four Parquet files per day:
`{out_dir}/{YYYY-MM-DD}/{book_snapshots,book_deltas,trade_prints,feed_status}.parquet`, one row
per event, columns = the dataclass fields (snapshot `bids`/`asks` as `list<struct<price:double,size:double>>`),
sorted by `(instrument, seq, ts_local)`, zstd compression. These files are Block 8's replay input.

**Retention.** The archiver deletes raw `.ndjson.gz` directories older than
`archive.retention_days` at startup and at each UTC-midnight rotation.

---

## 5. Adapter interface and reference adapter

### 5.1 `MarketDataAdapter` (ABC)

```python
@dataclass(frozen=True, slots=True)
class RawFrame:
    venue: str
    ts_local: int          # stamped by the gateway core, ns
    payload: bytes         # exact wire bytes

class AdapterParseError(Exception):
    """Raised by stream() for a frame that cannot be normalized. Carries .raw: RawFrame."""

class ConnectionLost(Exception):
    """Raised by stream() when the transport drops. Carries .reason: str."""

ParsedEvent = Union[BookSnapshot, BookDelta, TradePrint, None]  # None = heartbeat/admin

class MarketDataAdapter(abc.ABC):
    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the transport. Raise ConnectionError on failure. Idempotent if already open."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Close the transport; stream() must then raise ConnectionLost promptly. Idempotent."""

    @abc.abstractmethod
    async def subscribe(self, instruments: Sequence[str]) -> None:
        """Subscribe to book deltas + trades + session status for internal symbols.
        Raise ValueError for unknown symbols, ConnectionError on transport failure."""

    @abc.abstractmethod
    def stream(self) -> AsyncIterator[tuple[bytes, ParsedEvent]]:
        """Yield (raw_payload, parsed_event) per wire frame, in wire order.
        parsed_event.ts_local must be the sentinel 0 (core stamps it).
        Raise AdapterParseError for unparseable frames (after yielding nothing for them is NOT
        allowed — the error carries the raw frame so the core can archive it).
        Raise ConnectionLost when the transport drops."""

    @abc.abstractmethod
    async def request_snapshot(self, instrument: str) -> BookSnapshot:
        """Fetch a full book snapshot (REST or dedicated channel). ts_local sentinel 0.
        Raise TimeoutError / ConnectionError on failure. Must be safe to call while
        stream() is being consumed."""
```

**Adapter contract (normative):**

- A1. Every yielded event has `ts_local == 0` and internal (not vendor) symbols.
- A2. Prices are rounded to `tick_size`, sizes to `lot_size` (from reference data / config).
- A3. `BookDelta.size` is absolute-new-size semantics; adapters for venues that send increments
  must convert (which requires venue-side aggregate state — allowed inside the adapter).
- A4. `seq` is monotonically increasing per instrument within one `connect()` epoch, without
  legitimate holes: the adapter maps the venue's scheme (per-book update IDs, global seq +
  filtering, etc.) so that any hole seen by the core **is** data loss. If a venue genuinely
  cannot provide this, the adapter must synthesize `seq` by counting and detect venue-level
  gaps itself, surfacing them as `AdapterParseError` — never silently renumber across a hole.
- A5. `stream()` yields events parsed from one wire frame contiguously and in-frame order.
- A6. Adapters hold no asyncio locks across yields and never call the bus.

### 5.2 Reference adapter: `SimulatedFeedAdapter` (deterministic)

Purpose: unit/integration testing of the gateway core and downstream blocks without a network.
Fully deterministic: identical config + seed ⇒ byte-identical event stream.

```python
@dataclass(frozen=True, slots=True)
class SimFault:
    at_event: int                      # global event index at which the fault fires
    kind: Literal["gap", "disconnect", "malformed", "silence"]
    param: int = 0                     # gap: #messages to skip; silence: ms of no output

class SimulatedFeedAdapter(MarketDataAdapter):
    def __init__(
        self,
        instruments: Sequence[str],
        seed: int = 42,
        events_per_second: float = 100.0,
        n_events: int | None = None,        # None = unbounded
        start_price: float = 100.00,
        tick_size: float = 0.01,
        lot_size: float = 1.0,
        book_depth: int = 10,
        trade_fraction: float = 0.2,        # P(event is a trade), else a delta
        snapshot_interval: int = 1000,      # push a full snapshot every N events per instrument
        t0_ns: int = 1_750_000_000_000_000_000,
        faults: Sequence[SimFault] = (),
        realtime: bool = False,             # False: yield without sleeping (tests)
    ) -> None: ...
```

**Generation algorithm (normative — implement exactly):**

1. One `random.Random(seed + index(instrument))` PRNG per instrument; instruments round-robin
   in config order (event *i* belongs to instrument `instruments[i % len(instruments)]`).
2. Per instrument, maintain `mid` (starts `start_price`) and a synthetic book of `book_depth`
   levels per side: level *k* (0-based) at price `round(mid ∓/± (k+1)*tick_size)` with size
   `100*(k+1)` initially. All prices rounded to `tick_size` via
   `round(p / tick_size) * tick_size` then `round(p, 10)`.
3. `ts_exchange` for event *i* (global index, starting 0) is
   `t0_ns + i * int(1e9 / events_per_second)`. If `realtime`, sleep the same interval before
   yielding; otherwise yield immediately.
4. Per-instrument `seq` starts at 1000 and increments by 1 per generated message (snapshots
   included). Event *i*, with that instrument's PRNG `r`:
   a. Draw `u = r.random()`.
   b. If this is the instrument's `snapshot_interval`-th message since its last snapshot
      (and at message 0): emit a `BookSnapshot` of the current synthetic book at the current seq.
   c. Elif `u < trade_fraction`: emit `TradePrint(price=best bid if r.random()<0.5 else best ask,
      size=float(r.randint(1,10))*lot_size, aggressor_side=SELL if hit-bid else BUY)`. Then move
      `mid` by `tick_size * r.choice([-1, 0, 0, 1])` and rebuild level prices around it
      (sizes persist per level index).
   d. Else: pick `side = BID if r.random()<0.5 else ASK`, level `k = r.randint(0, book_depth-1)`,
      new size `float(r.randint(0, 500))` (0 ⇒ removal; the synthetic book keeps the price slot
      with size re-randomized to `100*(k+1)` for future snapshots). Emit `BookDelta` at that
      level's price with the new absolute size.
5. The raw payload for each event is the canonical JSON of the event dict (sorted keys, no
   whitespace) encoded UTF-8 — so the raw archive of a sim run is human-readable.
6. **Fault injection**, checked before generating event `at_event`:
   - `gap`: silently skip `param` messages (advance `seq` and the PRNG by generating and
     discarding them) — the next yielded event has a seq hole of exactly `param`.
   - `disconnect`: raise `ConnectionLost("sim: scripted disconnect")`. A subsequent
     `connect()` resumes generation at the same global index (state persists across epochs;
     `last_seq` continuity is intentional so reconnect tests also exercise re-snapshot logic).
   - `malformed`: raise `AdapterParseError` with `raw.payload = b'{"type":"garbage"}'`, then
     continue with the next event.
   - `silence`: if `realtime`, sleep `param` ms; else yield nothing and mark the gap in
     `ts_exchange` only (tests drive the watchdog with a fake clock).
7. `request_snapshot(instrument)` returns the current synthetic book with `seq` = that
   instrument's last generated seq, after an `asyncio.sleep(0)` (yield point). Deterministic:
   no PRNG draw.
8. `stream()` ends (StopAsyncIteration) after `n_events` total events when `n_events` is set.

---

## 6. Configuration

Format: TOML, loaded by `GatewayConfig.from_toml(path)`. Loading validates every range below and
raises `ConfigError` listing *all* violations. Design §4.4: this file is versioned; the run
journal records its SHA-256.

```toml
# config/gateway.toml — complete example with every parameter.

[gateway]
rng_seed             = 1               # int >= 0; seeds backoff jitter (determinism). default 1
stop_timeout_s       = 10.0            # float (0, 60]; max graceful-shutdown time. default 10.0

[reconnect]
backoff_base_s          = 0.5          # float (0, 10]; first retry delay.          default 0.5
backoff_factor          = 2.0          # float [1, 10]; exponential multiplier.     default 2.0
backoff_max_s           = 30.0         # float (0, 300]; delay ceiling.             default 30.0
backoff_jitter          = 0.2          # float [0, 0.5]; +/- fraction of delay.     default 0.2
backoff_reset_after_s   = 60.0         # float (0, 3600]; stable-uptime reset.      default 60.0
max_reconnect_attempts  = 0            # int >= 0; 0 = unlimited.                   default 0

[sequencing]
gap_buffer_max_events   = 10000        # int [100, 1_000_000]; per instrument.      default 10000
snapshot_timeout_ms     = 5000         # int [100, 60000].                          default 5000
snapshot_max_retries    = 5            # int [1, 20].                               default 5

[staleness]
stale_after_ms          = 2000         # int [100, 60000]; LIVE->STALE.             default 2000
dead_after_ms           = 15000        # int [1000, 300000]; all-stale reconnect.   default 15000
                                       # must be > stale_after_ms

[clock]
skew_alpha              = 0.05         # float (0, 1]; EWMA weight.                 default 0.05
max_clock_skew_ms       = 500          # int [10, 10000]; alert threshold.          default 500
skew_alert_interval_s   = 60           # int [1, 3600]; alert rate limit.           default 60

[archive]
dir                     = "data/raw"   # str; created if missing.                   default "data/raw"
rotate_max_mb           = 256          # int [16, 4096]; uncompressed.              default 256
flush_interval_s        = 1.0          # float (0, 60].                             default 1.0
queue_max_frames        = 100000       # int [1000, 10_000_000].                    default 100000
retention_days          = 90           # int [1, 3650].                             default 90
compact_out_dir         = "data/parquet"  # str.                       default "data/parquet"

# One [[venues]] table per venue. `adapter` selects the registered adapter class.
[[venues]]
name       = "sim"                     # str, unique across venues
adapter    = "simulated"               # str, key in the adapter registry
# Credentials come from environment variables, never from this file:
api_key_env    = "SIM_API_KEY"         # str; "" for adapters that need no auth. default ""
api_secret_env = "SIM_API_SECRET"      # str.                                    default ""
ws_url     = ""                        # str; "" for the simulated adapter
rest_url   = ""                        # str

  # Instrument universe for this venue. `symbol` = internal name used everywhere downstream.
  [[venues.instruments]]
  symbol        = "AAPL"               # str, unique across the whole config
  vendor_symbol = "AAPL"               # str; the venue's name for it
  tick_size     = 0.01                 # float > 0
  lot_size      = 1.0                  # float > 0
  benchmark     = false                # bool; exactly one instrument in the whole
                                       # config must set true (Block 9's benchmark feed)
  [[venues.instruments]]
  symbol        = "SPY"
  vendor_symbol = "SPY"
  tick_size     = 0.01
  lot_size      = 1.0
  benchmark     = true

# Adapter-specific parameters, namespaced by adapter name (passed through verbatim).
[adapters.simulated]
seed              = 42
events_per_second = 100.0
book_depth        = 10
trade_fraction    = 0.2
snapshot_interval = 1000
```

Cross-field validation: `dead_after_ms > stale_after_ms`; exactly one `benchmark = true`
instrument; venue names and symbols unique; every `adapter` value present in the registry.

---

## 7. Error handling and the FeedStatus state machine

### 7.1 State machine (per instrument)

```
                 +--------------------------- disconnect / venue dead --------------------+
                 |                                                                        |
   startup       v                                                                        |
  ---------->  DOWN ----connect+subscribe----> GAP ----snapshot ok + buffer drained---> LIVE
                 ^                              ^  \                                   ^  |
                 |                              |   +--resnapshot (overflow/tail gap)--+  |
                 |                              |                                         |
                 |                              +---------------- seq gap ----------------+
                 |                                                                        |
                 +---- snapshot retries exhausted ---- GAP                                |
                                                                                          v
                                             LIVE --no msg for stale_after_ms--------> STALE
                                             LIVE <---------- any message ------------ STALE
                                             STALE --seq gap--> GAP    STALE --disconnect--> DOWN
```

Legal transitions (exhaustive): DOWN→GAP, GAP→LIVE, GAP→GAP (re-snapshot), GAP→DOWN,
LIVE→STALE, STALE→LIVE, LIVE→GAP, STALE→GAP, LIVE→DOWN, STALE→DOWN. Anything else is a bug;
assert in code. Every transition publishes exactly one `FeedStatus`. Repeated states (e.g.
GAP→GAP) also publish, so consumers can watch recovery progress.

### 7.2 Failure-mode table

| # | Failure | Detection | Response | Emitted on bus |
|---|---------|-----------|----------|----------------|
| E1 | Websocket connect refused/timeout | `adapter.connect()` raises | Backoff §4.2, retry | `FeedStatus("*", DOWN, "connect: <err>")` once per state entry |
| E2 | Websocket drops mid-session | `stream()` raises `ConnectionLost` | Clear gap buffers, backoff, reconnect, resubscribe, re-snapshot all | `FeedStatus("*", DOWN, "disconnect: <reason>")`, then per-instrument GAP→LIVE during recovery |
| E3 | Subscription rejected | `adapter.subscribe()` raises | Treat as E1 (disconnect + backoff) | `FeedStatus("*", DOWN, "subscribe: <err>")` |
| E4 | Sequence gap | §4.4 step 5 | Buffer, snapshot recovery §4.5 | `FeedStatus(instr, GAP, "gap: expected N, got M")`, then snapshot + LIVE |
| E5 | Duplicate/old seq | §4.4 step 4 | Drop, count | nothing |
| E6 | Malformed vendor message | `AdapterParseError`, or `ValueError` from event `__post_init__` | Archive the raw frame, increment `malformed_dropped`, drop, continue. If > `10` malformed in 60 s (hard-coded circuit breaker): force reconnect | nothing per message; on breaker: `FeedStatus("*", DOWN, "malformed: parse-error storm")` |
| E7 | Snapshot request fails/times out | §4.5 step 4 | Retry with backoff; after `snapshot_max_retries`: force venue reconnect | `FeedStatus(instr, GAP, "snapshot: retry k/N")`; on exhaustion `FeedStatus(instr, DOWN, "snapshot: recovery failed")` |
| E8 | Bus queue full (`BusFull`) | `bus.publish` raises | This means downstream is wedged — losing one delta corrupts Block 2's book, so treat the instrument as gapped: enter GAP, retry publish with 10 ms sleep up to 1 s, then drop buffer + recover via §4.5 | `FeedStatus(instr, GAP, "bus: backpressure")` (published via a reserved always-accepts status lane) |
| E9 | Feed silent | Watchdog §4.6 | Report; after `dead_after_ms` all-stale: reconnect | `FeedStatus(instr, STALE, "stale: no message for X ms")` |
| E10 | Clock skew beyond bound | §4.7 | Report only (Block 7 acts) | `FeedStatus(instr, <state>, "clock_skew: ewma=X ms")`, rate-limited |
| E11 | Archive disk full / write error | Archiver thread I/O exception | Log CRITICAL, drop frames, keep trading (archiving is not safety-critical); retry file open every 30 s | `FeedStatus("*", <state>, "archive: write failed")`, rate-limited to 1/min |
| E12 | Gap buffer overflow | §4.5 step 8 | Discard buffer, re-snapshot | `FeedStatus(instr, GAP, "gap: buffer overflow, re-snapshotting")` |
| E13 | Reconnect attempts exhausted (`max_reconnect_attempts > 0`) | §4.2 step 5 | Stop venue task; other venues unaffected | `FeedStatus("*", DOWN, "fatal: reconnect attempts exhausted")` |

`detail` code prefixes are normative and machine-parseable: `startup:`, `connect:`,
`disconnect:`, `subscribe:`, `gap:`, `snapshot:`, `recovered:`, `stale:`, `clock_skew:`,
`malformed:`, `bus:`, `archive:`, `fatal:`, `shutdown:`.

### 7.3 Fail-safe posture

Per design §4.3, the MDG never fabricates data to paper over a problem. When in doubt it
publishes GAP/DOWN and recovers via snapshot; Blocks 5/7 translate feed degradation into
halt/flatten. The MDG itself takes no trading action.

---

## 8. Performance and memory budget

Targets on the reference deployment (single core of a modern x86-64, CPython 3.11):

| Metric | Budget |
|---|---|
| Frame receive → event on bus, p50 | ≤ 1 ms |
| Frame receive → event on bus, p99 | ≤ 5 ms (fits the system's 10–500 ms budget with ≥ 5 ms left for Blocks 2–6) |
| Sustained throughput | ≥ 5,000 events/s across all instruments without STALE flapping |
| Snapshot recovery, gap → LIVE (excluding venue RTT) | ≤ 50 ms processing |
| `ts_local` stamping error | ≤ 100 µs after frame arrival (stamp before parse) |

Memory bounds (all hard, enforced by bounded structures):

- Gap buffer: ≤ `gap_buffer_max_events` events/instrument (~200 B each → ≤ 2 MB/instrument at
  default).
- Archive queue: ≤ `queue_max_frames` frames; frames average ≤ 512 B ⇒ ≤ 50 MB at default.
  Overflow drops frames (counted), never blocks.
- Per-instrument state: O(1) — last_seq, timestamps, skew EWMA, counters.
- No unbounded caches anywhere. Steady-state RSS attributable to the MDG ≤ 150 MB at defaults
  with 50 instruments.

Hot-path rules: no per-event allocation beyond the event object and its `replace`; no logging at
INFO or above per event (per-event diagnostics at DEBUG only, off in production); archiver and
compactor never run on the event loop.

---

## 9. Test plan

Use `SimulatedFeedAdapter` (`realtime=False`) with an injected fake clock
(`clock=lambda: fake_ns`) and a recording bus stub that appends published events to a list.

### 9.1 Unit tests

| ID | Test | Input | Expected |
|---|---|---|---|
| U1 | Dataclass validation | `PriceLevel(price=-1, size=5)`; `BookDelta(..., size=-1)`; snapshot with bids `[99.99, 100.00]` (ascending) | `ValueError` in each case |
| U2 | Delta size-zero accepted | `BookDelta(..., price=100.00, size=0.0, seq=7)` | constructs; `size == 0.0` |
| U3 | Seq: in-order | last_seq=100; deltas seq 101,102,103 | all published, last_seq=103 |
| U4 | Seq: duplicate | last_seq=102; delta seq 102, then seq 101 | both dropped, `duplicates_dropped == 2`, nothing published |
| U5 | Seq: gap detection | last_seq=102; delta seq 105 | `FeedStatus(GAP, "gap: expected 103, got 105")` published; delta buffered, not published |
| U6 | Backoff schedule | defaults, jitter=0, attempts 0..7 | delays 0.5, 1, 2, 4, 8, 16, 30, 30 s |
| U7 | Backoff jitter determinism | `rng_seed=1`, jitter=0.2, two gateway instances | identical delay sequences |
| U8 | Backoff reset | connect at t; watchdog tick at t+61 s (fake clock) | `attempt == 0` |
| U9 | Skew EWMA | events with `raw_skew` 100 ms constant, alpha 0.05 | ewma → 100 ms monotonically; alert fires once when ewma > 500 ms would require inputs > 500 — with 100 ms inputs no alert |
| U10 | Skew alert rate limit | skew 600 ms constant, 1000 events in 10 s | exactly one `clock_skew:` FeedStatus |
| U11 | Stale transition | LIVE, fake clock advances 2001 ms with no messages, watchdog tick | `FeedStatus(STALE, ...)`; then one delta arrives → `FeedStatus(LIVE, "recovered: ...")` |
| U12 | Archive record format | one frame `b'{"a":1}'`, venue "sim", ts_local=123 | line `{"v":1,"venue":"sim","ts_local":123,"payload_b64":"eyJhIjoxfQ=="}` |
| U13 | Archive rotation | write 257 MB uncompressed (rotate_max_mb=256) | 2 files; second name has counter `001` |
| U14 | Config validation | `dead_after_ms=1000, stale_after_ms=2000`; zero/two benchmark instruments | `ConfigError` naming both violations |
| U15 | State machine legality | drive every transition in §7.1; attempt DOWN→STALE via test hook | legal ones succeed with one FeedStatus each; illegal one raises AssertionError |
| U16 | Sim determinism | two `SimulatedFeedAdapter(seed=42, n_events=1000)` runs | byte-identical raw payload sequences |
| U17 | Malformed storm breaker | 11 `AdapterParseError` frames within 60 s (fake clock) | first 10: dropped + archived; 11th triggers reconnect + `FeedStatus(DOWN, "malformed: ...")` |

### 9.2 Integration tests

| ID | Scenario | Setup | Expected |
|---|---|---|---|
| I1 | Cold start to LIVE | sim adapter, 2 instruments, `n_events=100` | per instrument: FeedStatus DOWN(startup) → GAP(awaiting initial snapshot) → BookSnapshot → LIVE; then 100 events total on bus in per-instrument seq order |
| I2 | Gap + recovery (worked example below) | `faults=[SimFault(at_event=50, kind="gap", param=3)]` | see §9.3 |
| I3 | Disconnect + reconnect | `faults=[SimFault(at_event=200, kind="disconnect")]` | FeedStatus DOWN → (backoff 0.5 s on fake clock) → reconnect → per-instrument snapshot → LIVE; no delta lost or duplicated across the boundary (verify contiguous seq per instrument after each recovery snapshot) |
| I4 | Snapshot failure escalation | adapter stub whose `request_snapshot` always times out; trigger one gap | `snapshot_max_retries` GAP(`snapshot: retry k/N`) statuses, then DOWN(`snapshot: recovery failed`), then venue reconnect |
| I5 | Stop is clean | start, run 100 events, `await stop()` | stop returns < `stop_timeout_s`; archive file closed and gzip-valid; final FeedStatus DOWN(shutdown) per instrument; second `stop()` is a no-op |
| I6 | End-to-end archive replay | run 1000 sim events; decode archive; re-feed payloads through a fresh adapter parse | decoded event stream identical to what the bus recorded (modulo ts_local) |
| I7 | Compactor | journal from I1 | 4 Parquet files, row counts match published event counts, sorted by (instrument, seq, ts_local) |
| I8 | Bus backpressure | bus stub raises BusFull for 500 ms then accepts | FeedStatus GAP("bus: backpressure"), then snapshot recovery, then LIVE; no partially-applied delta sequence visible downstream |

### 9.3 Worked example (normative reference for I2 and for Block 2's tests)

Single instrument `AAPL`, `tick_size=0.01`. The venue delivers, in order:

1. Initial snapshot, `seq=100`:
   - bids: `[(100.00, 500), (99.99, 300)]`, asks: `[(100.01, 400), (100.02, 200)]`
2. `BookDelta(side=BID, price=100.00, size=450, seq=101)`
3. `TradePrint(price=100.01, size=50, aggressor_side=BUY, seq=102)`
4. — venue loses `seq=103` (a delta) —
5. `BookDelta(side=ASK, price=100.02, size=250, seq=104)`
6. Recovery snapshot (returned by `request_snapshot`), `seq=105`:
   - bids: `[(100.00, 450), (99.99, 300)]`, asks: `[(100.01, 350), (100.03, 100)]`

Expected bus output, in order (ts_local values are whatever the fake clock returned; shown as t1…):

| # | Event | Key fields |
|---|---|---|
| 1 | `FeedStatus` | `AAPL, GAP, "startup: awaiting initial snapshot"` |
| 2 | `BookSnapshot` | `seq=100`, bids `[(100.00,500),(99.99,300)]`, asks `[(100.01,400),(100.02,200)]` |
| 3 | `FeedStatus` | `AAPL, LIVE, "recovered: snapshot seq=100, replayed 0 buffered"` |
| 4 | `BookDelta` | `BID, 100.00, 450, seq=101` |
| 5 | `TradePrint` | `100.01, 50, BUY, seq=102` |
| 6 | `FeedStatus` | `AAPL, GAP, "gap: expected 103, got 104"` — triggered by the seq-104 delta, which is **buffered, not published** |
| 7 | `BookSnapshot` | `seq=105`, bids `[(100.00,450),(99.99,300)]`, asks `[(100.01,350),(100.03,100)]` |
| 8 | `FeedStatus` | `AAPL, LIVE, "recovered: snapshot seq=105, replayed 0 buffered"` |

The buffered seq-104 delta is dropped during the drain because `104 <= 105` (snapshot already
includes it — note ask 100.02 is absent from the recovery snapshot: the lost seq-103 delta had
removed it, and 104's change was superseded). `last_seq` is now 105; the next accepted message
must carry `seq=106`.

### 9.4 Performance test

P1: sim adapter, 10 instruments, `events_per_second=5000` aggregate, `realtime=True`, 60 s run
on the reference machine: assert p99 receive→publish ≤ 5 ms (measure `bus_publish_ts - ts_local`),
zero `archive_frames_dropped`, zero STALE transitions, RSS growth < 20 MB over the run.

---

## 10. Acceptance criteria

- [ ] All four event types implemented exactly as §2 (field names, types, frozen, slots,
      validation), importable from `mdg/events.py`.
- [ ] `MarketDataGateway.start()/stop()` behave per §3.2; `stop()` idempotent and bounded by
      `stop_timeout_s`.
- [ ] Single event loop + single archiver thread; no other threads; no locks on the hot path.
- [ ] Per-instrument events reach the bus in strictly increasing seq order, snapshots reset the
      baseline, and nothing is published for an instrument while it is in GAP.
- [ ] Reconnect backoff matches U6/U7 exactly (deterministic under `rng_seed`).
- [ ] The §7.1 state machine is enforced (illegal transitions assert) and every transition emits
      exactly one `FeedStatus` with the correct `detail` code prefix.
- [ ] All 13 failure modes in §7.2 behave as tabulated, each covered by at least one test.
- [ ] Raw archive files match §4.8 (record format U12, rotation U13, naming, retention) and are
      replayable (I6); compactor produces Block 8-consumable Parquet (I7).
- [ ] `SimulatedFeedAdapter` is bit-deterministic (U16) and implements all four fault kinds.
- [ ] `GatewayConfig.from_toml` accepts the §6 example verbatim and rejects each documented
      range violation with all errors listed (U14).
- [ ] Performance test P1 passes on the reference machine.
- [ ] The MDG contains no code that maintains a book, computes features, or sends orders
      (verified by review against §1.2).
- [ ] Every test in §9 implemented and green in CI.
