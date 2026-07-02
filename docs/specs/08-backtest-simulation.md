# Block 8 — Backtest & Simulation Environment

Spec version: 1.0. Conforms to `docs/design.md` (§3 Block 8, §4 cross-cutting rules, §5 technology
assumptions). Language: Python 3.11+. All timestamps are `int` nanoseconds since Unix epoch, UTC,
unless stated otherwise. All money values are `float` USD; all quantities are `int` shares.

---

## 1. Overview and responsibilities

Block 8 is the offline twin of the online system. It replays archived market data from Block 1
through the **unchanged** online Blocks 2–7 with a simulated clock and a simulated broker, produces
the metrics that decide whether a parameter set may trade real money, and owns walk-forward
calibration and the promotion pipeline.

### 1.1 Responsibilities

- Read and validate Block 1 archive files (format defined authoritatively in §3).
- Drive Blocks 2–7 in event time via a deterministic simulated clock (§4) and replay engine (§5).
- Provide a backtest `ExecutionAdapter` (simulated broker) with a conservative fill model,
  fees, and seeded simulated latency (§6).
- Compute the metric suite: net return, annualized Sharpe, max drawdown, hit rate, turnover,
  trades/day, capacity estimate, benchmark comparison (§7).
- Run walk-forward calibration over a declared parameter search space and apply the promotion
  rule (§8).
- Emit `BacktestReport` JSON and versioned promoted-parameter files consumed by Block 4 (§9).
- Define and gate the promotion pipeline stages: backtest → paper → small live → scaled (§10).
- Guarantee and verify determinism: same archive + config + seed → bit-identical journal (§5.5).

### 1.2 Non-responsibilities

- Does **not** contain any strategy, risk, execution, or scheduling logic. All decision logic
  lives in Blocks 4–7 and runs here unmodified.
- Does **not** talk to any live market-data or broker API. It never imports a live adapter.
- Does **not** write archives (Block 1 does) and does not modify archives (read-only).
- Does **not** run the paper/live stages itself; it defines their go/no-go thresholds (§10) and
  evaluates their results, but paper/live execution is the online system with a different
  `ExecutionAdapter`.
- Does **not** do real-time monitoring or dashboards (Block 9). It reuses Block 9's journal writer
  as a library so backtest journals are format-identical to live journals.

### 1.3 The "same code" principle and injection points

Blocks 2–7 MUST run byte-identical code in live trading and in backtest. Exactly three
dependencies are swapped, all injected at construction time through the application context. No
block may read the wall clock, spawn its own timers, or open a network socket directly; violations
are bugs in that block, not configuration points.

The three injection points (protocols defined in `src/bot/core/interfaces.py`; live
implementations elsewhere; simulated implementations in this block):

```python
from typing import Protocol, Callable, Sequence

class Clock(Protocol):
    def now_ns(self) -> int: ...
    def call_at(self, deadline_ns: int, callback: Callable[[], None]) -> "TimerHandle": ...
    def call_later(self, delay_ns: int, callback: Callable[[], None]) -> "TimerHandle": ...

class TimerHandle(Protocol):
    def cancel(self) -> None: ...

class MarketDataSource(Protocol):
    """Emits normalized Block 1 output events onto the event bus:
    BookSnapshot, BookDelta, TradePrint, FeedStatus."""
    async def run(self) -> None: ...

class ExecutionAdapter(Protocol):
    """The only order path. Block 6 calls these; adapter emits BrokerAck,
    BrokerFill, BrokerReject, BrokerCancelAck events back onto the bus."""
    def submit(self, order: "BrokerOrder") -> None: ...
    def cancel(self, broker_order_id: str) -> None: ...
    def positions(self) -> dict[str, int]: ...   # broker-side reconciliation view
```

| Dependency | Live implementation | Backtest implementation (this block) |
|---|---|---|
| `Clock` | `WallClock` (asyncio loop time mapped to UTC ns) | `SimClock` (§4) |
| `MarketDataSource` | Block 1 `MarketDataGateway` | `ArchiveReplaySource` (§5) |
| `ExecutionAdapter` | broker adapter / paper adapter | `SimBroker` (§6) |

Wiring: `build_app(ctx: AppContext) -> App` constructs Blocks 2–7 and takes
`ctx.clock`, `ctx.market_data_source`, `ctx.execution_adapter`. Block 7 obtains **all** timers
from `ctx.clock`. Block 2–7 event handlers are synchronous with respect to a single delivered
event (design §5: single process, bounded queues); the replay driver exploits this in §4.3.

Module layout (all under `src/bot/backtest/`): `clock.py`, `archive.py`, `replay.py`,
`sim_broker.py`, `metrics.py`, `benchmark.py`, `walkforward.py`, `report.py`, `promote.py`,
`cli.py`, `config.py`.

---

## 2. Inputs and outputs (contract summary)

**Inputs**
- Archive directory produced by Block 1 (§3).
- Backtest config file (YAML, §12) and candidate parameter set(s) (JSON, §9.2) or a search-space
  file (§8.2).
- Root RNG seed (`int`).

**Outputs** (all under `--out` run directory, §9.3)
- `report.json` — `BacktestReport` (§9.1).
- `journal.ndjson` — the replayed event journal (same format as live, Block 9 writer).
- `fills.parquet`, `equity.parquet` — per-fill and per-minute equity tables.
- `params-<ts>-<hash8>.json` — promoted parameter file (calibrate mode only, on promotion) (§9.2).
- `config.snapshot.yaml` — exact resolved config used (design §4.4).

---

## 3. Archive format (authoritative definition)

Design.md says only "Raw-feed archive files (for Block 8)" and "Parquet (archives)". This section
is the authoritative contract; Block 1's spec MUST write this format, and Block 8 reads it.

The archive stores Block 1's **normalized output events** (post-normalization, pre-bus), which is
what replay needs. Block 1 may additionally keep raw vendor bytes for its own debugging; Block 8
never reads those.

### 3.1 Layout

```
<archive_root>/
  <YYYY-MM-DD>/                      # exchange trading date (exchange calendar, not UTC date)
    manifest.json
    <INSTRUMENT>.events.parquet      # one file per instrument per day
```

`INSTRUMENT` is the internal symbol, uppercase, `[A-Z0-9._]+`.

### 3.2 `events.parquet` schema

One row per normalized event, ordered by (`ts_local_ns`, `seq`) ascending. Columns:

| column | type | nullability | meaning |
|---|---|---|---|
| `seq` | int64 | required | Block 1 per-instrument monotone sequence number |
| `ts_exchange_ns` | int64 | required | exchange timestamp |
| `ts_local_ns` | int64 | required | Block 1 receive timestamp; **replay ordering key** |
| `event_type` | dictionary<string> | required | `SNAPSHOT` \| `DELTA` \| `TRADE` \| `STATUS` |
| `side` | dictionary<string> | DELTA/TRADE | `BID` \| `ASK` (DELTA); `BUY` \| `SELL` aggressor (TRADE) |
| `price` | float64 | DELTA/TRADE | price level / trade price |
| `size` | int64 | DELTA/TRADE | new displayed size at level (DELTA, 0 = level removed); trade size (TRADE) |
| `bids` | list<struct{price: float64, size: int64}> | SNAPSHOT | best-first, full depth as received |
| `asks` | list<struct{price: float64, size: int64}> | SNAPSHOT | best-first |
| `status` | dictionary<string> | STATUS | `LIVE` \| `STALE` \| `GAP` \| `DOWN` |
| `detail` | string | STATUS | free text |

