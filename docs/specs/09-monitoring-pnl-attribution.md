# Block 9 — Monitoring, Logging & PnL Attribution: Implementation Specification

Status: v1. Conforms to `docs/design.md` (§3 Block 9, §4 cross-cutting rules, §5 technology
assumptions). Python 3.11+, single process, asyncio.

---

## 1. Overview and responsibilities

Block 9 is the **read-only observer** of the whole system. It consumes every inter-block event,
and produces four artifacts:

1. **Event journal** — the append-only NDJSON system of record. Block 8 replays trading days from
   it; humans debug from it; compliance retention is satisfied by it.
2. **Structured logs** — human/ops-oriented diagnostic log lines (distinct from the journal).
3. **Real-time status endpoint** — an HTTP JSON endpoint on localhost showing positions, PnL,
   feed health, order states, kill-switch status, session phase, and seconds-to-close.
4. **Alerts and the daily PnL attribution report** — per-signal / per-instrument PnL vs. a
   benchmark, with a cost breakdown, answering "did we beat the market today?" with numbers.

### 1.1 Non-responsibilities (hard rules)

- Block 9 **never influences trading decisions**. It publishes no message that any of Blocks 1–7
  consumes. Its only outputs leave the process (files, HTTP responses, webhooks) or stay inside
  Block 9.
- Block 9 **never blocks the trading path**. Publishers deliver events to Block 9 with a
  non-blocking put; if Block 9 lags, events are dropped for Block 9 (and loudly accounted for —
  §8.3), never queued with backpressure against a trading block.
- Block 9 does not size risk, does not send orders, does not talk to the exchange. The kill
  switch belongs to Block 5; Block 9 only *reports* it.
- Block 9 does not compute the authoritative live position/PnL — Block 5's `PositionState` is
  authoritative intraday. Block 9's attribution ledger is an independent end-of-day
  reconstruction from fills, reconciled against Block 5 (§6.7).

---

## 2. Event journal

### 2.1 Envelope schema

Every journaled event is one line of NDJSON (UTF-8, `\n` terminated, no pretty-printing, keys in
the exact order below). One envelope per bus message.

```json
{"ts": 1751461200123456789, "seq": 42, "source_block": 6, "event_type": "Fill", "schema_version": 1, "payload": {"order_id": "O-101", "intent_id": "I-001", "instrument": "XYZ", "qty": 100, "price": 50.0, "fee": 0.1, "ts": 1751461200123001000}}
```

| Field            | Type   | Meaning |
|------------------|--------|---------|
| `ts`             | int    | Journal-write receive time, **nanoseconds since Unix epoch, UTC**, taken by Block 9 (`time.time_ns()`) when the event is dequeued. Payloads keep their own producer timestamps. |
| `seq`            | int    | Journal-global sequence number. Starts at `1` per session (per trading date), increases by exactly 1 per envelope, continues across file parts within the same date. |
| `source_block`   | int    | Producing block, `1`–`8`; `9` for Block 9's self-emitted meta events (§2.6). |
| `event_type`     | str    | Exact PascalCase type name from §3.2. |
| `schema_version` | int    | Envelope+payload schema version. This spec defines version `1`. Readers MUST reject unknown major versions with an error. |
| `payload`        | object | The event body, verbatim as received from the bus (JSON-serializable dict). Block 9 MUST NOT mutate, reorder-semantically, or enrich the payload. |

Serialization rules: `json.dumps(env, separators=(",", ":"), ensure_ascii=False,
allow_nan=False)`. Floats that are NaN/Inf in a payload are a producer bug; Block 9 replaces them
with `null` and increments `malformed_field_count` (§10) rather than crashing.

### 2.2 File naming and directory layout