Mapping to bus messages is 1:1 (`SNAPSHOT`→`BookSnapshot`, `DELTA`→`BookDelta`,
`TRADE`→`TradePrint`, `STATUS`→`FeedStatus`).

### 3.3 `manifest.json` schema

```json
{
  "schema_version": 1,
  "date": "2026-01-05",
  "session": {"open_ns": 1767625200000000000, "close_ns": 1767648600000000000,
              "half_day": false},
  "instruments": {
    "XYZA": {"file": "XYZA.events.parquet", "rows": 1834211,
             "first_seq": 1, "last_seq": 1834211, "gap_count": 0,
             "sha256": "hex64…"},
    "XYZB": {"file": "XYZB.events.parquet", "rows": 902113,
             "first_seq": 1, "last_seq": 902114, "gap_count": 1,
             "sha256": "hex64…"}
  },
  "writer": {"code_revision": "git-sha", "written_at": "2026-01-05T21:05:11Z"}
}
```

`sha256` is over the Parquet file bytes. `gap_count` counts `seq` discontinuities Block 1 could
not repair (each is preceded in-stream by a `STATUS GAP` row and followed by a `SNAPSHOT`).

### 3.4 Reader validation (mandatory, per day, before replay)

1. `manifest.json` parses and `schema_version == 1`.
2. Every listed file exists; `sha256` matches (skippable with `--no-checksum`, default on).
3. Per file: `ts_local_ns` non-decreasing; `seq` strictly increasing; first event of the day per
   instrument is `SNAPSHOT` or `STATUS`.
4. Any failure → error per §13 row A/B; the run aborts (no silent partial replay).

---

## 4. Simulated clock (`SimClock`)

### 4.1 Semantics

`SimClock` is an **event-time** clock. Simulated time advances only when the replay driver
advances it; between advances, `now_ns()` is frozen. It implements the `Clock` protocol:

- `now_ns()` returns the current simulated time `t_sim`.
- `call_at(deadline_ns, cb)` registers a timer; `call_later(delay_ns, cb)` ≡
  `call_at(now_ns() + delay_ns, cb)`. Registering a deadline `< t_sim` fires it at `t_sim`
  (matching asyncio semantics).
- Timers get a monotone registration id `timer_id` (int, starts at 0, increments per
  registration, never reused). `cancel()` marks the timer dead; dead timers never fire.

### 4.2 Advancing time

The replay driver calls `sim_clock.advance_to(t_target)`:

1. While the earliest pending timer deadline `d ≤ t_target`: set `t_sim = d`, pop and run **all**
   timers with that exact deadline in ascending `timer_id` order (timers registered by a firing
   callback with the same deadline also fire, after the current batch, still by `timer_id`).
2. Set `t_sim = t_target`.

`advance_to` with `t_target < t_sim` is a fatal internal error (§13 row H).

### 4.3 Interleaving with data events — deterministic ordering rule

The global ordering key for everything that happens in the simulation is:

```
(ts_ns, source_priority, tiebreak)
```

| source_priority | source | tiebreak |
|---|---|---|
| 0 | timer callbacks (Block 7 phases, cooldown timers, tactic timeouts, sampling ticks) | `timer_id` |
| 1 | `SimBroker` deliveries (acks, fills, rejects, cancel-acks) at their latency-adjusted delivery time | broker-event monotone id |
| 2 | archive events | (`instrument` ascending lexicographic, `seq`) |

Consequences the implementer MUST honor:

- Timers with deadline `t` fire **before** any archive event with `ts_local_ns == t`.
- Broker deliveries at `t` fire after timers at `t` but before archive events at `t`.
- Two archive events with equal `ts_local_ns` are delivered in `(instrument, seq)` order.
- Each delivery is processed to completion (the bus drains fully: an archive event may cascade
  Block 2 → 3 → 4 → 5 → 6 → `SimBroker.submit`) before the next key is popped. The bus in
  backtest is the same in-process bus, run to quiescence synchronously per delivered event.

The driver loop (normative pseudocode):

```python
while True:
    t_next = min(clock.next_timer_deadline(),      # +inf if none
                 broker.next_delivery_ts(),        # +inf if none
                 archive.peek_ts())                # +inf if exhausted
    if t_next == INF: break
    # priority 0: timers strictly first at t_next
    clock.advance_to(t_next)                       # fires all timers with deadline <= t_next
    while broker.next_delivery_ts() == t_next:
        bus.deliver(broker.pop_delivery()); bus.drain()
    while archive.peek_ts() == t_next:
        bus.deliver(archive.pop_event()); bus.drain()
```

---

## 5. Replay engine (`ArchiveReplaySource`)

### 5.1 Scope of a run

A run is defined by (`archive_root`, `date_from`, `date_to` inclusive, `instruments` list). Days
are the exchange-calendar dates present as directories; days in range but absent are handled per
§13 row B (default: warn and record in report; error with `--strict-days`).

### 5.2 Ordering across instruments

Within a day, the reader performs a k-way merge across the per-instrument Parquet files using the
key `(ts_local_ns, instrument, seq)` (matching §4.3 priority-2 tiebreak). Files are streamed in
row-group batches (default 65 536 rows); the whole day is never required to fit in memory.

### 5.3 Day boundaries

For each day, in order:

1. **Day setup.** Construct a fresh `App` (Blocks 2–7) — no intraday state crosses days. State
   that legitimately spans days (walk-forward parameters, cumulative metrics) lives in Block 8,
   outside the app. `SimClock` is initialized to `manifest.session.open_ns − pre_open_lead_s·1e9`
   (default lead 300 s). Block 7 receives the day's calendar entry from the manifest `session`
   object and schedules its phase timers via `ctx.clock` exactly as in live.
2. **Replay.** Drive the §4.3 loop until archive exhausted, then
   `clock.advance_to(session.close_ns + post_close_lag_s·1e9)` (default lag 300 s) so Block 7's
   FORCE_FLAT and EOD verification timers fire even if the feed ends early.
3. **Day teardown.** Assert broker positions are all zero. Non-flat EOD in backtest is a hard
   error (§13 row I) — it means Block 7 or the fill model is broken. Record the day's fills,
   PnL, and equity marks; append the day's journal; discard the `App`.

### 5.4 What replay emits

`ArchiveReplaySource` re-emits archive rows as the corresponding bus messages with their archived
`ts_exchange_ns`/`ts_local_ns` untouched. It emits nothing else. `SimBroker` independently
consumes the same raw stream (§6.1) — it does **not** depend on Block 2 output, so the broker's
view can never be contaminated by a bug in the blocks under test.

### 5.5 Determinism requirement

**Same archive bytes + same resolved config + same seed ⇒ bit-identical `journal.ndjson`.**

Rules that make this hold:

- No component reads wall clock, `os.urandom`, or global `random`/`numpy.random` state. The root
  seed is split per component as
  `component_seed = int.from_bytes(sha256(f"{root_seed}:{component_name}".encode()).digest()[:8], "big")`;
  `SimBroker` uses `numpy.random.Generator(numpy.random.PCG64(component_seed))` with
  `component_name = "sim_broker"`.
- Journal serialization is canonical: `json.dumps(obj, sort_keys=True, separators=(",", ":"))`,
  one event per line, `\n` terminated, UTF-8; floats via CPython `repr` (shortest round-trip) —
  no locale, no timestamps of the host.
- No thread pools / process pools inside a single replay. Parallelism is allowed only **across**
  independent replays (walk-forward cells, §8.3).
- Verification: `--check-determinism` runs the replay twice and compares
  `sha256(journal.ndjson)`; mismatch is a fatal error reporting the first divergent line
  (§13 row C). The journal hash is always recorded in `report.json` as `journal_sha256`.

---

## 6. Simulated broker (`SimBroker`, the backtest `ExecutionAdapter`)

### 6.1 Internal state

`SimBroker` subscribes to the replayed `BookSnapshot`/`BookDelta`/`TradePrint` stream and
maintains its own book replica per instrument (reusing Block 2's book-builder as a plain library
class — not the Block 2 task under test). It tracks: resting simulated orders, per-instrument
position, cash, cumulative fees, and a pending-delivery queue of broker events with delivery
timestamps (§4.3 priority 1).

### 6.2 Latency model (configurable, seeded)

Every `submit`/`cancel` and every generated fill report is delayed:

- `ack_latency`: drawn per order at submit; the order becomes **active** (eligible to match) at
  `t_active = t_submit + ack_latency`; the `BrokerAck` is delivered at `t_active`.
- `fill_report_latency`: drawn per fill; `BrokerFill` is delivered at `t_fill + fill_report_latency`.
- `cancel_latency`: drawn per cancel; the order stops matching at `t_cancel_effective =
  t_cancel + cancel_latency`; `BrokerCancelAck` delivered then. Fills occurring before
  `t_cancel_effective` stand (realistic cancel race).

Distribution config (one per latency kind), all draws from the `sim_broker` RNG stream in
submit/cancel/fill occurrence order:

| `distribution` | params | draw |
|---|---|---|
| `lognormal` (default) | `median_ms: float`, `sigma: float` | `exp(N(ln(median_ms), sigma))` ms |
| `fixed` | `value_ms: float` | constant (use in tests) |

Defaults: ack `{lognormal, median_ms: 20, sigma: 0.5}`, fill_report `{lognormal, median_ms: 10,
sigma: 0.5}`, cancel `{lognormal, median_ms: 20, sigma: 0.5}`. Draws are made even when a value
turns out unused, so the RNG stream position is a pure function of the order/cancel/fill sequence.

### 6.3 Marketable orders — book walk with slippage haircut

An order is *marketable* if it is `MARKET`, or `LIMIT` whose limit crosses the current opposite
best at `t_active`. Matching runs at `t_active` against the broker book state as of the latest
archive event `≤ t_active`:

1. Walk the opposite side best-first. At each level `(p_k, s_k)` fill
   `min(remaining, floor(s_k * level_take_fraction))` shares (`level_take_fraction` default 1.0;
   set < 1 to be extra conservative about displayed size).
2. Apply the extra-slippage haircut to each level's price:
   `p_fill = p_k * (1 + slippage_haircut_bps/1e4)` for buys,
   `p_k * (1 − slippage_haircut_bps/1e4)` for sells. Default `slippage_haircut_bps = 2.0`.
3. `MARKET`: walk at most `max_market_levels` levels (default 5); any remainder is rejected with
   reason `LIQUIDITY` (conservative: no fantasy depth). Marketable `LIMIT`: walk levels with
   `p_k` within limit (haircut may NOT push a fill through the limit — cap `p_fill` at the limit
   price); remainder rests at the limit per §6.4.
4. Each level consumed produces one `BrokerFill{qty, price=p_fill}`; taker fee per §6.5.
5. Walked liquidity is remembered per (instrument, side, price) and **not** reused by another
   simulated order until an archive event updates that level (no self-replenishing book).

### 6.4 Passive orders — trade-through with queue-position penalty

A resting limit order (buy at `p`; sell is mirrored) fills **only** on `TradePrint` evidence:

- **Placement.** At `t_active`, set `queue_ahead = displayed size at price p on our side`
  (0 if the level does not exist). Conservative: we always join the back; later size increases at
  `p` do NOT increase `queue_ahead` (they are behind us), and cancellations ahead of us are
  ignored (`queue_ahead` only decreases via trades). Level-removal deltas do not touch
  `queue_ahead` either — trades are the only decrement.
- **Trade at our price** (`TradePrint.price == p`, aggressor `SELL` for our buy):
  ```
  eligible     = max(0, v − queue_ahead)                # v = print size
  queue_ahead  = max(0, queue_ahead − v)
  fill_qty     = min(remaining, floor(eligible * (1 − queue_position_penalty)))
  ```
  Default `queue_position_penalty = 0.25`. Fill price = `p` exactly (no haircut on passive
  fills; the penalty is the conservatism). Maker fee per §6.5.
- **Trade through our price** (`TradePrint.price < p` for our buy): the market traded through our
  level ⇒ fill the entire `remaining` at `p` (maker fee). No penalty — a through-print is
  unambiguous evidence.
- No fills from book deltas alone; no fills while `t_sim < t_active` or after
  `t_cancel_effective`.

### 6.5 Fees

`fee = qty * price * fee_bps/1e4 + qty * fee_per_share`, with separate
`taker_fee_bps` (default 1.0), `maker_fee_bps` (default 0.2), `fee_per_share` (default 0.0),
`min_fee_per_order` (default 0.0, applied to the per-order fee total). Fees are attached to each
`BrokerFill.fee` and deducted from cash.

### 6.6 Order lifecycle events

`submit` → (at `t_active`) `BrokerAck` → zero or more `BrokerFill` → terminal
`BrokerFill(last=True)` when remaining = 0, or `BrokerCancelAck`, or `BrokerReject{reason}`
(reasons: `LIQUIDITY`, `UNKNOWN_INSTRUMENT`, `BAD_QTY` for qty ≤ 0). `positions()` returns the
broker-side position map (used by Block 7 EOD verification, exactly as live).

### 6.7 Parameter summary (all under `fill_model:` / `latency:` / `fees:` in config, §12)

| parameter | type | default | range |
|---|---|---|---|
| `slippage_haircut_bps` | float | 2.0 | [0, 50] |
| `level_take_fraction` | float | 1.0 | (0, 1] |
| `max_market_levels` | int | 5 | [1, 50] |
| `queue_position_penalty` | float | 0.25 | [0, 1) |
| `taker_fee_bps` | float | 1.0 | [0, 20] |
| `maker_fee_bps` | float | 0.2 | [0, 20] |
| `fee_per_share` | float | 0.0 | [0, 0.1] |
| `min_fee_per_order` | float | 0.0 | [0, 10] |
| latency distributions | table §6.2 | §6.2 | median [0, 5000] ms, sigma [0, 2] |

---

## 7. Metrics (exact formulas)

Let `capital` = configured deployed capital (constant), `D` = number of replayed days,
`ANN = 252`. Daily PnL `pnl_d` = realized PnL net of all fees for day `d` (positions are always
flat at EOD, so realized = total). Daily return `r_d = pnl_d / capital`.