All paths are under `config.data_root` (default `./data`). `<date>` is the **exchange trading
date** (from Block 7's calendar, provided in config as the session date at startup), format
`YYYY-MM-DD`.

```
data/journal/<date>/journal-<date>.part000.ndjson        # active file
data/journal/<date>/journal-<date>.part001.ndjson        # after size rotation
data/journal/<date>/journal-<date>.part000.ndjson.zst    # after compression (past dates only)
data/journal/<date>/malformed.ndjson                     # quarantined undecodable events (§10)
```

Part numbers are zero-padded to 3 digits, starting at `000`.

### 2.3 Rotation

Rotate (close current part, fsync, open `partNNN+1`) when either:

- current part size ≥ `journal.max_part_bytes` (default 512 MiB), checked after each write; or
- the trading date changes (only relevant if the process runs across midnight — normally one
  process per session).

`seq` does **not** reset on part rotation; it resets only per trading date.

### 2.4 Compression

- The **active date's** files are never compressed.
- At startup and after the daily report is written (§6), a background task compresses every
  `*.ndjson` file from **prior dates** to `*.ndjson.zst` (zstandard, level 10, via the `zstandard`
  package), verifies by decompressing and comparing SHA-256 of the plaintext, then deletes the
  original. If verification fails, keep the original, delete the bad `.zst`, raise a `WARNING`
  alert `JOURNAL_COMPRESS_FAIL`.

### 2.5 fsync policy

The journal writer buffers via normal buffered file I/O and forces durability
(`file.flush()` + `os.fsync(fd)`) when the **first** of these occurs:

1. `journal.fsync_interval_ms` (default **200 ms**) elapsed since last fsync and ≥ 1 event was
   written since;
2. `journal.fsync_max_events` (default **500**) events written since last fsync;
3. the event just written has `event_type` in the **critical set**
   `{Fill, OrderRequest, IntentRejection, KillSwitch, FlattenCommand, EodReport}` — fsync
   immediately after that write;
4. rotation or shutdown.

Rationale: at most 200 ms / 500 non-critical events can be lost on power failure; nothing needed
to reconstruct positions or a kill decision can be.

### 2.6 Block 9 self-emitted meta events

Block 9 journals its own operational events with `source_block: 9`:

- `MonitorLag{dropped_total, dropped_by_type: {event_type: int}, queue_high_watermark}` —
  emitted whenever the drop counter increased since the last `MonitorLag` (at most once per 5 s),
  and at session end.
- `Alert{...}` — every alert raised (§5), payload = the `Alert` dataclass (§7.1) as a dict.
- `JournalCheckpoint{part, events_in_part, last_seq, sha256_hex}` — written as the **final line
  of every part** at rotation/close: `sha256_hex` is the SHA-256 of all prior bytes of the part.
  Used for corruption detection (§10).

### 2.7 Replayability guarantee

The journal is the system of record for Block 8 replays and debugging. Block 9 MUST guarantee:

- **Total order.** Reading a date's parts in part order, lines in file order, yields every event
  in exactly the order Block 9 dequeued them, with `seq` strictly increasing by 1 from 1.
  Because the v1 bus is a single in-process fan-out and Block 9 consumes from one queue, this
  order is a valid interleaving of every block's outputs; Block 8 replays decisions by feeding
  `payload`s back through Blocks 2–7 in `seq` order.
- **Completeness accounting.** If any event was dropped due to consumer lag (§8.3), a
  `MonitorLag` event with nonzero `dropped_total` appears in the journal and
  `journal_integrity.dropped_events > 0` in the daily report. Replays of such a day are marked
  non-bit-exact. Zero drops ⇒ the journal contains **every** bus message of the session.
- **Verifiability.** Each part ends with a `JournalCheckpoint` whose hash lets any reader prove
  the part is untampered/uncorrupted.

---

## 3. Structured logging and the mandatory journaled event set

### 3.1 Log schema (diagnostics — separate from the journal)

Block 9 configures Python `logging` for the whole process with a JSON formatter. One JSON object
per line to `data/logs/bot-<date>.log` and (if `log.stderr: true`) to stderr:

```json
{"ts": "2026-07-02T14:30:00.123456+00:00", "level": "WARNING", "logger": "block9.alerts", "block": 9, "msg": "webhook delivery failed, attempt 2/5", "context": {"alert_id": "a-7f3c", "status": 503}}
```

| Field | Type | Rule |
|---|---|---|
| `ts` | str | ISO-8601 with microseconds, UTC offset. |
| `level` | str | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL`. |
| `logger` | str | Dotted logger name, prefix `blockN.`. |
| `block` | int | Producing block number. |
| `msg` | str | Human text. Never contains data needed for replay — that belongs in the journal. |
| `context` | object | Arbitrary JSON-safe key/values; MAY be omitted. |

Level policy: `DEBUG` off in live by default; `INFO` = lifecycle (start/stop/rotate/report
written); `WARNING` = degraded but operating; `ERROR` = a Block 9 function failed but trading is
unaffected; `CRITICAL` = reserved for events that also raise a `CRITICAL` alert.

### 3.2 Mandatory journaled event set

Every block MUST publish the following events to the bus; Block 9 journals **all** of them. This
table is the contract — payload fields are exactly the design-doc message fields.

| `event_type` | `source_block` | Payload fields (design doc §3) |
|---|---|---|
| `TradeIntent` | 4 | `intent_id`, `instrument`, `ts`, `side ∈ {BUY, SELL}`, `conviction ∈ (0,1]`, `horizon_s`, `entry_type ∈ {MARKET, LIMIT}`, `reason: {feature: value}` |
| `IntentRejection` | 5 | `intent_id`, `rule`, `detail` |
| `OrderRequest` | 5 | `order_id`, `intent_id`, `instrument`, `side`, `qty`, `type`, `limit_price?`, `time_in_force` |
| `Fill` | 6 | `order_id`, `intent_id`, `instrument`, `qty` (signed: + buy, − sell), `price`, `fee`, `ts` |
| `OrderState` | 6 | `order_id`, `state ∈ {NEW, ACKED, PARTIAL, FILLED, CANCELLED, REJECTED}`, `reason?` |
| `ExecutionQuality` | 6 | `intent_id`, `arrival_mid`, `avg_fill_price`, `slippage_bps`, `latency_ms` |
| `PositionState` | 5 | `instrument`, `qty`, `avg_price`, `realized_pnl`, `unrealized_pnl` |
| `FeatureStats` | 3 | `name`, `rolling_mean`, `rolling_std` |
| `SignalHealth` | 4 | `signals_evaluated`, `suppressed`, `fired` |
| `SessionPhase` | 7 | `phase ∈ {PRE_OPEN, OPEN_AUCTION, TRADING, WIND_DOWN, FORCE_FLAT, CLOSED}`, `ts`, `seconds_to_close` |
| `FlattenCommand` | 7 | `mode ∈ {PASSIVE, AGGRESSIVE}`, `deadline_ts` |
| `EodReport` | 7 | `flat_confirmed: bool`, `residual_positions: []` |
| `KillSwitch` | 5 | `reason` |
| `FeedStatus` | 1 | `instrument`, `state ∈ {LIVE, STALE, GAP, DOWN}`, `detail` |
| `BookIntegrity` | 2 | `instrument`, `ok`, `crossed_book`, `staleness_ms` |

Additionally journaled but **not** consumed by dashboard/alerts/attribution: `BookState` sampled
ticks for the **benchmark instrument only** (`event_type: BookState`, `source_block: 2`), needed
for §6.5. Full-universe `BookState`/`BookDelta` raw data is Block 1's archive responsibility, not
the journal's.

An event of unknown `event_type` is journaled verbatim (forward compatibility) and counted in
`unknown_event_count`; it triggers a `WARNING` log the first time per type per session.

---

## 4. Real-time dashboard: HTTP JSON status endpoint

v1 is **not** a UI. It is an HTTP endpoint; render with `curl | jq` or `watch`.

### 4.1 Server

- Implementation: `aiohttp.web` application, listening on
  `http://127.0.0.1:<dashboard.port>` (default port **8790**), bound to `127.0.0.1` only. If the
  project rejects the aiohttp dependency, an equivalent hand-rolled `asyncio.start_server`
  HTTP/1.1 responder (GET only, `Connection: close`) is acceptable; routes and JSON are identical.
- Routes:
  - `GET /healthz` → `200` body `{"ok": true}` whenever the Block 9 tasks are running.
  - `GET /status` → `200` with the schema below, `Content-Type: application/json`.
- The handler serves a **snapshot**: Block 9's consumer task updates an in-memory
  `DashboardState` on every consumed event; the handler serializes the current state. Update
  cadence is therefore event-driven (effectively continuous); the HTTP layer adds no polling loop.

### 4.2 `GET /status` response schema

Every subsection carries `as_of_ts` (int ns, `ts` of the last event that updated it, `null` if
never) and `stale: bool` — `true` if `now_ns − as_of_ts > dashboard.staleness_s × 1e9`
(default 5 s) or `as_of_ts` is null. Clients MUST treat `stale: true` sections as untrusted.

```json
{
  "schema_version": 1,
  "now_ts": 1751461200123456789,
  "session": {
    "as_of_ts": 1751461199000000000, "stale": false,
    "phase": "TRADING", "seconds_to_close": 8100
  },
  "kill_switch": {
    "as_of_ts": null, "stale": true,
    "engaged": false, "reason": null, "ts_engaged": null
  },
  "feeds": {
    "as_of_ts": 1751461200000000000, "stale": false,
    "by_instrument": {"XYZ": "LIVE", "ABC": "LIVE", "SPY": "LIVE"},
    "worst_state": "LIVE"
  },
  "positions": {
    "as_of_ts": 1751461199500000000, "stale": false,
    "by_instrument": {
      "XYZ": {"qty": 100, "avg_price": 50.0, "realized_pnl": 0.0, "unrealized_pnl": 12.0}
    },
    "gross_exposure_qty": 100
  },
  "pnl": {
    "as_of_ts": 1751461199500000000, "stale": false,
    "realized": 4.9, "unrealized": 12.0, "total": 16.9,
    "daily_loss_limit": -1000.0, "loss_limit_used_frac": 0.0
  },
  "orders": {
    "as_of_ts": 1751461198000000000, "stale": false,
    "open": {"O-105": {"state": "ACKED", "instrument": "XYZ"}},
    "counts_today": {"NEW": 5, "ACKED": 5, "PARTIAL": 1, "FILLED": 4, "CANCELLED": 1, "REJECTED": 0}
  },
  "signals": {
    "as_of_ts": 1751461195000000000, "stale": false,
    "evaluated": 12000, "suppressed": 40, "fired": 3, "rejections_today": 1
  },
  "alerts": {
    "as_of_ts": 1751461100000000000, "stale": false,
    "active": [{"rule_id": "FEED_STALE", "severity": "WARNING", "instrument": "ABC",
                "since_ts": 1751461100000000000, "count": 2}]
  },
  "monitor": {
    "queue_depth": 3, "queue_capacity": 65536, "dropped_events": 0,
    "journal": {"part": 0, "last_seq": 1042, "degraded": false}
  }
}
```

Field semantics:
- `kill_switch.engaged` becomes `true` on the first `KillSwitch` event and stays `true` for the
  session; `reason`/`ts_engaged` from that event.
- `feeds.worst_state`: worst of all instruments, order `LIVE < STALE < GAP < DOWN`.
- `pnl.*` mirrors the latest `PositionState` stream (sum over instruments), not the attribution
  ledger. `loss_limit_used_frac = max(0, −total) / |daily_loss_limit|`, clamped to `[0, ∞)`.
- `orders.open`: orders whose last `OrderState` is `NEW`, `ACKED`, or `PARTIAL`.
- `alerts.active`: alerts whose condition has not cleared (§5.5) — max 50, newest first.

---

## 5. Alerting

### 5.1 Severities

`AlertSeverity = INFO | WARNING | CRITICAL` (enum, §7.1). `CRITICAL` means "a human must look
now"; `WARNING` means "look within minutes"; `INFO` is record-keeping (report written, session
started).