| metric | formula |
|---|---|
| `net_return` | `∏_{d=1..D} (1 + r_d) − 1` |
| `net_return_ann` | `(1 + net_return)^(ANN / D) − 1` |
| `sharpe` | `mean(r_d) / std(r_d, ddof=1) * sqrt(ANN)`; define `sharpe = 0.0` if `D < 2` or `std == 0`. Risk-free = 0 (flat overnight). |
| `max_drawdown` | Equity curve `E_t = capital + cumulative net PnL, marked to mid`, sampled every 60 s of event time during TRADING plus at every fill and at EOD. `max_dd = max_t (max_{s≤t} E_s − E_t) / max_{s≤t} E_s` (positive fraction). |
| `hit_rate` | A *round trip* = maximal interval per instrument where position ≠ 0 (flat → flat). `hit_rate = #{round trips with net PnL > 0} / #round trips` (PnL == 0 counts as a loss). |
| `trades_per_day` | `#round trips / D`. |
| `turnover` | `mean_d ( Σ_{fills in d} |qty * price| / capital )` (daily gross traded notional over capital). |
| `total_fees` / `fees_bps_of_notional` | `Σ fees`; `1e4 * Σ fees / Σ |qty*price|`. |

### 7.1 Capacity estimate — participation-rate method

For each parent order `i` (one `OrderRequest`) with filled quantity `q_i > 0`, let `V_i` = total
archived `TradePrint` volume in that instrument during `[t_submit_i, t_terminal_i]` (terminal =
last fill / cancel-ack / reject), and `p_i = q_i / max(V_i, 1)` its realized participation. Then:

```
capacity_multiplier = participation_cap / quantile({p_i}, 0.95)
capacity_estimate   = capital * capacity_multiplier
```

`participation_cap` default 0.05. If there are no filled orders, `capacity_estimate = 0` and the
report flags `capacity_undefined: true`. Interpretation: the capital at which the strategy's 95th
percentile per-order participation would hit the cap.

### 7.2 Benchmark (computed from the archive)

Equal-weight intraday buy-and-hold over the configured universe:

- `mid_open(d, i)` = mid of the first valid (uncrossed, both sides present) book state of
  instrument `i` at or after `session.open_ns + benchmark_open_offset_s·1e9` (default 60 s, skips
  the opening auction).
- `mid_close(d, i)` = mid of the last valid book state at or before Block 7's FORCE_FLAT
  deadline for day `d`.
- `b_d = mean_i ( mid_close(d,i) / mid_open(d,i) − 1 )`, skipping instruments with no valid marks
  (if all skipped, `b_d = 0` and the day is flagged).
- `benchmark_return = ∏(1 + b_d) − 1`; `alpha_d = r_d − b_d`;
  `alpha = net_return − benchmark_return`.
- **"Beats benchmark after costs"** over a set of days S ⇔
  `∏_{d∈S}(1 + r_d) > ∏_{d∈S}(1 + b_d)` with `r_d` net of all simulated fees and slippage.

---

## 8. Walk-forward calibration

### 8.1 Fold construction

Inputs: ordered list of available trading days `days[0..D-1]` in the run range,
`train_days` (default 20), `validation_days` (default 5), `step_days` (default 5). Rolling
window, no gaps, validation never overlaps its own train window:

```
n_folds = floor((D − train_days − validation_days) / step_days) + 1     # require ≥ min_folds
fold k (0-based):
  train      = days[k*step : k*step + train_days]
  validation = days[k*step + train_days : k*step + train_days + validation_days]
```

Require `n_folds ≥ min_folds` (default 4); otherwise the calibrate command errors (§13 row F).
Trailing days that don't fill a whole fold are unused (reported as `unused_days`).

### 8.2 Parameter search space format

YAML file; every entry names a Block 4 parameter (dotted path into the `params` object, §9.2) and
is either an explicit grid or a range:

```yaml
search_space:
  ofi_z_entry:        {grid: [1.5, 2.0, 2.5, 3.0]}
  ofi_z_exit:         {min: 0.25, max: 1.0, step: 0.25}     # → [0.25, 0.5, 0.75, 1.0]
  horizon_s:          {grid: [30, 60, 120]}
  regime_vr_min:      {grid: [1.0, 1.1]}
fixed:                                # passed through, not searched
  cooldown_s: 30
```

Range entries expand to `[min, min+step, …]` inclusive of `max` when
`(max − min)/step` is integral (validation error otherwise). Types are preserved (int grids stay
int). The candidate set is the full Cartesian product; its size is reported and MUST be
≤ `max_candidates` (default 4096) or the command errors.

### 8.3 Search method

Exhaustive **grid search** (v1; no random/Bayesian search). For each fold `k` and each candidate
`θ`: replay the train window with `θ` (full pipeline, §5, same seed) and compute the objective
(§8.4) on train days. Winner `θ*_k = argmax J` (ties broken by first candidate in deterministic
Cartesian-product order: parameters sorted by name, values in declared order). Then replay the
validation window once with `θ*_k` and record validation metrics. Independent (fold, candidate)
replays MAY run in parallel (`--jobs`); each replay is internally single-threaded (§5.5) and
results are reduced in deterministic (fold, candidate) order, so `--jobs` does not affect output.

### 8.4 Objective function

Computed on the window's days, net of all simulated costs:

```
J(θ) = net_return_ann(θ) − dd_penalty_lambda * max_drawdown(θ)
```

`dd_penalty_lambda` default 2.0 (i.e. one point of max-DD costs two points of annualized
return). If the window produced fewer than `min_trades_per_fold` round trips, `J = −inf`
(candidate ineligible on that fold).

### 8.5 Overfitting guards

1. **Minimum trades.** `min_trades_per_fold` (default 50) round trips required in each **train**
   window for a candidate to be eligible (§8.4), and in each **validation** window for the fold
   to count as *valid*. If the fraction of valid folds is `< min_valid_fold_frac` (default 0.8),
   promotion is refused with reason `INSUFFICIENT_TRADES`.
2. **Parameter stability.** For every searched scalar parameter `p` with search values spanning
   `[lo_p, hi_p]` (grid min/max), compute the winners' dispersion across folds:
   `stability_p = std({θ*_k[p]}, ddof=0) / (hi_p − lo_p)` (if `hi_p == lo_p`, `stability_p = 0`).
   Require `stability_p ≤ stability_max` (default 0.25) for **all** searched parameters; any
   violation refuses promotion with reason `UNSTABLE_PARAMS` listing offenders. (Rationale: if
   the winning threshold jumps across the grid fold-to-fold, the "edge" is fit noise.)

### 8.6 Promotion rule (backtest gate)

Let valid folds be `K_v`. Promote iff **all** hold:

1. Guards §8.5 pass.
2. `#{k ∈ K_v : θ*_k beats benchmark after costs on validation days (§7.2)} / |K_v|
   ≥ promote_min_beat_frac` (default 0.70).
3. Pooled validation days (all valid folds' validation days, each replayed with its own fold's
   `θ*_k`) satisfy: `sharpe ≥ promote_min_sharpe` (default 1.5) and
   `max_drawdown ≤ promote_max_dd` (default 0.05).

The **promoted parameter set** is `θ*_last` — the winner on the most recent fold's train window
(freshest data). The decision and every threshold are recorded in the report.

---

## 9. Output artifacts

### 9.1 `BacktestReport` schema

`report.json`, canonical JSON (§5.5 serialization). JSON-Schema (draft 2020-12) — implement as a
frozen dataclass `BacktestReport` in `report.py` with `to_json()`/`from_json()`:

```json
{
  "$id": "bot/backtest-report/1",
  "type": "object",
  "required": ["schema_version", "run_id", "created_at", "code_revision", "config_hash",
               "seed", "archive", "params", "metrics", "benchmark", "fold_results",
               "promotion", "journal_sha256"],
  "properties": {
    "schema_version": {"const": 1},
    "run_id": {"type": "string"},
    "created_at": {"type": "string", "format": "date-time"},
    "code_revision": {"type": "string"},
    "config_hash": {"type": "string"},
    "seed": {"type": "integer"},
    "archive": {"type": "object", "required": ["root", "date_from", "date_to",
                "instruments", "days_replayed", "days_missing"]},
    "params": {"type": "object"},
    "metrics": {"type": "object", "required": ["net_return", "net_return_ann", "sharpe",
                "max_drawdown", "hit_rate", "trades_per_day", "turnover", "total_fees",
                "capacity_estimate", "round_trips"]},
    "benchmark": {"type": "object", "required": ["benchmark_return", "alpha",
                  "beats_benchmark"]},
    "fold_results": {"type": "array", "items": {"type": "object",
      "required": ["fold", "train", "validation", "chosen_params", "train_objective",
                   "validation_metrics", "validation_benchmark_return", "beats_benchmark",
                   "valid"]}},
    "promotion": {"type": "object", "required": ["promoted", "reasons", "thresholds",
                  "param_file"]},
    "journal_sha256": {"type": ["string", "null"]}
  }
}
```

Example (calibrate run, abbreviated):

```json
{"schema_version": 1,
 "run_id": "wf-20260702-093011-a1b2c3d4",
 "created_at": "2026-07-02T09:30:11Z",
 "code_revision": "4f9c2e1",
 "config_hash": "sha256:9c1d…",
 "seed": 42,
 "archive": {"root": "/data/archive", "date_from": "2026-05-01", "date_to": "2026-06-12",
             "instruments": ["XYZA", "XYZB"], "days_replayed": 30, "days_missing": []},
 "params": {"ofi_z_entry": 2.0, "ofi_z_exit": 0.5, "horizon_s": 60,
            "regime_vr_min": 1.1, "cooldown_s": 30},
 "metrics": {"net_return": 0.0412, "net_return_ann": 0.4051, "sharpe": 2.31,
             "max_drawdown": 0.021, "hit_rate": 0.543, "trades_per_day": 41.2,
             "turnover": 3.7, "total_fees": 1843.20, "capacity_estimate": 2400000.0,
             "round_trips": 1236},
 "benchmark": {"benchmark_return": 0.0111, "alpha": 0.0301, "beats_benchmark": true},
 "fold_results": [
   {"fold": 0, "train": ["2026-05-01", "2026-05-14"], "validation": ["2026-05-15", "2026-05-21"],
    "chosen_params": {"ofi_z_entry": 2.0, "ofi_z_exit": 0.5, "horizon_s": 60,
                      "regime_vr_min": 1.1},
    "train_objective": 0.3812, "validation_metrics": {"net_return": 0.0071, "sharpe": 1.9,
    "max_drawdown": 0.008, "round_trips": 212},
    "validation_benchmark_return": 0.0018, "beats_benchmark": true, "valid": true}],
 "promotion": {"promoted": true, "reasons": [],
   "thresholds": {"promote_min_beat_frac": 0.7, "promote_min_sharpe": 1.5,
                  "promote_max_dd": 0.05, "min_trades_per_fold": 50,
                  "min_valid_fold_frac": 0.8, "stability_max": 0.25},
   "param_file": "params/promoted/params-20260702-093011-a1b2c3d4.json"},
 "journal_sha256": null}
```