### 5.2 Alert rules (exact)

All thresholds are config (§9); defaults below. Each rule has a stable `rule_id`. Rules are
evaluated inside the consumer task, on the events named. "PnL" below means the summed
`PositionState` totals (realized + unrealized).

| `rule_id` | Severity | Exact trigger | Cleared when |
|---|---|---|---|
| `FEED_DOWN` | CRITICAL | Some instrument's `FeedStatus.state ∈ {DOWN, GAP}` continuously for > `alerts.feed_down_s` (default **5.0** s). Timer starts at the first non-LIVE event, checked by a 1 s ticker. | A `FeedStatus` with `state = LIVE` for that instrument. |
| `FEED_STALE` | WARNING | `FeedStatus.state = STALE` continuously for > `alerts.feed_stale_s` (default **10.0** s). | Same. |
| `LOSS_WARN` | WARNING | `loss_limit_used_frac ≥ alerts.loss_warn_frac` (default **0.80**). | Frac < threshold − 0.05 (hysteresis). |
| `LOSS_CRIT` | CRITICAL | `loss_limit_used_frac ≥ alerts.loss_crit_frac` (default **0.95**). | Never auto-clears intra-session. |
| `KILL_SWITCH` | CRITICAL | Any `KillSwitch` event. | Never auto-clears. |
| `EOD_NOT_FLAT` | CRITICAL | `EodReport.flat_confirmed == false`, **or** no `EodReport` received within `alerts.eod_grace_s` (default **120** s) after `SessionPhase.phase == CLOSED`. | Never auto-clears. |
| `BOOK_CROSSED` | WARNING→CRITICAL | `BookIntegrity.crossed_book == true`; escalate to CRITICAL if still crossed after **5.0** s. | `BookIntegrity.ok == true`. |
| `DIVERGENCE_SLIPPAGE` | WARNING / CRITICAL | §5.3 rule (a). | Statistic back inside the 2σ band. |
| `DIVERGENCE_HITRATE` | WARNING / CRITICAL | §5.3 rule (b). | Statistic back inside the 2σ band. |
| `JOURNAL_DEGRADED` | CRITICAL | Journal write fails (disk full/IO error, §10) or `dropped_events` transitions 0 → >0. | Never auto-clears. |
| `MONITOR_LAG` | WARNING | `queue_depth > 0.8 × queue_capacity`. | Depth < 0.5 × capacity. |
| `REPORT_WRITTEN` | INFO | Daily report persisted. | n/a. |

### 5.3 Backtest/live divergence — exact statistical rules

Baselines come from the promoted parameter file (Block 8 output), which MUST contain
`bt_slippage_mean_bps` (μ), `bt_slippage_std_bps` (σ), `bt_hit_rate` (p₀). Block 9 reads them
from `config.divergence.baseline_file` (JSON).

**(a) Slippage.** Keep a deque of the last `N = divergence.slippage_window` (default **50**)
`ExecutionQuality.slippage_bps` values (adverse = positive; convention:
`slippage_bps = 1e4 × side_sign × (avg_fill_price − arrival_mid) / arrival_mid`,
`side_sign = +1` buy, `−1` sell — produced by Block 6, consumed as-is). Once the deque is full,
on each new value compute the mean `s̄`. This is a one-sided z-test of `s̄` against the
backtest's sampling distribution of the mean:

- `WARNING` if `s̄ > μ + 2·σ/√N`
- `CRITICAL` if `s̄ > μ + 3·σ/√N`

Worked defaults: μ = 1.5, σ = 2.0, N = 50 ⇒ WARNING above 1.5 + 2·2/7.0711 = **2.066 bps**,
CRITICAL above **2.349 bps**.

**(b) Hit rate.** A **closed intent** is an entry `TradeIntent` all of whose opened lots (§6.2)
have been fully matched. It is a **hit** iff its attributed net PnL (§6.3, incl. both legs' fees)
> 0. Keep the last `M = divergence.hitrate_window` (default **100**) closed intents; once full,
let `h` = hits, `p̂ = h/M`, `se = sqrt(p₀(1−p₀)/M)`. One-sided (underperformance only):

- `WARNING` if `p̂ < p₀ − 2·se`
- `CRITICAL` if `p̂ < p₀ − 3·se`

Worked: p₀ = 0.54, M = 100 ⇒ se = 0.04984; WARNING below 0.4403 (h ≤ 44), CRITICAL below
0.3905 (h ≤ 39).

### 5.4 Delivery channels

Every raised alert is delivered to all three channels:

1. **Journal**: `Alert` meta event (§2.6) — with fsync-critical treatment if severity is CRITICAL.
2. **Log**: level `WARNING`→`WARNING`, `CRITICAL`→`CRITICAL`, `INFO`→`INFO`, logger
   `block9.alerts`.
3. **Webhook**: `POST config.alerts.webhook_url` with body = the `Alert` dataclass as JSON,
   `Content-Type: application/json`, timeout `alerts.webhook_timeout_s` (default 5.0). Retry on
   any exception or non-2xx: exponential backoff 1, 2, 4, 8, 16 s (max 5 attempts total), then
   drop with an `ERROR` log. Deliveries run in a dedicated asyncio task fed by a bounded queue
   (size 1000, drop-oldest on overflow with `ERROR` log). If `webhook_url` is null, channel 3 is
   disabled.

### 5.5 Deduplication and throttling

- **Alert key** = `(rule_id, instrument or "")`. While a key's condition persists, re-raises
  within `alerts.dedup_window_s` (default **300** s) are merged: increment `count`, keep
  `first_ts`, do not re-deliver — **unless severity escalated**, which always delivers.
- A persisting key re-delivers a reminder every `alerts.renotify_s` (default **900** s).
- Global cap: at most `alerts.max_per_minute` (default **30**) webhook deliveries per rolling
  minute; overflow is coalesced into one `WARNING` alert `ALERTS_SUPPRESSED` with the count.
- Clearing a key (per §5.2 "cleared when") emits one `INFO` delivery `"<rule_id> cleared"` and
  removes it from `alerts.active`.

---

## 6. Daily PnL attribution — exact computation

Runs incrementally during the session; finalized when `SessionPhase.phase == CLOSED` **and**
`EodReport` arrives (or the `eod_grace_s` timer fires, in which case the report is written with
`eod.flat_confirmed: false`).

### 6.1 Signal name resolution

For each `TradeIntent`, `signal_name` is:

1. `intent.reason["signal"]` if present and a string; else
2. `"+".join(sorted(intent.reason.keys()))` (deterministic composite name); else
3. `"__unknown__"` if `reason` is empty/missing.

Fills whose `intent_id` matches no journaled `TradeIntent` (e.g. flatten orders from Block 7,
`intent_id` like `FLATTEN-*`) resolve to signal `"__flatten__"` **for their own fee/slippage
bookkeeping only** — see §6.3 for where their PnL lands.

### 6.2 Per-fill lot ledger (FIFO)

Per instrument, maintain a FIFO deque of open lots
`Lot{qty_open: float (signed), price: float, intent_id: str, signal: str, ts: int}`.

On each `Fill` (signed `qty`: + buy, − sell):

- If the fill's sign equals the current net position's sign (or position is zero): append a new
  lot for the full qty, tagged with the fill's `intent_id`/`signal`.
- Else it is (partly) closing: match against the oldest lots first. For each matched quantity
  `q = min(|fill remaining|, |lot remaining|)`, realize
  `gross = (close_price − open_price) × q × direction`, where `direction = +1` if the lot is
  long, `−1` if short; reduce both; pop exhausted lots. If the fill's qty exceeds all open lots
  (position flip), the residual opens a new lot in the fill's direction tagged with the fill's
  own intent/signal.

### 6.3 Attribution of PnL, fees, and slippage

- **Gross round-trip PnL** of each matched quantity is attributed to the **opening lot's**
  `intent_id` and `signal_name` (the signal that initiated the risk owns the outcome, including
  exits performed by other intents or by `__flatten__` fills).
- **Fees**: an opening fill's fee goes to its own signal. A closing fill's fee is pro-rated
  across the matched quantities and attributed to each matched lot's opening signal. (Thus
  flatten-fill fees land on the signal being flattened, not on `__flatten__`.)
- **Slippage cost** (informational — it is already embedded in fill prices and MUST NOT be
  subtracted from net PnL again): per fill,
  `slippage_cost = side_sign × (fill_price − arrival_mid) × |qty|`, using `arrival_mid` from the
  `ExecutionQuality` event with the same `intent_id`; if none arrived, use the benchmark rule's
  fallback: the last journaled `BookState.mid` for that instrument before the fill's `ts`, else
  `0.0` with `quality_flags` noting it. Attribution follows the same rule as fees (opening fill →
  own signal; closing fill → matched opening signals pro-rata).
- **Per-signal net PnL** = Σ attributed gross − Σ attributed fees.
- **Per-instrument** aggregation: identical sums grouped by instrument instead of signal.
- End of day the book is flat (Block 7 guarantees it); if residual lots remain (EOD not flat),
  mark them to the last known mid, report the mark under `per_signal[*].unrealized_residual`, and
  the `EOD_NOT_FLAT` alert has already fired.

### 6.4 Totals

```
gross_pnl     = Σ all matched gross
fees          = Σ all Fill.fee
net_pnl       = gross_pnl − fees
slippage_cost = Σ all per-fill slippage_cost      # informational bucket
net_return    = net_pnl / config.capital_base
```

### 6.5 Benchmark return and alpha

Benchmark = configured instrument `config.benchmark.instrument` (e.g. `SPY`), whose sampled
`BookState` events Block 9 journals (§3.2).

- `open_mid` = `mid` of the **first** benchmark `BookState` with `ts ≥` the ts of the
  `SessionPhase` event entering `TRADING`.
- `close_mid` = `mid` of the **last** benchmark `BookState` with `ts ≤` the ts of the
  `SessionPhase` event entering `FORCE_FLAT` (or `CLOSED` if `FORCE_FLAT` was never seen).
- `benchmark_return = close_mid / open_mid − 1`.
- If either bound is missing (feed outage), `benchmark_return` and `alpha` are `null` in the
  report and a `WARNING` alert `BENCHMARK_MISSING` is raised.

`alpha = net_return − benchmark_return`. (v1 is deliberately simple: arithmetic excess return of
capital vs. buy-and-hold of the benchmark over the trading window.)

### 6.6 Daily report JSON schema — filled example

Written to `data/reports/<date>/report.json` (and `.txt`, §6.8), fsync'd, then the
`REPORT_WRITTEN` INFO alert fires. Example for the worked scenario used throughout this spec
(capital 100 000; two round trips; one rejection; SPY 500.00 → 501.00):