(`journal_sha256` is per-replay; for calibrate runs it is `null` at top level and recorded per
fold; for `replay` runs it is the single journal's hash.)

### 9.2 Promoted parameter file (canonical schema; the Block 4 contract)

Design.md only says "promoted, versioned parameter files consumed by Block 4"; this schema is
authoritative and Block 4's spec MUST load exactly this:

```json
{
  "version": 1,
  "created_at": "2026-07-02T09:30:11Z",
  "code_revision": "4f9c2e1",
  "config_hash": "sha256:9c1d…",
  "params": {
    "ofi_z_entry": 2.0,
    "ofi_z_exit": 0.5,
    "horizon_s": 60,
    "regime_vr_min": 1.1,
    "cooldown_s": 30
  },
  "fold_results": [
    {"fold": 0, "validation": ["2026-05-15", "2026-05-21"],
     "chosen_params": {"ofi_z_entry": 2.0, "ofi_z_exit": 0.5, "horizon_s": 60,
                       "regime_vr_min": 1.1},
     "validation_net_return": 0.0071, "validation_sharpe": 1.9,
     "beats_benchmark": true, "valid": true}
  ]
}
```

Field semantics: `version` — schema version int, currently 1; `created_at` — UTC ISO-8601;
`code_revision` — git SHA of the code that produced (and must consume) it; `config_hash` —
`"sha256:" + sha256(canonical-JSON of resolved backtest config)`; `params` — flat
name → int|float|str map handed verbatim to Block 4 (searched winners from `θ*_last` merged over
`fixed:`); `fold_results` — evidence trail (Block 4 ignores it; Block 9 displays it).

Block 4 MUST validate on load: `version == 1`, `params` non-empty, every expected parameter name
present, and MUST refuse the file (fail toward *no trading*, design §4.3) on any validation
error. Files are immutable once written; filename
`params-<YYYYMMDDHHMMSS>-<first 8 hex of sha256(file bytes minus itself: hash of canonical params+created_at)>.json`;
implement `hash8 = sha256(canonical_json({"params": params, "created_at": created_at}))[:8]`.
`params/promoted/CURRENT` is a one-line text file containing the filename of the active set —
written atomically (write temp + `os.replace`) by the promote step; Block 4 reads `CURRENT` at
startup.

### 9.3 Run directory layout

```
<out>/runs/<run_id>/
  report.json
  config.snapshot.yaml
  journal.ndjson              # replay mode; calibrate keeps per-fold journals only with --keep-journals
  fills.parquet               # columns: ts_ns, instrument, side, qty, price, fee, order_id, intent_id, liquidity ∈ {MAKER, TAKER}
  equity.parquet              # columns: ts_ns, equity, position_notional
  folds/<k>/{train,validation}/report.json          # calibrate mode
<out>/params/promoted/
  params-<ts>-<hash8>.json
  CURRENT
```

`run_id = "<mode>-<YYYYMMDD-HHMMSS>-<hash8 of config_hash+seed+range>"`. `created_at`/`run_id`
wall-clock values appear only in `report.json`/filenames, never inside `journal.ndjson` (§5.5).

---

## 10. Promotion pipeline stages

Design §4.5 names the pipeline; the stage gates live here as config (`promotion:` section, §12).
Block 8 evaluates stage 1 automatically; stages 2–4 are evaluated by running `backtest report`
over the journals those stages produce (paper/live journals are format-identical). A stage
transition requires **all** listed conditions; any kill-switch event or non-flat EOD during a
stage resets that stage's day counter to zero.

| stage | environment | minimum duration | go/no-go (all required) | config keys (defaults) |
|---|---|---|---|---|
| 1 backtest → paper | this block, walk-forward | per §8.1 folds | §8.6 promotion rule passes | `promote_min_beat_frac: 0.70`, `promote_min_sharpe: 1.5`, `promote_max_dd: 0.05` |
| 2 paper → small live | live data, simulated fills (paper `ExecutionAdapter`) | `paper_min_days: 10` sessions | paper net PnL > 0; paper Sharpe ≥ `paper_min_sharpe: 1.0`; realized-vs-backtest slippage divergence `|slip_paper − slip_bt| / max(slip_bt, 0.5 bps) ≤ paper_max_slip_div: 0.30`; signal-fire-rate ratio paper/backtest within `[0.5, 2.0]`; 0 kill-switch events; 0 non-flat EODs | `paper_min_days`, `paper_min_sharpe`, `paper_max_slip_div`, `paper_fire_rate_band: [0.5, 2.0]` |
| 3 small live → scaled | real orders, `small_live_capital_frac: 0.10` of target capital | `live_min_days: 20` sessions | live Sharpe ≥ `live_min_sharpe: 1.0`; live max DD ≤ `live_max_dd: 0.03`; realized slippage ≤ `live_max_slip_ratio: 1.5` × backtest model; live alpha > 0 over the stage; 0 kill-switch events; 0 non-flat EODs | `live_min_days`, `live_min_sharpe`, `live_max_dd`, `live_max_slip_ratio` |
| 4 scaled | capital stepped `scale_step_frac: 0.25` → 0.50 → 1.00, ≥ `scale_min_days: 10` sessions per step | at each step: Sharpe ≥ `live_min_sharpe`; slippage does not degrade > `scale_max_slip_growth: 0.25` (fraction) vs previous step; capacity check `capital ≤ capacity_estimate` from the latest backtest | `scale_step_frac`, `scale_min_days`, `scale_max_slip_growth` |

Demotion: failing any gate mid-stage drops the parameter set one stage (scaled → small live →
paper → retired) and raises a Block 9 alert. A new promoted parameter file always re-enters at
stage 2.

---

## 11. Public API and CLI

### 11.1 CLI (`python -m bot.backtest`, console script `backtest`)

Global flags: `--config PATH` (required), `--archive DIR` (default from config),
`--out DIR` (default `./bt-out`), `--seed INT` (default 0), `--log-level {DEBUG,INFO,WARN}`
(default INFO).

```
backtest replay --params PATH --from YYYY-MM-DD --to YYYY-MM-DD
                [--instruments A,B,...]        # default: config universe
                [--check-determinism]          # run twice, compare journal hashes
                [--strict-days]                # missing day in range = error
                [--no-checksum]                # skip archive sha256 verification
```
Replays one parameter set over the range; writes a run directory (§9.3). Exit 0 success.

```
backtest calibrate --search PATH --from YYYY-MM-DD --to YYYY-MM-DD
                   [--instruments A,B,...] [--jobs N]          # default N=1
                   [--promote]        # on pass: write param file + update CURRENT
                   [--keep-journals] [--strict-days] [--no-checksum]
```
Full walk-forward per §8. Without `--promote` it is a dry run (decision computed and reported,
nothing written to `params/promoted/`). Exit 0 whether or not promoted (the decision is data,
not an error); the report carries it.

```
backtest report --run DIR [--format {text,json,md}] [--compare DIR2]
```
Renders `report.json`; `--compare` prints a two-column metric diff (used for
backtest-vs-paper divergence checks, stage 2).

Exit codes (all subcommands): `0` success, `2` usage/config validation error, `3` archive/data
error, `4` determinism violation, `5` parameter-file validation error.

### 11.2 Python API (`bot.backtest`)

```python
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class ReplayRequest:
    archive_root: Path
    date_from: date
    date_to: date
    instruments: tuple[str, ...]
    params: dict[str, Any]           # Block 4 parameter map (§9.2 "params")
    config: "BacktestConfig"         # parsed §12 config
    seed: int = 0
    strict_days: bool = False
    verify_checksums: bool = True

@dataclass(frozen=True)
class ReplayResult:
    report: "BacktestReport"
    journal_path: Path | None
    fills_path: Path
    equity_path: Path

def run_replay(req: ReplayRequest, out_dir: Path,
               write_journal: bool = True) -> ReplayResult: ...

def check_determinism(req: ReplayRequest, out_dir: Path) -> bool:
    """Run twice; True iff journal sha256 identical. Raises DeterminismError with
    the first divergent line number and both lines otherwise."""

@dataclass(frozen=True)
class WalkForwardRequest:
    replay: ReplayRequest            # replay.params ignored; search drives params
    search_space: dict[str, Any]     # parsed §8.2 file
    train_days: int = 20
    validation_days: int = 5
    step_days: int = 5
    jobs: int = 1

@dataclass(frozen=True)
class CalibrationResult:
    report: "BacktestReport"
    promoted: bool
    param_file: Path | None          # set iff promoted and promote=True

def run_walk_forward(req: WalkForwardRequest, out_dir: Path,
                     promote: bool = False) -> CalibrationResult: ...

def load_param_file(path: Path) -> dict[str, Any]:
    """Validate per §9.2; raise ParamFileError on any violation.
    Shared with Block 4 (single source of truth for validation)."""

def compute_metrics(fills: "pa.Table", equity: "pa.Table",
                    capital: float, days: int) -> dict[str, float]: ...
```

Exceptions (module `bot.backtest.errors`): `ArchiveError`, `MissingDayError(ArchiveError)`,
`DeterminismError`, `ParamFileError`, `ConfigError` — mapped to exit codes 3/3/4/5/2.

---

## 12. Configuration

Single YAML file; every key shown with type, default, valid range. Unknown keys are a
`ConfigError` (fail closed).

```yaml
backtest:
  archive_root: /data/archive          # str, required, existing dir
  capital: 250000.0                    # float, required, > 0 — deployed capital for sizing/metrics
  universe: [XYZA, XYZB]               # list[str], required, non-empty
  pre_open_lead_s: 300                 # int, default 300, [0, 3600]
  post_close_lag_s: 300                # int, default 300, [0, 3600]
  benchmark_open_offset_s: 60          # int, default 60, [0, 1800]

fill_model:
  slippage_haircut_bps: 2.0            # float, default 2.0, [0, 50]
  level_take_fraction: 1.0             # float, default 1.0, (0, 1]
  max_market_levels: 5                 # int, default 5, [1, 50]
  queue_position_penalty: 0.25         # float, default 0.25, [0, 1)

latency:                               # each: {distribution: lognormal|fixed, ...} §6.2
  ack:         {distribution: lognormal, median_ms: 20.0, sigma: 0.5}
  fill_report: {distribution: lognormal, median_ms: 10.0, sigma: 0.5}
  cancel:      {distribution: lognormal, median_ms: 20.0, sigma: 0.5}

fees:
  taker_fee_bps: 1.0                   # float, default 1.0, [0, 20]
  maker_fee_bps: 0.2                   # float, default 0.2, [0, 20]
  fee_per_share: 0.0                   # float, default 0.0, [0, 0.1]
  min_fee_per_order: 0.0               # float, default 0.0, [0, 10]

metrics:
  participation_cap: 0.05              # float, default 0.05, (0, 1]
  equity_mark_interval_s: 60           # int, default 60, [1, 600]

walk_forward:
  train_days: 20                       # int, default 20, [5, 250]
  validation_days: 5                   # int, default 5, [1, 60]
  step_days: 5                         # int, default 5, [1, 60]
  min_folds: 4                         # int, default 4, [1, 100]
  max_candidates: 4096                 # int, default 4096, [1, 100000]
  dd_penalty_lambda: 2.0               # float, default 2.0, [0, 20]
  min_trades_per_fold: 50              # int, default 50, [1, 100000]
  min_valid_fold_frac: 0.8             # float, default 0.8, (0, 1]
  stability_max: 0.25                  # float, default 0.25, (0, 1]

promotion:
  promote_min_beat_frac: 0.70          # float, default 0.70, (0, 1]
  promote_min_sharpe: 1.5              # float, default 1.5, [0, 10]
  promote_max_dd: 0.05                 # float, default 0.05, (0, 1]
  paper_min_days: 10                   # int, default 10
  paper_min_sharpe: 1.0                # float, default 1.0
  paper_max_slip_div: 0.30             # float, default 0.30
  paper_fire_rate_band: [0.5, 2.0]     # [float, float]
  live_min_days: 20                    # int
  live_min_sharpe: 1.0                 # float
  live_max_dd: 0.03                    # float
  live_max_slip_ratio: 1.5             # float
  small_live_capital_frac: 0.10        # float, (0, 1]
  scale_step_frac: 0.25                # float, (0, 1]
  scale_min_days: 10                   # int
  scale_max_slip_growth: 0.25          # float

paths:
  out_dir: ./bt-out                    # str
  promoted_dir: ./bt-out/params/promoted   # str — Block 4 reads CURRENT here
```

`config_hash = "sha256:" + sha256(canonical JSON of the fully resolved config)` (defaults
applied, keys sorted). It is stamped into every report and param file.

---

## 13. Error handling

| # | condition | detection | action | exit |
|---|---|---|---|---|
| A | corrupt archive file | Parquet read error, sha256 mismatch vs manifest, `ts_local_ns` regression, non-monotone `seq`, first event not SNAPSHOT/STATUS | abort run; log file, row/byte offset, expected vs actual hash; nothing partial written except an `ERROR` line in the run log | 3 |
| B | missing day in requested range | date directory absent or manifest missing | default: WARN, record in `report.archive.days_missing`, continue; with `--strict-days`: abort | 0 / 3 |
| C | non-deterministic component | `--check-determinism` double run: `sha256(journal₁) ≠ sha256(journal₂)` | abort; report first divergent line number and both journal lines verbatim; name the emitting block from the event's `source` field | 4 |
| D | parameter file validation failure | `load_param_file`: bad JSON, `version != 1`, missing/empty `params`, wrong value types | `ParamFileError` with field-level message; never partially load | 5 |
| E | search-space invalid | non-integral `(max−min)/step`, empty grid, candidate count > `max_candidates`, unknown param name (not accepted by Block 4 schema) | `ConfigError` before any replay starts | 2 |
| F | too few days for folds | `n_folds < min_folds` (§8.1) | `ConfigError` stating D, train/val/step, and required minimum days | 2 |
| G | manifest/instrument mismatch | requested instrument absent from a day's manifest | treat as missing data for that instrument that day: WARN + report flag; `--strict-days` escalates | 0 / 3 |
| H | internal clock regression | `advance_to(t)` with `t < t_sim`; broker delivery scheduled in the past | fatal assertion — programming bug, never swallowed | 1 (crash) |
| I | non-flat EOD in simulation | `SimBroker.positions()` ≠ all-zero after post-close lag | abort run with instrument/qty listed (Block 7 or fill model defect) | 1 (crash) |
| J | zero valid book for benchmark | no uncrossed two-sided state in the marking windows | `b_d` contribution skipped; if all instruments skipped, `b_d = 0`, day flagged `benchmark_degraded` | 0 |

All WARN/ERROR lines go to stderr and to `<run_dir>/run.log`; `report.json` is written only on
success (exit 0).

---

## 14. Test plan

Tests live in `tests/backtest/`. Fixture generator `tests/backtest/synth_archive.py` builds the
tiny archive below.

### 14.1 Fixture: 2-instrument synthetic archive `SYNTH-DAY-1` (date 2026-01-05)

Session open `T0 = 09:30:00` (all times below are offsets from T0; stored as `ts_local_ns`).
Instruments `XYZA`, `XYZB`. Events (after the initial snapshots):

```
XYZA snapshot @ +0.0s : bids [(10.00, 1000), (9.99, 800)]   asks [(10.02, 300), (10.03, 500)]
XYZB snapshot @ +0.0s : bids [(50.00, 200)]                  asks [(50.10, 200)]
XYZA trade    @ +5.0s : price 10.01, size 600, aggressor SELL     (seq 2)
XYZA trade    @ +8.0s : price 10.01, size 700, aggressor SELL     (seq 3)
XYZA trade    @ +9.0s : price 10.00, size 100, aggressor SELL     (seq 4)
XYZB delta    @ +9.0s : ASK 50.10 → size 150                      (seq 2)
```

Config for the worked tests: all latencies `{distribution: fixed, value_ms: 0}` unless stated,
`slippage_haircut_bps = 2.0`, `queue_position_penalty = 0.25`, `taker_fee_bps = 1.0`,
`maker_fee_bps = 0.2`, `level_take_fraction = 1.0`.

### 14.2 T-FILL-1 — marketable walk with haircut (hand-checked numbers)

Submit `BUY 400 XYZA MARKET` at `+1.0s`. Book asks: 300@10.02, 500@10.03.

- Level 1: 300 @ 10.02 × 1.0002 = **10.022004** → notional 3006.6012
- Level 2: 100 @ 10.03 × 1.0002 = **10.032006** → notional 1003.2006
- Avg fill = 4009.8018 / 400 = **10.02450450**; raw-book avg was 10.0225, haircut cost
  = 4009.8018 − 4009.0 = **0.8018** (exactly 2 bps of notional).
- Taker fee = 4009.8018 × 1.0/1e4 = **0.40098018**.

Assert two fills with those exact prices/qtys, fee total, and position 400.

### 14.3 T-FILL-2 — passive queue penalty (hand-checked numbers)

Extend the fixture with delta `BID 10.01 → 1000` at `+0.5s` as XYZA seq 2 (the three trades and
the XYZB delta renumber to seq 3–6), so the 10.01 level exists with displayed size 1000.
Submit `BUY 500 XYZA LIMIT 10.01` at `+1.0s` → `queue_ahead = 1000`, remaining 500.

- Trade @ +5.0s: 600 SELL @ 10.01 → `eligible = max(0, 600 − 1000) = 0`;
  `queue_ahead = 400`; fill 0.
- Trade @ +8.0s: 700 SELL @ 10.01 → `eligible = 700 − 400 = 300`; `queue_ahead = 0`;
  `fill = min(500, floor(300 × 0.75)) = 225` @ 10.01. Maker fee = 225 × 10.01 × 0.2/1e4
  = **0.0450450**. Remaining 275.
- Trade @ +9.0s: 100 SELL @ **10.00 < 10.01** → traded through → fill remaining **275** @ 10.01.
  Maker fee = 275 × 10.01 × 0.2/1e4 = **0.0550550**.

Assert fills (225, 275) at 10.01, fees as above, order terminal-filled at +9.0s, position 500.

### 14.4 T-LAT-1 — latency gating

Same as T-FILL-2 but ack latency `fixed 6000 ms`: `t_active = +7.0s`, so the +5.0s print is
ignored and `queue_ahead` initializes at +7.0s from the then-displayed size at 10.01. With the
fixture (no other deltas), displayed = 1000 → +8.0s print gives `eligible = max(0, 700 − 1000)=0`,
fill 0; +9.0s through-print fills all 500. Assert exactly one 500-share fill at +9.0s.

### 14.5 T-CLOCK-1 — ordering rule

Register a timer at exactly `T0+9.0s`; assert callback runs before the seq-4 trade and the XYZB
delta are delivered, and that XYZA seq 4 (instrument "XYZA" < "XYZB") is delivered before XYZB
seq 2 (equal `ts_local_ns`; §4.3 priority-2 tiebreak).

### 14.6 T-DET-1 — determinism

Replay `SYNTH-DAY-1` twice with seed 7 and a trivial always-trade parameter set;
assert `sha256(journal₁) == sha256(journal₂)`. Then mutate: patch `SimBroker` to draw one extra
RNG sample conditionally on `id(order) % 2` (a deliberate nondeterminism); assert
`check_determinism` raises `DeterminismError` naming the first divergent line.

### 14.7 T-WF-1 — walk-forward toy with exact fold boundaries and promotion decision

25 synthetic trading days `d1..d25` (generator repeats `SYNTH-DAY-1` with a per-day drift knob).
Config: `train_days=10, validation_days=5, step_days=5, min_folds=3` →
`n_folds = floor((25 − 10 − 5)/5) + 1 = 3`:

| fold | train | validation |
|---|---|---|
| 0 | d1–d10 | d11–d15 |
| 1 | d6–d15 | d16–d20 |
| 2 | d11–d20 | d21–d25 |

Search space: `ofi_z_entry {grid: [1.5, 2.0]}` (2 candidates). The generator is rigged so:
winners `θ*_0 = θ*_1 = θ*_2 = {ofi_z_entry: 2.0}` (stability: std = 0 ≤ 0.25 ✓); every train and
validation window produces ≥ `min_trades_per_fold = 50` round trips (all 3 folds valid,
3/3 ≥ 0.8 ✓); validation beats benchmark on folds 0 and 1, loses on fold 2.

Assert: `beat_frac = 2/3 ≈ 0.667 < 0.70` ⇒ **not promoted**, `promotion.reasons ==
["BEAT_FRAC_BELOW_MIN"]`, no param file written even with `--promote`. Re-run with
`promote_min_beat_frac = 0.60` and pooled validation Sharpe ≥ 1.5, max DD ≤ 0.05 ⇒ **promoted**;
assert the param file's `params.ofi_z_entry == 2.0` (= `θ*_last` from fold 2's train window),
`version == 1`, `config_hash` matches, `CURRENT` points at it, and `load_param_file` round-trips.