```json
{
  "schema_version": 1,
  "date": "2026-07-02",
  "generated_ts": 1751486460000000000,
  "config_hash": "sha256:9f2c…",
  "code_revision": "git:4b8a1e7",
  "capital_base": 100000.0,
  "pnl": {
    "gross_pnl": 25.0,
    "fees": 0.3,
    "net_pnl": 24.7,
    "slippage_cost": 3.0,
    "net_return": 0.000247
  },
  "benchmark": {
    "instrument": "SPY",
    "open_mid": 500.0,
    "close_mid": 501.0,
    "return": 0.002
  },
  "alpha": -0.001753,
  "per_signal": [
    {"signal": "ofi_momo", "gross_pnl": 20.0, "fees": 0.2, "net_pnl": 19.8,
     "slippage_cost": 2.0, "round_trips": 1, "hits": 1, "hit_rate": 1.0,
     "unrealized_residual": 0.0},
    {"signal": "coint_rev", "gross_pnl": 5.0, "fees": 0.1, "net_pnl": 4.9,
     "slippage_cost": 1.0, "round_trips": 1, "hits": 1, "hit_rate": 1.0,
     "unrealized_residual": 0.0}
  ],
  "per_instrument": [
    {"instrument": "XYZ", "gross_pnl": 20.0, "fees": 0.2, "net_pnl": 19.8, "slippage_cost": 2.0},
    {"instrument": "ABC", "gross_pnl": 5.0, "fees": 0.1, "net_pnl": 4.9, "slippage_cost": 1.0}
  ],
  "counts": {
    "intents": 4, "rejections": 1, "orders": 4, "fills": 4,
    "kill_switches": 0, "flatten_commands": 1
  },
  "rejections_by_rule": {"gross_position_cap": 1},
  "risk_events": [],
  "divergence": {
    "slippage_mean_bps": 2.66, "slippage_warn_at": 2.066, "slippage_n": 4, "slippage_active": false,
    "hit_rate": 1.0, "hitrate_warn_at": 0.4403, "hitrate_n": 2, "hitrate_active": false
  },
  "eod": {"flat_confirmed": true, "residual_positions": []},
  "reconciliation": {"block5_realized_pnl": 24.7, "attributed_net_pnl": 24.7, "diff": 0.0},
  "journal_integrity": {"events": 1187, "parts": 1, "dropped_events": 0, "malformed_events": 0},
  "alerts_summary": {"CRITICAL": 0, "WARNING": 0, "INFO": 1}
}
```

Notes: `divergence.*_active` = whether the alert condition held at close; `*_n` = samples seen
(windows not yet full ⇒ tests not evaluated, `_active: false`). `config_hash` = SHA-256 of the
resolved config file bytes; `code_revision` = `git rev-parse --short HEAD` captured at startup
(cross-cutting rule 4).

### 6.7 Reconciliation

`reconciliation.block5_realized_pnl` = `realized_pnl` sum of the **last** `PositionState` per
instrument. If `|diff| > max(0.01, 1e-6 × capital_base)` raise `WARNING` alert
`ATTRIBUTION_MISMATCH` — it means the fill ledger and Block 5 disagree and one of them is buggy.

### 6.8 Human-readable text rendering

`data/reports/<date>/report.txt`, fixed-width, rendered from the same data (format exactly):

```
DAILY PNL ATTRIBUTION — 2026-07-02
Capital base: 100,000.00      Config: sha256:9f2c…  Code: git:4b8a1e7

NET PNL:        +24.70   (gross +25.00, fees 0.30)      net return  +2.47 bps
BENCHMARK SPY:  500.00 -> 501.00                        bench return +20.00 bps
ALPHA:                                                  -17.53 bps

PER SIGNAL              gross      fees       net   slippage  trips  hit%
  ofi_momo             +20.00      0.20    +19.80       2.00      1   100
  coint_rev             +5.00      0.10     +4.90       1.00      1   100

PER INSTRUMENT          gross      fees       net   slippage
  XYZ                  +20.00      0.20    +19.80       2.00
  ABC                   +5.00      0.10     +4.90       1.00

COUNTS: intents 4  rejections 1  orders 4  fills 4  kills 0
EOD FLAT: YES          JOURNAL: 1187 events, 0 dropped, 0 malformed
ALERTS: 0 CRITICAL, 0 WARNING
```

---

## 7. Data structures, storage, retention

### 7.1 Dataclasses

```python
from __future__ import annotations
import enum
from dataclasses import dataclass, field

class AlertSeverity(enum.StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

@dataclass(frozen=True, slots=True)
class JournalEnvelope:
    ts: int                      # ns since epoch UTC, assigned by journal writer
    seq: int
    source_block: int            # 1..9
    event_type: str
    schema_version: int          # == 1
    payload: dict[str, object]

@dataclass(slots=True)
class Alert:
    alert_id: str                # uuid4 hex
    rule_id: str
    severity: AlertSeverity
    ts: int                      # ns, latest raise
    first_ts: int                # ns, first raise of this key
    count: int                   # merged occurrences (dedup)
    instrument: str | None
    message: str
    context: dict[str, float | int | str | bool]

@dataclass(slots=True)
class Lot:
    qty_open: float              # signed remaining quantity
    price: float
    intent_id: str
    signal: str
    ts: int

@dataclass(slots=True)
class SignalPnl:
    signal: str
    gross_pnl: float = 0.0
    fees: float = 0.0
    slippage_cost: float = 0.0
    round_trips: int = 0
    hits: int = 0
    unrealized_residual: float = 0.0
    @property
    def net_pnl(self) -> float: return self.gross_pnl - self.fees

@dataclass(slots=True)
class InstrumentPnl:
    instrument: str
    gross_pnl: float = 0.0
    fees: float = 0.0
    slippage_cost: float = 0.0

@dataclass(slots=True)
class DailyReport:               # mirrors §6.6 JSON exactly; to_json_dict() emits it
    date: str
    generated_ts: int
    config_hash: str
    code_revision: str
    capital_base: float
    gross_pnl: float
    fees: float
    net_pnl: float
    slippage_cost: float
    net_return: float
    benchmark_instrument: str
    benchmark_open_mid: float | None
    benchmark_close_mid: float | None
    benchmark_return: float | None
    alpha: float | None
    per_signal: list[SignalPnl]
    per_instrument: list[InstrumentPnl]
    counts: dict[str, int]
    rejections_by_rule: dict[str, int]
    risk_events: list[dict[str, object]]
    divergence: dict[str, float | int | bool]
    eod_flat_confirmed: bool
    eod_residual_positions: list[dict[str, object]]
    reconciliation: dict[str, float]
    journal_integrity: dict[str, int]
    alerts_summary: dict[str, int]
```

### 7.2 Directory layout (complete)

```
data/
  journal/<date>/journal-<date>.partNNN.ndjson[.zst]
  journal/<date>/malformed.ndjson
  reports/<date>/report.json
  reports/<date>/report.txt
  alerts/<date>/alerts.ndjson          # one Alert JSON per delivery (raise/escalate/clear)
  logs/bot-<date>.log
```

### 7.3 Retention policy

A startup task deletes files older than: journals **400** calendar days; alerts **400** days;
logs **90** days; reports **never deleted** (`retention.*_days` config, `0` = keep forever).
Deletion is by directory date, logged at `INFO`.

---

## 8. Public API and asyncio model

Module: `bot/monitoring/` — `block.py`, `journal.py`, `dashboard.py`, `alerts.py`,
`attribution.py`, `models.py` (dataclasses above), `logsetup.py`.

```python
import asyncio
from bot.monitoring.models import JournalEnvelope, Alert, DailyReport

class BusTap:
    """Publisher-side adapter the event bus calls for every message. Never blocks."""
    def __init__(self, queue: asyncio.Queue[JournalEnvelope], drop_counter: DropCounter) -> None: ...
    def publish(self, source_block: int, event_type: str, payload: dict[str, object]) -> None:
        """Wrap into a (ts=0, seq=0) proto-envelope and queue.put_nowait().
        On asyncio.QueueFull: increment drop_counter[event_type] and return. NEVER raises."""

class EventJournal:
    def __init__(self, root: Path, date: str, cfg: JournalConfig) -> None: ...
    def recover(self) -> int:
        """Startup corruption scan (§10). Returns next seq to use."""
    def append(self, env: JournalEnvelope) -> None: ...   # write + fsync policy §2.5
    def close(self) -> None: ...

class DashboardServer:
    def __init__(self, state: DashboardState, host: str, port: int) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

class AlertEngine:
    def __init__(self, cfg: AlertsConfig, journal: EventJournal, webhook_queue_size: int = 1000) -> None: ...
    def on_event(self, env: JournalEnvelope) -> None: ...   # rule evaluation, §5.2
    def on_tick(self, now_ns: int) -> None: ...             # 1 s ticker rules (feed timers, EOD grace)
    def raise_alert(self, rule_id: str, severity: AlertSeverity, message: str,
                    instrument: str | None = None, **context: float | int | str | bool) -> None: ...
    async def webhook_worker(self) -> None: ...             # retries per §5.4

class PnlAttributor:
    def __init__(self, cfg: AttributionConfig) -> None: ...
    def on_event(self, env: JournalEnvelope) -> None: ...   # dispatches Fill/TradeIntent/ExecutionQuality/BookState/SessionPhase/EodReport
    def finalize(self, journal_integrity: dict[str, int], alerts_summary: dict[str, int]) -> DailyReport: ...

class MonitoringBlock:
    """Facade; owns all tasks. Constructed once per session."""
    def __init__(self, config: MonitoringConfig, session_date: str) -> None: ...
    @property
    def tap(self) -> BusTap: ...                            # given to the bus at wiring time
    async def start(self) -> None:
        """Order: logging setup -> journal.recover() -> spawn consumer task, ticker task,
        webhook task, dashboard server, retention/compression task."""
    async def run_consumer(self) -> None:
        """while True: env = await queue.get(); stamp ts=time.time_ns(), seq=next;
        journal.append(env); dashboard_state.update(env); alert_engine.on_event(env);
        attributor.on_event(env). Exceptions in any consumer step are caught per-step,
        logged ERROR, and never abort the loop (§10)."""
    async def stop(self) -> None:
        """Drain queue (bounded by 5 s), finalize report if session CLOSED and not yet
        written, journal JournalCheckpoint, fsync, close, stop server."""
```

### 8.1 Lifecycle

1. Blocks are wired: the bus is given `monitoring.tap`; every `bus.publish()` from any block also
   calls `tap.publish()`.
2. `await monitoring.start()` before any trading block starts (so startup events are journaled).
3. On `SessionPhase → CLOSED` + `EodReport` (or grace timeout): finalize and write the daily
   report, then compress prior-date journals.
4. `await monitoring.stop()` last during shutdown.

### 8.2 Queue

Single ingest queue `asyncio.Queue[JournalEnvelope](maxsize=config.queue_size)` (default
**65 536**). One consumer task fans out to journal → dashboard → alerts → attribution, in that
order (journal first so the record survives a downstream bug).

### 8.3 Lag policy: DROP, never backpressure

When the queue is full, `BusTap.publish` **drops the event** (per-event-type counter) instead of
blocking or raising. Justification: Block 9 is a read-only consumer (§1.1); cross-cutting rule 3
says failures must degrade toward *flat and halted*, and a monitoring stall that blocks Block 5/6
message flow would do the opposite — stall risk checks and order handling. A journal gap is an
incident (it breaks the bit-exact replay guarantee) but a bounded, visible one: drops trigger the
`JOURNAL_DEGRADED` CRITICAL alert, a `MonitorLag` journal event, `monitor.dropped_events` on the
dashboard, and `journal_integrity.dropped_events` in the daily report. The queue is sized so that
drops only occur under pathological conditions (dead disk, consumer bug).

---

## 9. Configuration

Loaded from the run's versioned config (YAML). Complete example with defaults:

```yaml
monitoring:
  data_root: ./data              # str, existing writable dir
  capital_base: 100000.0         # float > 0; denominator of net_return
  queue_size: 65536              # int, 1024..1048576
  benchmark:
    instrument: SPY              # str; must be in Block 1 universe
  journal:
    max_part_bytes: 536870912    # int, 1 MiB..8 GiB (default 512 MiB)
    fsync_interval_ms: 200       # int, 10..5000
    fsync_max_events: 500        # int, 1..100000
    compress_level: 10           # int, 1..19 (zstd)
  dashboard:
    host: 127.0.0.1              # str; MUST be loopback in v1
    port: 8790                   # int, 1024..65535
    staleness_s: 5.0             # float, 0.5..60
  alerts:
    webhook_url: null            # str|null, https URL
    webhook_timeout_s: 5.0       # float, 1..30
    feed_down_s: 5.0             # float, 1..60
    feed_stale_s: 10.0           # float, 1..120
    loss_warn_frac: 0.80         # float, 0.5..0.95
    loss_crit_frac: 0.95         # float, loss_warn_frac..1.0
    eod_grace_s: 120.0           # float, 10..600
    dedup_window_s: 300.0        # float, 10..3600
    renotify_s: 900.0            # float, 60..86400
    max_per_minute: 30           # int, 1..600
  divergence:
    baseline_file: ./params/promoted.json   # str; JSON with bt_slippage_mean_bps,
                                            # bt_slippage_std_bps, bt_hit_rate
    slippage_window: 50          # int, 10..1000
    hitrate_window: 100          # int, 20..2000
  retention:
    journal_days: 400            # int, 0(=forever)..3650
    alerts_days: 400
    logs_days: 90
    reports_days: 0
  log:
    level: INFO                  # DEBUG|INFO|WARNING|ERROR
    stderr: true                 # bool
```