### 14.8 Remaining enumerated tests

- **T-ARC-1..4**: checksum mismatch → exit 3; missing day default-warn vs `--strict-days`;
  `ts_local_ns` regression → exit 3; first-event-not-snapshot → exit 3.
- **T-FEE-1**: `min_fee_per_order` floor applied per order, not per fill.
- **T-MKT-1**: `MARKET` for 900 with `max_market_levels=2` and asks 300+500 → 800 filled,
  100 rejected `LIQUIDITY`; walked liquidity not reusable until a delta refreshes the level.
- **T-MET-1**: metrics on a hand-built 3-day fills/equity fixture — verify `net_return`
  compounding, Sharpe `mean/std·√252` (ddof=1), max-DD peak logic, hit-rate zero-PnL-is-loss,
  turnover, capacity p95 formula against hand values.
- **T-BM-1**: benchmark marks skip the first 60 s and stop at FORCE_FLAT; crossed-book states
  excluded; all-skipped day flagged.
- **T-EOD-1**: rig fill model to leave a residual position → run crashes with §13 row I.
- **T-PF-1**: `load_param_file` rejects `version: 2`, missing `params`, non-numeric threshold —
  exit 5 via CLI.
- **T-CFG-1**: unknown config key, out-of-range value, non-integral range step → exit 2.
- **T-SAME-1** (guard for §1.3): static check that `bot.backtest` never imports live adapter
  modules, and that Blocks 2–7 modules contain no `time.time`/`datetime.now`/`asyncio.sleep`
  references outside the injected `Clock` (AST-based lint test).