Validation: on startup, reject out-of-range values with a fatal error **before** trading blocks
start. Missing `baseline_file` disables both divergence rules with a `WARNING` log (paper-trading
bootstrap case).

---

## 10. Error handling

| Failure | Detection | Behavior (imperative) |
|---|---|---|
| **Disk full / IO error on journal write** | `OSError` (`ENOSPC` etc.) from write/fsync | Do not crash and do not block trading. Set `journal.degraded = true`, raise `JOURNAL_DEGRADED` CRITICAL (log + webhook still work), retry opening a fresh part every 10 s; while degraded, events are counted as dropped (they join `dropped_events`). Dashboard shows `monitor.journal.degraded: true`. |
| **Webhook endpoint down** | Exception / non-2xx | Retry per §5.4 (1,2,4,8,16 s; 5 attempts), then drop that delivery with `ERROR` log. Journal + log channels are unaffected, so no alert is ever fully lost. |
| **Malformed event received** (unserializable payload, non-dict, NaN floats) | `TypeError`/`ValueError` during `json.dumps`; type checks in consumer | Write `{"ts":…,"seq":…,"source_block":…,"event_type":"__malformed__","schema_version":1,"payload":{"repr": repr(obj)[:2000]}}` to `malformed.ndjson` (not the main journal — seq is still consumed to keep the count honest: write a main-journal line with `event_type:"__malformed__"` and the repr payload). Increment `malformed_events`; `WARNING` log; `WARNING` alert `MALFORMED_EVENTS` when count crosses 10. Never raise out of the consumer loop. |
| **Journal corruption detected on startup** (`recover()`) | For each existing part of today's date: stream-parse lines; verify `seq` contiguity; verify trailing `JournalCheckpoint` hash if the part is closed | (a) Truncated final line in the newest part (crash mid-write): truncate the file to the last complete line, log `WARNING`, continue appending with next seq. (b) Undecodable line mid-file or checkpoint hash mismatch: rename the part to `*.corrupt`, raise `JOURNAL_DEGRADED` CRITICAL, start a new part; `recover()` returns last-good `seq + 1`. Never silently skip bad bytes. |
| **Consumer-step exception** (bug in dashboard/alerts/attribution) | `except Exception` around each fan-out step | Log `ERROR` with traceback (throttled to 1/s per step), increment a per-step error counter (exposed under `monitor`), continue. The journal step runs first, so the record is intact even if analytics are broken. |
| **Benchmark data missing** | No `BookState` in window at finalize | `benchmark_return`/`alpha` = null, `BENCHMARK_MISSING` WARNING; report still written. |
| **Baseline file missing/invalid** | At startup | Divergence rules disabled, `WARNING` log. |

---

## 11. Test plan

Unit tests in `tests/monitoring/`. Use a fake clock (injected `now_ns: Callable[[], int]`) and
`tmp_path`. Every test below is required.

**T-01 Envelope round-trip.** Journal 3 events; read the file back; assert byte-exact key order
(`ts, seq, source_block, event_type, schema_version, payload`), `seq` = 1,2,3, valid NDJSON.

**T-02 Rotation & checkpoint.** Set `max_part_bytes` = 1 KiB; write until 3 parts exist; assert
part naming, `seq` continuity across parts, and that each closed part's final line is a
`JournalCheckpoint` whose `sha256_hex` matches the preceding bytes.

**T-03 fsync policy.** Monkeypatch `os.fsync` to count calls. Write 499 non-critical events
within 200 ms of fake time → 0 fsyncs; write the 500th → 1 fsync. Write one `Fill` → immediate
fsync. Advance fake clock 201 ms, write one `FeatureStats` → fsync on the interval rule.

**T-04 Recovery — truncated tail.** Write a valid part, append the bytes `{"ts":123,"se` (no
newline); run `recover()`; assert file truncated to last complete line and next `seq` is
last-good + 1.

**T-05 Recovery — corrupt middle.** Flip one byte mid-file, run `recover()`; assert file renamed
`*.corrupt`, new part started, `JOURNAL_DEGRADED` CRITICAL raised.

**T-06 Attribution — worked example (exact numbers).** Feed this event sequence (session date
2026-07-02, `capital_base` = 100 000, benchmark SPY):

1. `SessionPhase{TRADING}`; `BookState{SPY, mid: 500.00}`.
2. `TradeIntent I-001` XYZ BUY, `reason {"signal":"ofi_momo","ofi_z":3.1}`;
   `OrderRequest O-101`; `Fill{O-101, I-001, XYZ, qty:+100, price:50.00, fee:0.10}`;
   `ExecutionQuality{I-001, arrival_mid:49.99, avg_fill_price:50.00, slippage_bps:2.00}`.
3. `TradeIntent I-002` XYZ SELL, `reason {"signal":"ofi_momo","ofi_z":-0.2}`;
   `OrderRequest O-102`; `Fill{O-102, I-002, XYZ, qty:-100, price:50.20, fee:0.10}`;
   `ExecutionQuality{I-002, arrival_mid:50.21, avg_fill_price:50.20, slippage_bps:1.99}`.
4. `TradeIntent I-003` ABC SELL, `reason {"signal":"coint_rev","resid_z":-2.7}`;
   `OrderRequest O-103`; `Fill{O-103, I-003, ABC, qty:-50, price:30.10, fee:0.05}`;
   `ExecutionQuality{I-003, arrival_mid:30.11, avg_fill_price:30.10, slippage_bps:3.32}`.
5. `TradeIntent I-004` XYZ BUY; `IntentRejection{I-004, rule:"gross_position_cap"}`.
6. `FlattenCommand{PASSIVE}`; `OrderRequest O-104` (intent `FLATTEN-1`);
   `Fill{O-104, FLATTEN-1, ABC, qty:+50, price:30.00, fee:0.05}`;
   `ExecutionQuality{FLATTEN-1, arrival_mid:29.99, avg_fill_price:30.00, slippage_bps:3.34}`.
7. Final `PositionState`s: XYZ realized 19.80, ABC realized 4.90, qty 0 each.
8. `BookState{SPY, mid: 501.00}`; `SessionPhase{FORCE_FLAT}`; `SessionPhase{CLOSED}`;
   `EodReport{flat_confirmed: true, residual_positions: []}`.

Assert the finalized `DailyReport` **exactly** (tolerance 1e-9): `gross_pnl` 25.00, `fees` 0.30,
`net_pnl` 24.70, `slippage_cost` 3.00 (1.00 + 1.00 + 0.50 + 0.50), `net_return` 0.000247;
per-signal `ofi_momo` {gross 20.00, fees 0.20, net 19.80, slippage 2.00, round_trips 1, hits 1};
`coint_rev` {gross 5.00, fees 0.10, net 4.90, slippage 1.00, round_trips 1, hits 1} — note the
flatten fill's fee (0.05) and slippage (0.50) land on `coint_rev`, not `__flatten__`;
per-instrument XYZ {20.00, 0.20, 19.80, 2.00}, ABC {5.00, 0.10, 4.90, 1.00};
`benchmark.return` 0.002, `alpha` −0.001753; counts {intents 4, rejections 1, orders 4, fills 4};
`rejections_by_rule` {"gross_position_cap": 1}; `reconciliation.diff` 0.0; `eod_flat_confirmed`
true. Also assert `report.txt` matches the §6.8 rendering.

**T-07 Attribution — position flip.** Fills: +100 @ 10.00 (I-A, signal "s1"), −150 @ 10.10
(I-B, signal "s2"), +50 @ 10.05 (I-C, signal "s1"), all fee 0. Assert: s1 gross = (10.10 −
10.00) × 100 = **+10.00** (long lot I-A closed by I-B); s2 gross = (10.10 − 10.05) × 50 =
**+2.50** (the 50-short residual lot opened by I-B, closed by I-C); flat at end.

**T-08 Benchmark missing.** Same as T-06 but omit all SPY `BookState` events: report has
`benchmark.return: null`, `alpha: null`; `BENCHMARK_MISSING` WARNING raised; report still written.

**T-09 Alert — FEED_DOWN.** `FeedStatus{XYZ, DOWN}` at t=0; tick to t=4.9 s → no alert; tick to
t=5.1 s → one CRITICAL `FEED_DOWN` (key `("FEED_DOWN","XYZ")`). `FeedStatus{XYZ, LIVE}` at
t=8 s → INFO clear delivered, key removed from `alerts.active`.

**T-10 Alert — loss thresholds & dedup.** `daily_loss_limit` = −1000. Feed `PositionState`s
summing to −810 → one `LOSS_WARN` WARNING. Feed −820, −830 within dedup window → no new
deliveries, `count` = 3. Feed −960 → `LOSS_CRIT` CRITICAL delivered despite dedup (severity
escalation). Feed −700 → `LOSS_WARN` clears (hysteresis at 0.75).

**T-11 Alert — divergence.** Baselines μ = 1.5, σ = 2.0, N = 50, p₀ = 0.54, M = 100.
(a) Feed 50 `ExecutionQuality` with `slippage_bps` = 2.0 → mean 2.0 < 2.066, no alert; feed one
more at 5.4 → window mean (49 × 2.0 + 5.4)/50 = 2.068 > 2.066 → WARNING
`DIVERGENCE_SLIPPAGE`. (b) Close 100 intents with 44 hits → p̂ = 0.44 < 0.4403 → WARNING
`DIVERGENCE_HITRATE`; with 39 hits → CRITICAL. With 45 hits → no alert.

**T-12 Alert — EOD.** (a) `EodReport{flat_confirmed:false}` → CRITICAL `EOD_NOT_FLAT`.
(b) `SessionPhase{CLOSED}` then no `EodReport`; tick past 120 s → CRITICAL `EOD_NOT_FLAT`.

**T-13 Webhook retry.** Fake webhook returns 503 twice then 200: assert 3 POSTs with backoff
1 s, 2 s (fake clock) and the alert marked delivered. Fake always-503: assert exactly 5 attempts,
then `ERROR` log, delivery dropped; alert still present in journal and `alerts.ndjson`.

**T-14 Lag policy.** `queue_size` = 4, consumer paused. `tap.publish()` 6 events: no exception,
no blocking (call returns in < 1 ms), `dropped_total` = 2, drops attributed to correct event
types. Resume consumer: the 4 queued events are journaled; a `MonitorLag` event with
`dropped_total: 2` follows; `JOURNAL_DEGRADED` CRITICAL raised; `/status` shows
`monitor.dropped_events: 2`; the finalized report shows `journal_integrity.dropped_events: 2`.

**T-15 Dashboard.** Drive T-06's stream through `MonitoringBlock` with the HTTP server on an
ephemeral port; `GET /status` mid-stream: assert schema keys of §4.2 all present,
`session.phase`, `pnl.total`, `orders.counts_today` correct. Advance fake clock 6 s with no
events → all sections report `stale: true`. `GET /healthz` → `{"ok": true}`.

**T-16 Read-only guarantee (static).** Assert `bot/monitoring/` has no import of blocks 1–8's
modules except `models`/bus types, and grep-level check that it never calls `bus.publish` (only
`tap` receives). Enforced with a simple AST/import test.

---

## 12. Acceptance criteria checklist

- [ ] Block 9 exposes exactly one ingress (`BusTap.publish`) and publishes nothing to the bus;
      T-16 passes.
- [ ] Journal envelope, key order, `seq` semantics, naming, rotation, zstd compression, and fsync
      policy match §2 exactly (T-01…T-03).
- [ ] `recover()` handles truncated-tail and mid-file corruption per §10 (T-04, T-05); every
      closed part ends with a verifiable `JournalCheckpoint`.
- [ ] All 15 mandatory event types of §3.2 (plus benchmark `BookState`, `__malformed__`, and
      Block 9 meta events) are journaled; unknown types are journaled, not dropped.
- [ ] Structured JSON logging installed process-wide with the §3.1 schema.
- [ ] `GET /status` on `127.0.0.1:8790` returns the §4.2 schema with per-section `as_of_ts` /
      `stale`; `GET /healthz` works (T-15).
- [ ] All alert rules of §5.2 implemented with the exact thresholds/clearing, the §5.3
      divergence statistics, webhook retry/backoff, dedup, renotify, and rate cap
      (T-09…T-13).
- [ ] Daily attribution implements FIFO lot matching, opening-signal attribution of gross +
      closing-leg fees/slippage, benchmark open/close-mid return, and `alpha`; T-06 and T-07
      reproduce the worked numbers exactly; `report.json` validates against §6.6 and
      `report.txt` matches §6.8.
- [ ] Reconciliation vs. Block 5 realized PnL with `ATTRIBUTION_MISMATCH` alert on divergence.
- [ ] Lag policy is drop-with-accounting: T-14 passes; a full queue can never block or raise
      into a trading block.
- [ ] Every failure in the §10 table is handled without crashing the process or blocking
      trading; the consumer loop survives per-step exceptions.
- [ ] Config loads/validates the §9 schema with defaults and range checks; startup fails fast on
      invalid config, before trading blocks start.
- [ ] Retention task enforces §7.3.
- [ ] All tests T-01…T-16 implemented and green.