### 14.9 Test acceptance

All tests run in CI, no network, total runtime budget < 120 s. Every numeric assertion above is
exact (floats compared to the stated values with `rel=1e-12`), not "approximately right".

---

## 15. Acceptance criteria checklist

- [ ] Blocks 2–7 run in backtest byte-identical to live; only `Clock`, `MarketDataSource`,
      `ExecutionAdapter` are swapped via `AppContext` (T-SAME-1 passes).
- [ ] Archive reader validates layout, checksums, ordering per §3.4; all §13 rows behave as
      specified with the specified exit codes.
- [ ] `SimClock` + driver implement the §4.3 ordering key exactly (T-CLOCK-1).
- [ ] Same archive + config + seed produces bit-identical `journal.ndjson`;
      `--check-determinism` detects an injected nondeterminism and reports the first divergent
      line (T-DET-1).
- [ ] Fill model reproduces T-FILL-1/T-FILL-2/T-LAT-1/T-MKT-1 hand numbers exactly; passive
      fills occur only on trade prints; walked liquidity is not reused.
- [ ] Fees and latency distributions are config-driven with §6 defaults; latency draws are
      seeded and reproducible.
- [ ] Metrics match §7 formulas on T-MET-1; capacity uses the p95 participation method;
      benchmark computed from archive per §7.2 (T-BM-1).
- [ ] Walk-forward builds folds per §8.1, searches the full grid deterministically, applies the
      §8.4 objective, §8.5 guards, and §8.6 promotion rule (T-WF-1 both branches).
- [ ] `report.json` validates against the §9.1 schema; promoted param file matches §9.2 exactly;
      `CURRENT` updated atomically; `load_param_file` shared with Block 4 rejects invalid files.
- [ ] Promotion pipeline stage gates exist as config keys with §10 defaults and are evaluated by
      `backtest report --compare`.
- [ ] CLI subcommands, flags, and exit codes match §11.1; Python API signatures match §11.2.
- [ ] Every day ends flat in simulation or the run crashes (T-EOD-1).
- [ ] Full test suite green, < 120 s, offline.
