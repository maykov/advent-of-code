# Block 4 — Signal Engine: Implementation Specification

Status: v1. Conforms to `docs/design.md` (§3 Block 4, §4 cross-cutting rules, §5 technology assumptions).

---

## 1. Overview and responsibilities

The Signal Engine is a **pure decision function**: it maps each incoming `FeatureVector` (Block 3), under the current `SessionPhase` (Block 7) and a versioned calibrated-parameter set (Block 8), to **at most one `TradeIntent` per instrument per tick**, plus periodic `SignalHealth` telemetry (Block 9).

v1 is an ensemble of four explicit, individually explainable rules ("signals"). Every emitted intent carries the exact feature values that caused it.

### 1.1 Responsibilities

- Consume the `FeatureVector` stream; evaluate all enabled signals per tick.
- Combine multi-signal results per instrument per tick into zero or one `TradeIntent` (§4).
- Enforce per-signal and per-instrument cooldowns (§4.3–§4.4).
- Enforce session gating: entries only during `TRADING`, with configurable buffers after open and before wind-down (§5).
- Load and validate the calibrated-parameter file at startup; stamp its version on every output (§6.3).
- Emit `SignalHealth` counters on a fixed interval (§6.2).
- Log every intent, suppression, and conflict with full explainability (§10).

### 1.2 Explicitly NOT responsibilities

- **No sizing.** `TradeIntent` has no quantity. Block 5 sizes.
- **No risk checks.** No position, loss, or rate limits. Block 5 enforces all limits.
- **No orders, no prices.** Never talks to a broker; never chooses a limit price (`entry_type` is advisory; Block 5/6 price and route).
- **No exits.** Block 4 emits *entry* intents only. `horizon_s` is advisory; exit management belongs to Blocks 5/7.
- **No online learning / recalibration.** Parameters change only via a new Block 8 file and a restart (§6.3.4).
- **No feature computation.** Consumes Block 3 features by name; computes nothing but comparisons and the conviction map.

### 1.3 Determinism contract (design §4.1)

The core evaluation (`SignalEngine.evaluate`) MUST be a pure function of `(FeatureVector, latest SessionPhase, parameter file, static config, internal cooldown state)`. It MUST NOT read the wall clock; all time arithmetic (cooldowns, gating buffers) uses `FeatureVector.ts_ns` and `SessionPhase.ts_ns`. The wall clock may be used only to schedule `SignalHealth` emission (non-decisional).

---

## 2. Input contracts

### 2.1 Timestamps

All `ts_ns` fields are `int`, UTC epoch **nanoseconds** (`ts` in the design doc). Local receive time, assigned by Block 1.

### 2.2 `FeatureVector` (from Block 3)

```python
@dataclass(frozen=True, slots=True)
class FeatureVector:
    instrument: str                 # e.g. "AAPL"
    ts_ns: int
    features: dict[str, float]      # name -> value; may omit warming-up features
    quality_flags: frozenset[str]   # see below
```

`quality_flags` entries are either:
- a **feature name** (lower_snake_case): that feature's value is present but degraded — treat as missing; or
- a **global flag** (UPPER_SNAKE_CASE, so it can never collide with a feature name): `BOOK_STALE`, `FEED_GAP`, `WARMUP` — the whole vector is untrusted.

### 2.3 Block 3 feature-naming convention and the v1 consumed set

Convention (contract with `docs/specs/03-feature-pattern-engine.md`, which MUST export these exact names): `lower_snake_case`, pattern `<family>_<qualifier>_<window>`, with `_z` suffix meaning "z-score against that feature's rolling mean/std as computed by Block 3". Block 4 consumes exactly these:

| Feature name | Type | Meaning |
|---|---|---|
| `ofi_l5_2s_z` | z-score | 5-level order-flow imbalance summed over a 2 s window, z-scored. Positive = net buying pressure. |
| `qimb_l3_z` | z-score | Depth-weighted queue imbalance over top 3 levels, z-scored. Positive = bid-heavy book. |
| `ret_10s_z` | z-score | 10 s mid-price log return, z-scored. |
| `vr_60s` | raw ratio | Variance ratio over 60 s. ≈1 random walk; >1 trending; <1 mean-reverting. |
| `pair_resid_z` | z-score | Cointegration residual of this instrument vs. its Block-3-configured partner. Positive = rich vs. pair. |
| `lr_resid_energy_z` | z-score | Residual energy of the book matrix after rank-k reconstruction (rolling SVD). High = structural anomaly. |
| `spread_bps` | raw | Current bid-ask spread in basis points of mid. |
| `vpin_50` | raw ∈ [0,1] | VPIN-style flow-toxicity estimate over 50 volume buckets. |

Any other features in the vector are ignored. Pair membership, windows, ranks, etc. are Block 3 configuration; Block 4 knows only the names above.

### 2.4 `SessionPhase` (from Block 7)

```python
class Phase(str, Enum):
    PRE_OPEN = "PRE_OPEN"; OPEN_AUCTION = "OPEN_AUCTION"; TRADING = "TRADING"
    WIND_DOWN = "WIND_DOWN"; FORCE_FLAT = "FORCE_FLAT"; CLOSED = "CLOSED"

@dataclass(frozen=True, slots=True)
class SessionPhase:
    phase: Phase
    ts_ns: int
    seconds_to_close: float
```

---

## 3. The v1 signal set

### 3.1 Shared definitions

**Availability.** A feature `f` is *available* on a tick iff `f in fv.features`, `f not in fv.quality_flags`, and `math.isfinite(fv.features[f])`. A signal whose required features are not all available is **skipped for that tick** (not the whole tick — see §8).

**Common filter (all signals).** `spread_bps <= filters.max_spread_bps` (static config, default 4.0). A signal whose firing condition holds but whose filter fails is counted `suppressed_filter`, does **not** fire, and does **not** enter cooldown.

**Conviction formula (all signals).** Each signal defines a *driver* z-score `z_drv ≥ 0` and calibrated `theta` (firing threshold) and `z_sat` (saturation, must be `> theta`). When the signal fires (`z_drv ≥ theta` guaranteed):

```
conviction = c_min + (1.0 - c_min) * min(1.0, (z_drv - theta) / (z_sat - theta))
```

with static `c_min ∈ (0, 1)`, default `0.2`. So conviction = `c_min` exactly at threshold, `1.0` at/beyond saturation — always in `(0, 1]`.

**Cooldown (all signals).** Calibrated `cooldown_s` and `rearm_ratio ∈ (0, 1]` (default 0.5). After firing at tick time `t_fire`, the signal may fire again only when **both**: (a) `fv.ts_ns ≥ t_fire + cooldown_s·1e9`, and (b) at least one tick since `t_fire` had the driver available with `z_drv < rearm_ratio * theta` (hysteresis re-arm). Full state machine in §4.3.

**MARKET/LIMIT rule.** Where a signal's entry type is "spread-dependent": `MARKET` if `spread_bps <= filters.market_spread_bps` (static, default 1.5), else `LIMIT`. Signals marked "always LIMIT"/"always MARKET" ignore this.

### 3.2 S1 — `ofi_momentum` (OFI momentum)

Follow strong signed order flow when the short-horizon regime is trending.

| Item | Definition |
|---|---|
| Required features | `ofi_l5_2s_z`, `vr_60s`, `spread_bps` |
| Fire LONG | `ofi_l5_2s_z >= +theta AND vr_60s >= vr_min_trend AND spread_bps <= max_spread_bps` |
| Fire SHORT | `ofi_l5_2s_z <= -theta AND vr_60s >= vr_min_trend AND spread_bps <= max_spread_bps` |
| Direction | `BUY` on LONG condition, `SELL` on SHORT condition |
| Driver | `z_drv = abs(ofi_l5_2s_z)` |
| Conviction | shared formula with `theta`, `z_sat` (defaults 2.5 / 4.5) |
| Horizon | `horizon_s` (default 30.0) |
| Entry type | spread-dependent (MARKET when tight — flow signals decay fast) |
| Cooldown | `cooldown_s` default 15.0; re-arm `abs(ofi_l5_2s_z) < rearm_ratio*theta` |
| Extra params | `vr_min_trend` (default 1.10) |

### 3.3 S2 — `qimb_reversion` (book-imbalance mean reversion)

Fade a short-horizon price move when the resting book leans against it and the regime is mean-reverting. Skip when flow is toxic.

| Item | Definition |
|---|---|
| Required features | `qimb_l3_z`, `ret_10s_z`, `vr_60s`, `vpin_50`, `spread_bps` |
| Fire LONG | `qimb_l3_z >= +theta AND ret_10s_z <= -theta_ret AND vr_60s <= vr_max_mr AND vpin_50 <= vpin_max AND spread_bps <= max_spread_bps` |
| Fire SHORT | `qimb_l3_z <= -theta AND ret_10s_z >= +theta_ret AND vr_60s <= vr_max_mr AND vpin_50 <= vpin_max AND spread_bps <= max_spread_bps` |
| Direction | `BUY` on LONG (price dipped into a bid-heavy book), `SELL` on SHORT |
| Driver | `z_drv = abs(qimb_l3_z)` (the `ret_10s_z` condition is a gate only, not a conviction input) |
| Conviction | shared formula with `theta`, `z_sat` (defaults 2.5 / 5.0) |
| Horizon | `horizon_s` (default 60.0) |
| Entry type | **always LIMIT** (mean reversion earns the spread; never cross) |
| Cooldown | `cooldown_s` default 30.0; re-arm `abs(qimb_l3_z) < rearm_ratio*theta` |
| Extra params | `theta_ret` (default 1.0), `vr_max_mr` (default 0.90); `vpin_max` is a static filter (default 0.7) |

### 3.4 S3 — `pair_reversion` (pair-deviation reversion)

Trade the reversion of the cointegration residual computed by Block 3. **Single-leg in v1**: only this instrument is traded; no hedge leg is emitted (documented limitation; Block 5 sees it as an ordinary directional intent).

| Item | Definition |
|---|---|
| Required features | `pair_resid_z`, `spread_bps` |
| Fire SHORT | `pair_resid_z >= +theta AND spread_bps <= max_spread_bps`  (rich vs. pair → sell) |
| Fire LONG | `pair_resid_z <= -theta AND spread_bps <= max_spread_bps`  (cheap vs. pair → buy) |
| Direction | opposite the residual sign, as above |
| Driver | `z_drv = abs(pair_resid_z)` |
| Conviction | shared formula with `theta`, `z_sat` (defaults 2.0 / 4.0) |
| Horizon | `horizon_s` (default 300.0) |
| Entry type | **always LIMIT** |
| Cooldown | `cooldown_s` default 120.0; re-arm `abs(pair_resid_z) < rearm_ratio*theta` |

Instruments with no configured pair simply never receive `pair_resid_z` from Block 3; the signal is skipped per §8 (and auto-disabled for that instrument after warm-up, §8 row 4).

### 3.5 S4 — `lowrank_anomaly` (low-rank residual anomaly)

A spike in low-rank reconstruction residual marks a structural break in the book; trade it in the direction of concurrent order flow.

| Item | Definition |
|---|---|
| Required features | `lr_resid_energy_z`, `ofi_l5_2s_z`, `spread_bps` |
| Fire | `lr_resid_energy_z >= theta AND abs(ofi_l5_2s_z) >= theta_dir AND spread_bps <= max_spread_bps` |
| Direction | `BUY` if `ofi_l5_2s_z > 0`, `SELL` if `ofi_l5_2s_z < 0` (the `theta_dir > 0` gate makes 0 impossible) |
| Driver | `z_drv = lr_resid_energy_z` (one-sided; residual energy has no negative anomaly) |
| Conviction | shared formula with `theta`, `z_sat` (defaults 3.0 / 5.0) |
| Horizon | `horizon_s` (default 15.0) |
| Entry type | **always MARKET** (fastest decay in the set) |
| Cooldown | `cooldown_s` default 45.0; re-arm `lr_resid_energy_z < rearm_ratio*theta` |
| Extra params | `theta_dir` (default 1.0) |

---

## 4. Ensemble combination and cooldown state machines

### 4.1 Per-tick combination

For each `FeatureVector` (one instrument), after per-signal evaluation you have a set of *fires*, each `(signal_name, side, conviction, horizon_s, entry_type, reason_fragment)`. Combine deterministically:

1. Partition fires by `side` into `B` (BUY) and `S` (SELL).
2. Combined conviction per side (noisy-OR; exact, order-independent, result in (0,1]):
   `C(side) = 1 - Π_{i in side} (1 - conviction_i)`; `C(empty side) = 0.0`.
3. **Same direction only** (one side empty): emit that side with `conviction = C(side)`.
4. **Conflict** (both sides non-empty):
   - If `abs(C(BUY) - C(SELL)) >= ensemble.conflict_margin` (static, default 0.30): the larger side **wins**; emit it with `conviction = max(ensemble.conviction_floor, C(win) - C(lose))` (floor static, default 0.05; result stays in (0,1]).
   - Otherwise: **suppress** — emit nothing this tick; log `evt=conflict_suppressed` (§10.2); count `suppressed_conflict`.
5. `horizon_s` and `entry_type` of the emitted intent come from the **primary signal**: the fire on the winning side with the highest individual conviction; ties broken by fixed priority `ofi_momentum > qimb_reversion > pair_reversion > lowrank_anomaly`.
6. `reason` = merged reason fragments of **winning-side** fires only, plus ensemble keys (§10.1). Losing-side fires appear only in the conflict log line.

### 4.2 When cooldowns start

A signal enters cooldown at the moment its firing condition + filters pass while it is `ARMED` — **regardless of whether the ensemble later suppresses or loses the conflict**. (Rationale: the signal's information was consumed; re-firing every tick during a standoff is noise.) Fires blocked by *cooldown* or by *session gating* never happened: gated ticks are not evaluated at all (§5.3) and cooldown-blocked fires don't restart the timer.

### 4.3 Per-(instrument, signal) cooldown state machine

State stored per `(instrument, signal_name)`; initial state `ARMED`.

| State | Meaning | Transition |
|---|---|---|
| `ARMED` | May fire. | Firing condition passes → record fire, go `COOLING(until = ts_ns + cooldown_s*1e9, rearmed = False)`. |
| `COOLING` | Timer running. On every evaluated tick where the driver is available: if `z_drv < rearm_ratio*theta`, set `rearmed = True`. | When `fv.ts_ns >= until`: if `rearmed` → `ARMED`; else → `REARM_WAIT`. (Check before evaluating the firing condition on that tick.) |
| `REARM_WAIT` | Timer done, hysteresis not yet satisfied. | Tick with driver available and `z_drv < rearm_ratio*theta` → `ARMED` (that same tick may NOT fire — re-arm and fire are mutually exclusive by construction since `rearm_ratio*theta < theta`). |

No persistence across restarts: all states reset to `ARMED` on startup (acceptable: worst case is one early re-entry, which Block 5 position caps bound).

### 4.4 Per-instrument cooldown

After **emitting** a `TradeIntent` for instrument `I` at `t_emit`, emit no further intent for `I` until `fv.ts_ns >= t_emit + ensemble.instrument_cooldown_s * 1e9` (static, default 5.0 s). Signals are still evaluated during this window (their own cooldowns start per §4.2); the block applies at step 7 of the flow (§7.4). Suppressions here count `suppressed_instrument_cooldown`. Conflict suppression (§4.1.4) does **not** start the instrument cooldown (nothing was emitted).

---

## 5. Session gating

### 5.1 Phase rule

Maintain `current_phase` (from the latest `SessionPhase`; **before any `SessionPhase` arrives, behave as `CLOSED`**), `seconds_to_close` from that message, and `trading_start_ns` = `ts_ns` of the first `SessionPhase` with `phase == TRADING` this session (reset when phase leaves `TRADING`-family, i.e. on `CLOSED`).

Entries are allowed on a tick iff **all** of:

1. `current_phase == Phase.TRADING`. All other phases (`PRE_OPEN`, `OPEN_AUCTION`, `WIND_DOWN`, `FORCE_FLAT`, `CLOSED`) suppress unconditionally.
2. `fv.ts_ns >= trading_start_ns + gating.open_buffer_s * 1e9` (post-open no-trade buffer; static, default 300 s).
3. `latest seconds_to_close - (fv.ts_ns - latest SessionPhase.ts_ns)/1e9 > gating.close_buffer_s` (pre-wind-down buffer; static, default 600 s). Note the extrapolation: `seconds_to_close` is projected forward from the phase message's timestamp to the tick's timestamp so gating does not depend on `SessionPhase` publish frequency.

### 5.2 Rationale for buffers

`open_buffer_s` lets Block 3's rolling z-score windows stabilize after the auction; `close_buffer_s` guarantees Block 4 stops proposing entries strictly before Block 7's `WIND_DOWN` (Block 7's stop-entries deadline is a *backstop*, not the primary control).

### 5.3 Behavior while gated

Gated ticks are **not evaluated**: no firing checks, no cooldown transitions, no re-arm observations. Count them as `suppressed_gated` in `SignalHealth`. `SessionPhase` messages are always processed. This keeps replay simple: gating is a pure prefix check.

---

## 6. Data structures and the parameter file

### 6.1 `TradeIntent`

```python
class Side(str, Enum):
    BUY = "BUY"; SELL = "SELL"

class EntryType(str, Enum):
    MARKET = "MARKET"; LIMIT = "LIMIT"

@dataclass(frozen=True, slots=True)
class TradeIntent:
    intent_id: str                  # see below
    instrument: str
    ts_ns: int                      # ts of the triggering FeatureVector
    side: Side
    conviction: float               # (0, 1], post-ensemble
    horizon_s: float                # advisory expected holding time
    entry_type: EntryType           # advisory; Block 5/6 decide price/tactic
    reason: dict[str, float]        # full explainability map, §10.1
    signals: tuple[str, ...]        # contributing (winning-side) signal names, sorted
    params_version: str             # from the loaded parameter file
```

`instrument, ts(=ts_ns), side, conviction, horizon_s, entry_type, reason` match the design doc; `intent_id`, `signals`, `params_version` are documented extensions (`intent_id` is already referenced by Blocks 5/6/9).

**`intent_id` generation rule.** `intent_id = f"si-{instrument}-{ts_ns}"`, e.g. `si-AAPL-1751468400123456789`. Deterministic (replay-stable, design §4.1) and unique because §7.4 emits at most one intent per instrument per tick and §8 row 6 drops non-monotonic ticks. No randomness, no wall clock.

### 6.2 `SignalHealth`

Emitted every `health_interval_s` (static, default 10.0 s, wall-clock scheduled) and once at shutdown; counters cover the window since the previous emission.

```python
@dataclass(frozen=True, slots=True)
class SignalHealth:
    ts_ns: int
    window_s: float
    signals_evaluated: int          # per-signal firing checks performed
    suppressed: int                 # sum of all suppressed_* below
    fired: int                      # per-signal fires (pre-ensemble)
    intents_emitted: int
    suppressed_breakdown: dict[str, int]
        # keys: "gated", "cooldown", "instrument_cooldown", "conflict",
        #       "filter", "missing_feature", "quality_flag", "queue_full",
        #       "clock_backwards", "global_quality"
    per_signal_fired: dict[str, int]   # signal_name -> fires in window
    params_version: str
```

`signals_evaluated / suppressed / fired` match the design doc; the rest are documented extensions.

### 6.3 Calibrated-parameter file (Block 8 → Block 4 contract)

#### 6.3.1 Format and schema

One JSON file (UTF-8, single object). Path given by static config `params_file`.

| Key | Type | Required | Rule |
|---|---|---|---|
| `schema_version` | int | yes | Must equal `1`. |
| `params_version` | str | yes | Unique version tag; stamped on every intent/health message. |
| `generated_at` | str | yes | ISO-8601 UTC. |
| `valid_from`, `valid_until` | str | yes | ISO dates (inclusive). Trading date must lie inside; else stale (§8 row 5). |
| `signals` | object | yes | Map signal name → param object. Exactly the names in §3 are known; unknown names → load error. Missing name = signal disabled. |
| `signals.<name>.enabled` | bool | yes | |
| `signals.<name>.theta` | float | yes | `> 0`. |
| `signals.<name>.z_sat` | float | yes | `> theta`. |
| `signals.<name>.horizon_s` | float | yes | `(0, 3600]`. |
| `signals.<name>.cooldown_s` | float | yes | `[0, 3600]`. |
| `signals.<name>.rearm_ratio` | float | yes | `(0, 1]`. |
| `signals.ofi_momentum.vr_min_trend` | float | yes for S1 | `[1.0, 3.0]`. |
| `signals.qimb_reversion.theta_ret` | float | yes for S2 | `> 0`. |
| `signals.qimb_reversion.vr_max_mr` | float | yes for S2 | `(0, 1.0]`. |
| `signals.lowrank_anomaly.theta_dir` | float | yes for S4 | `> 0`. |
| `per_instrument_overrides` | object | no | instrument → signal name → partial param object; same field rules; deep-merged over `signals` at load time. |

Unknown top-level or per-signal keys → load **error** (typo protection), not a warning.

#### 6.3.2 Example file

```json
{
  "schema_version": 1,
  "params_version": "wf2026-06-28.r12",
  "generated_at": "2026-06-28T21:14:03Z",
  "valid_from": "2026-06-29",
  "valid_until": "2026-07-05",
  "signals": {
    "ofi_momentum":    {"enabled": true, "theta": 2.5, "z_sat": 4.5, "horizon_s": 30.0,
                        "cooldown_s": 15.0, "rearm_ratio": 0.5, "vr_min_trend": 1.10},
    "qimb_reversion":  {"enabled": true, "theta": 2.5, "z_sat": 5.0, "horizon_s": 60.0,
                        "cooldown_s": 30.0, "rearm_ratio": 0.5, "theta_ret": 1.0, "vr_max_mr": 0.90},
    "pair_reversion":  {"enabled": true, "theta": 2.0, "z_sat": 4.0, "horizon_s": 300.0,
                        "cooldown_s": 120.0, "rearm_ratio": 0.5},
    "lowrank_anomaly": {"enabled": true, "theta": 3.0, "z_sat": 5.0, "horizon_s": 15.0,
                        "cooldown_s": 45.0, "rearm_ratio": 0.5, "theta_dir": 1.0}
  },
  "per_instrument_overrides": {
    "TSLA": {"ofi_momentum": {"theta": 3.0}}
  }
}
```

#### 6.3.3 Validation on load

`CalibratedParams.from_file(path)` MUST: parse JSON; check every rule in §6.3.1; apply overrides; compute the file's SHA-256; log `evt=params_loaded params_version=... sha256=...`. Any violation → raise `ParamsError` → **process refuses to start** (fail-safe: no trading on bad parameters). Staleness (`valid_until` in the past, or `valid_from` in the future, judged against the configured session's trading date) is a startup error too — never trade through it silently.

#### 6.3.4 Reload semantics: restart-only

**Hot reload is NOT supported in v1.** Parameters are immutable for the process lifetime. Changing parameters = deploy new file + restart before the session (determinism: one run ↔ one `params_version`, recorded on every message, per design §4.4). Implementations MUST NOT watch the file for changes.

---

## 7. Public API, lifecycle, asyncio model

Module: `bot/signal_engine.py` (+ `bot/messages.py` for shared dataclasses if it already exists).

### 7.1 Classes

```python
class ParamsError(Exception): ...

@dataclass(frozen=True)
class CalibratedParams:
    params_version: str
    sha256: str
    valid_from: datetime.date
    valid_until: datetime.date
    signals: Mapping[str, Mapping[str, float | bool]]          # post-merge defaults
    overrides: Mapping[str, Mapping[str, Mapping[str, float | bool]]]

    @classmethod
    def from_file(cls, path: pathlib.Path, trading_date: datetime.date) -> "CalibratedParams": ...
    def for_instrument(self, instrument: str, signal: str) -> Mapping[str, float | bool]: ...

@dataclass(frozen=True)
class SignalFire:                                              # internal
    signal: str
    side: Side
    conviction: float
    horizon_s: float
    entry_type: EntryType
    reason: dict[str, float]

class SignalRule(Protocol):                                    # one impl per §3 signal
    name: str
    required_features: frozenset[str]
    def evaluate(self, fv: FeatureVector,
                 params: Mapping[str, float | bool],
                 filters: FilterConfig) -> SignalFire | None: ...
    def driver(self, fv: FeatureVector) -> float | None: ...   # z_drv, or None if unavailable

class SignalEngine:
    def __init__(self, config: SignalEngineConfig, params: CalibratedParams,
                 bus: EventBus) -> None: ...
    async def run(self) -> None: ...                           # lifecycle task; returns on cancel
    def on_session_phase(self, sp: SessionPhase) -> None: ...
    def evaluate(self, fv: FeatureVector) -> TradeIntent | None: ...  # pure core, sync
    def health_snapshot(self, now_ns: int) -> SignalHealth: ...
```

`EventBus` is the shared in-process bus from design §2/§5: `bus.subscribe(topic: str, maxsize: int) -> asyncio.Queue`, `bus.publish_nowait(topic: str, msg: object) -> None` (raises `QueueFull`). Topics: subscribe `"feature_vector"`, `"session_phase"`; publish `"trade_intent"`, `"signal_health"`.

### 7.2 Lifecycle

1. Caller builds `CalibratedParams.from_file(...)` (may raise → abort startup) and `SignalEngine(...)`.
2. Caller schedules `asyncio.create_task(engine.run())`.
3. `run()` loops: `await` on the merged input queues (single consumer task — **all state is task-confined; no locks**); `SessionPhase` → `on_session_phase`; `FeatureVector` → `evaluate` → publish intent if not `None`; publish `SignalHealth` when `health_interval_s` elapsed.
4. Shutdown: task cancellation; emit a final `SignalHealth`; no other cleanup (no persistent state).

### 7.3 Concurrency and backpressure

Single asyncio task; `evaluate` is synchronous and O(#signals). Input queue `maxsize = config.input_queue_size`; if Block 3 outpaces Block 4 the bus's overflow policy applies upstream. Publishing an intent uses `publish_nowait`; on `QueueFull` **drop the intent**, log ERROR, count `suppressed_breakdown["queue_full"]` (fail-safe direction, design §4.3: a dropped *entry* is safe; never block the loop).

### 7.4 Per-FeatureVector evaluation flow (normative)

For `evaluate(fv)`:

1. **Monotonicity.** If `fv.ts_ns < last_ts_ns[fv.instrument]` → drop tick (§8 row 6), return `None`. Else record `last_ts_ns`.
2. **Global quality.** If `quality_flags ∩ {BOOK_STALE, FEED_GAP, WARMUP} ≠ ∅` → count `global_quality`, return `None` (no cooldown/re-arm updates).
3. **Session gate** (§5.1). If gated → count `gated`, return `None`.
4. **Cooldown bookkeeping.** For each enabled signal in `COOLING`/`REARM_WAIT` for this instrument: apply the §4.3 timer-expiry and re-arm-observation transitions using `fv.ts_ns` and `rule.driver(fv)`.
5. **Evaluate signals.** For each enabled signal (fixed order: S1, S2, S3, S4): check availability (§3.1; count `missing_feature`/`quality_flag` per skipped signal); increment `signals_evaluated`; run firing condition + filters; if fired and state is `ARMED` → append `SignalFire`, transition to `COOLING`, increment `fired`; if fired but not `ARMED` → count `cooldown`; if condition true but filter failed → count `filter`.
6. **Ensemble** (§4.1). May yield nothing (count `conflict` if step 4.1.4 suppressed).
7. **Instrument cooldown** (§4.4). If active → count `instrument_cooldown`, return `None`.
8. **Build `TradeIntent`**: `intent_id` per §6.1, `reason` per §10.1, `params_version` from loaded params. Record `t_emit = fv.ts_ns` for §4.4.
9. Return the intent (caller publishes it and writes the §10.2 log line).
10. All counters land in the next `SignalHealth`.

---

## 8. Error handling (normative table)

| # | Condition | Detection | Behavior | Telemetry |
|---|---|---|---|---|
| 1 | Required feature missing from `fv.features` | availability check §3.1 | **Skip that signal only**; other signals still evaluate this tick | `suppressed_breakdown["missing_feature"]`; DEBUG log, rate-limited to 1/feature/60 s |
| 2 | Required feature present but in `quality_flags`, or NaN/±inf | availability check | Same as row 1 (skip signal, not tick) | `suppressed_breakdown["quality_flag"]`; DEBUG rate-limited |
| 3 | Global flag `BOOK_STALE` / `FEED_GAP` / `WARMUP` | step 2 of flow | **Skip the entire tick** (vector untrusted); no state updates | `suppressed_breakdown["global_quality"]` |
| 4 | Signal's required feature never observed for an instrument after `warmup_ticks` (static, default 1000) evaluated ticks | per-(instrument, signal) counter | Disable that signal for that instrument for the rest of the run (probable Block 3/4 name mismatch — the "unknown feature name" case) | one ERROR log `evt=feature_never_seen signal=... feature=... instrument=...`; Block 9 alert |
| 5 | Stale/future-dated parameter file, schema violation, unknown key, range violation | `from_file` at startup | Raise `ParamsError`; **process refuses to start**. No stale-tolerance flag exists in v1. | ERROR log with every violation listed |
| 6 | Clock going backwards: `fv.ts_ns < last_ts_ns[instrument]` | step 1 of flow | Drop the tick silently (state untouched). If > `clock.max_backwards_per_min` (static, default 100) drops occur in any 60 s window (measured on `ts_ns` of dropped ticks): enter **HALT mode** — evaluate nothing, emit nothing except `SignalHealth`, until restart (fail-safe, design §4.3) | `suppressed_breakdown["clock_backwards"]`; WARNING per drop (rate-limited 1/10 s); CRITICAL on HALT |
| 7 | `SessionPhase` never received | `current_phase` unset | Treat as `CLOSED` → everything gated | counted in `gated` |
| 8 | Intent output queue full | `publish_nowait` raises | Drop intent (entry drops are safe); never block | `suppressed_breakdown["queue_full"]`; ERROR log |
| 9 | Unknown enum value in `SessionPhase.phase` | `on_session_phase` | Treat as `CLOSED`; WARNING | — |

---

## 9. Configuration (static, versioned per design §4.4)

Everything **calibrated by Block 8's walk-forward** lives in the parameter file (§6.3): `enabled`, `theta`, `z_sat`, `horizon_s`, `cooldown_s`, `rearm_ratio`, and per-signal extras (`vr_min_trend`, `theta_ret`, `vr_max_mr`, `theta_dir`). Everything that is **wiring, safety, or session policy** lives in static config below and is never touched by calibration. That is the line; no parameter appears in both.

```yaml
signal_engine:
  params_file: "config/params/signal-params.json"   # str, required
  health_interval_s: 10.0        # float, (1, 300], default 10.0
  input_queue_size: 4096         # int, [64, 65536], default 4096
  gating:
    open_buffer_s: 300.0         # float, [0, 1800], default 300  — no entries this long after TRADING starts
    close_buffer_s: 600.0        # float, [0, 3600], default 600  — no entries when projected seconds_to_close <= this
  ensemble:
    conflict_margin: 0.30        # float, [0, 1], default 0.30
    conviction_floor: 0.05       # float, (0, 0.5], default 0.05
    instrument_cooldown_s: 5.0   # float, [0, 300], default 5.0
  conviction:
    c_min: 0.20                  # float, (0, 1), default 0.20
  filters:
    max_spread_bps: 4.0          # float, (0, 100], default 4.0   — hard entry filter, all signals
    market_spread_bps: 1.5       # float, (0, max_spread_bps], default 1.5 — MARKET/LIMIT switch (§3.1)
    vpin_max: 0.70               # float, (0, 1], default 0.70    — S2 toxicity filter
  clock:
    max_backwards_per_min: 100   # int, [1, 100000], default 100  — HALT threshold (§8 row 6)
  warmup_ticks: 1000             # int, [1, 1e6], default 1000    — §8 row 4
```

Validate all ranges at startup; violations → refuse to start.

---

## 10. Explainability

### 10.1 `reason` map (exact content)

`dict[str, float]` on every intent. Keys, all mandatory where applicable:

- For **each winning-side fired signal**, every feature its firing condition or filter read, under its Block 3 name: e.g. `ofi_l5_2s_z`, `vr_60s`, `spread_bps` (deduplicated across signals; identical by construction within one tick).
- `signal.<name>` = that signal's individual conviction, one entry per winning-side fire (e.g. `signal.ofi_momentum: 0.6`).
- `theta.<name>` = the `theta` actually applied for that signal (post-override), one entry per winning-side fire.
- `ensemble.n_buy`, `ensemble.n_sell` = fire counts per side (floats).
- `ensemble.c_buy`, `ensemble.c_sell` = combined per-side convictions from §4.1.2.

Everything a human needs to answer "why did this trade happen?" from the intent alone.

### 10.2 Log line format

Structured single-line JSON on the standard logger, component `signal_engine`. Exact fields:

Intent (INFO):
```json
{"ts":"2026-07-02T14:31:07.123456789Z","lvl":"INFO","comp":"signal_engine","evt":"intent",
 "intent_id":"si-AAPL-1751466667123456789","instrument":"AAPL","side":"BUY",
 "conviction":0.6,"horizon_s":30.0,"entry_type":"MARKET","signals":["ofi_momentum"],
 "reason":{"ofi_l5_2s_z":3.0,"vr_60s":1.25,"spread_bps":1.0,"signal.ofi_momentum":0.6,
           "theta.ofi_momentum":2.0,"ensemble.n_buy":1.0,"ensemble.n_sell":0.0,
           "ensemble.c_buy":0.6,"ensemble.c_sell":0.0},
 "params_version":"wf2026-06-28.r12"}
```

Conflict suppression (INFO): `evt="conflict_suppressed"`, plus `instrument`, `ts_ns`, `c_buy`, `c_sell`, `signals_buy`, `signals_sell`, and the merged feature values of **all** fires.

Signal-skip / drops (DEBUG or WARNING per §8): `evt` ∈ `{"signal_skipped","tick_dropped","halt"}` with `detail`.

---

## 11. Test plan

Framework: `pytest`, pure-sync tests against `SignalEngine.evaluate` with injected params/config; one asyncio test for lifecycle. Unless stated otherwise every test uses:

- **Params P\***: the example file of §6.3.2 but with `ofi_momentum.theta = 2.0, z_sat = 4.0` and `pair_reversion.theta = 2.0, z_sat = 4.0` (others as printed); **Config C\***: the §9 defaults.
- Phase state: `SessionPhase(TRADING, ts_ns = T0, seconds_to_close = 20_000.0)` already applied, where `T0 = 1_751_466_000_000_000_000`; all test ticks at `T1 = T0 + 400e9` (400 s after TRADING start → open buffer of 300 s satisfied; projected `seconds_to_close = 19_600 > 600` → not close-gated).
- A "quiet" feature baseline: `{"ofi_l5_2s_z": 0.0, "qimb_l3_z": 0.0, "ret_10s_z": 0.0, "vr_60s": 1.0, "pair_resid_z": 0.0, "lr_resid_energy_z": 0.0, "spread_bps": 1.0, "vpin_50": 0.3}`, `quality_flags = ∅`, instrument `"AAPL"`. Tests override named entries.

| # | Test | Input (overrides on baseline) | Expected |
|---|---|---|---|
| T1 | S1 fires long, MARKET | `ofi_l5_2s_z=3.0, vr_60s=1.25` | Intent: `side=BUY`, `conviction=0.6` (= 0.2+0.8·(3−2)/(4−2)), `horizon_s=30.0`, `entry_type=MARKET` (spread 1.0 ≤ 1.5), `signals=("ofi_momentum",)`, `intent_id="si-AAPL-<T1>"`, reason exactly as §10.2 example with `theta.ofi_momentum=2.0`. |
| T2 | S1 conviction saturates | `ofi_l5_2s_z=5.0, vr_60s=1.25` | `conviction=1.0` exactly. |
| T3 | S1 regime gate blocks | `ofi_l5_2s_z=3.0, vr_60s=1.0` | `None`; `fired=0`; S1 stays `ARMED` (no cooldown). |
| T4 | S1 LIMIT on wide spread | `ofi_l5_2s_z=3.0, vr_60s=1.25, spread_bps=2.0` | Intent `BUY`, `entry_type=LIMIT` (2.0 > 1.5, ≤ 4.0). |
| T5 | Spread filter kills all | `ofi_l5_2s_z=3.0, vr_60s=1.25, spread_bps=4.5` | `None`; `suppressed_breakdown["filter"]=1`; no cooldown started. |
| T6 | S2 fires long | `qimb_l3_z=3.5, ret_10s_z=-1.5, vr_60s=0.8` | Intent `BUY`, `conviction=0.52` (= 0.2+0.8·(3.5−2.5)/(5.0−2.5)), `horizon_s=60.0`, `entry_type=LIMIT`. |
| T7 | S2 toxicity gate | T6 plus `vpin_50=0.8` | `None` (filter). |
| T8 | S3 fires short | `pair_resid_z=2.6` | Intent `SELL`, `conviction=0.44` (= 0.2+0.8·0.3), `horizon_s=300.0`, `LIMIT`. |
| T9 | S4 fires, direction from OFI | `lr_resid_energy_z=4.0, ofi_l5_2s_z=1.2` | Intent `BUY` (ofi z > 0; 1.2 ≥ θ_dir=1.0 but < S1's θ=2.0 so only S4 fires), `conviction=0.6` (= 0.2+0.8·(4−3)/(5−3)), `horizon_s=15.0`, `MARKET`. |
| T10 | Agreement combination | `ofi_l5_2s_z=3.0, vr_60s=1.25, lr_resid_energy_z=4.0` (S1 BUY 0.6 and S4 BUY 0.6 both fire) | One intent `BUY`, `conviction=0.84` (= 1−0.4·0.4), primary = S1 (tie at 0.6 → priority) → `horizon_s=30.0`, `entry_type=MARKET`; `signals=("lowrank_anomaly","ofi_momentum")` (sorted); reason has both `signal.*` and `theta.*` entries, `ensemble.n_buy=2.0`, `ensemble.c_buy=0.84`. |
| T11 | Conflict → suppress | `ofi_l5_2s_z=3.0, vr_60s=1.25, pair_resid_z=2.6` (S1 BUY 0.6 vs S3 SELL 0.44) | `None` (|0.6−0.44| = 0.16 < 0.30); `suppressed_breakdown["conflict"]=1`; **both** S1 and S3 in `COOLING`; conflict log line with `c_buy=0.6, c_sell=0.44`. |
| T12 | Conflict → stronger side wins | `ofi_l5_2s_z=5.0, vr_60s=1.25, pair_resid_z=2.6` (S1 BUY 1.0 vs S3 SELL 0.44) | Intent `BUY`, `conviction=0.56` (= max(0.05, 1.0−0.44)), primary S1 → `horizon_s=30.0`, `MARKET`; `signals=("ofi_momentum",)`; reason `ensemble.c_sell=0.44`. |
| T13 | Signal cooldown | Tick A = T1 input → intent. Tick B at `+6 s` (`ts = T1 + 6_000_000_000`), same input | Tick B → `None`: instrument cooldown passed (6 s > 5 s) but S1 is `COOLING` (6 s < 15 s); `suppressed_breakdown["cooldown"]=1`. |
| T14 | Re-arm required | Ticks: A = T1 input (fires); B at `+20 s` with same input (timer expired, never re-armed) | B → `None`, S1 in `REARM_WAIT`. Then C at `+25 s` with `ofi_l5_2s_z=0.5` (< 0.5·2.0) → re-arms, no fire; D at `+30 s` with T1 input → intent again, `intent_id="si-AAPL-<ts_D>"`. |
| T15 | Instrument cooldown | Tick A = T1 input (S1 fires, intent). Tick B at `+3 s` with `lr_resid_energy_z=4.0, ofi_l5_2s_z=1.2` (S4 fires) | B → `None`; `suppressed_breakdown["instrument_cooldown"]=1`; S4 **is** in `COOLING` afterwards (§4.2). |
| T16 | Gating: phases | T1 input, but phase set to each of PRE_OPEN, OPEN_AUCTION, WIND_DOWN, FORCE_FLAT, CLOSED, and no-phase-ever-received | All → `None`, `suppressed_breakdown["gated"]` increments; no state changes. |
| T17 | Gating: open buffer | Phase `TRADING` at `T0`; T1 input tick at `T0+299e9` ns | `None` (gated). Same tick at `T0+301e9` → intent. |
| T18 | Gating: close buffer with projection | Phase msg `(TRADING, ts=T0, seconds_to_close=1000)`; T1 input tick at `T0+401e9` | Projected stc = 1000 − 401 = 599 ≤ 600 → `None` (gated). At `T0+399e9`: 601 > 600 → intent. |
| T19 | Missing feature skips signal only | Baseline minus `pair_resid_z` key, plus `ofi_l5_2s_z=3.0, vr_60s=1.25` | Intent from S1 as in T1; `suppressed_breakdown["missing_feature"]=1` (S3 skipped). |
| T20 | Quality-flagged feature | T1 input with `quality_flags={"ofi_l5_2s_z"}` | `None` for S1 and S4 paths; S2/S3 still evaluated (both quiet → no intent); count `quality_flag`. |
| T21 | Global flag skips tick | T1 input with `quality_flags={"BOOK_STALE"}` | `None`; `global_quality=1`; no cooldown/re-arm updates. |
| T22 | Clock backwards | Tick A at T1, tick B at `T1−1` | B dropped, `clock_backwards=1`, state unchanged. 101 such drops within 60 s → HALT: subsequent good ticks return `None`; CRITICAL logged once. |
| T23 | Params validation | Files with: `z_sat == theta`; unknown key `"thetaa"`; `valid_until="2026-06-01"` (past); missing `theta_dir` | Each → `ParamsError` at load; engine never constructed. |
| T24 | Override applied | P* + override `{"AAPL": {"ofi_momentum": {"theta": 3.5}}}`; T1 input (`ofi z=3.0`) | `None` for AAPL (3.0 < 3.5); same tick for `"MSFT"` → intent, and `reason["theta.ofi_momentum"]=2.0`. |
| T25 | Health accounting | Run T11's tick then one quiet tick | `SignalHealth`: `signals_evaluated=8`, `fired=2`, `suppressed ≥ 1` with `conflict=1`, `intents_emitted=0`, `params_version="wf2026-06-28.r12"`. |
| T26 | Determinism / replay | Feed the identical sequence (phases + 500 mixed ticks, fixed seed generator) twice through fresh engines | Byte-identical intent sequences including `intent_id`s. |
| T27 | Queue full | Bus stub whose `publish_nowait` raises `QueueFull` on `trade_intent`; T1 input | No exception escapes `run()`; `queue_full=1`; ERROR logged. |

(T13 note: all "+N s" offsets are `N * 1_000_000_000` ns added to T1.)

---

## 12. Acceptance criteria checklist

- [ ] All tests T1–T27 pass with the numbers exactly as stated (convictions to 1e-9).
- [ ] At most one `TradeIntent` per instrument per tick; `conviction ∈ (0, 1]` on every emitted intent, enforced by assertion.
- [ ] No intent is ever emitted outside `TRADING` phase or inside the open/close buffers (property test over randomized phase sequences).
- [ ] `evaluate` never reads the wall clock or any global mutable state; replaying a journaled day reproduces identical intents and `intent_id`s (T26).
- [ ] Startup fails loudly on any invalid, stale, or future-dated parameter file; `params_version` and file SHA-256 appear in the startup log and on every intent and `SignalHealth`.
- [ ] No hot-reload path exists (grep: no file watching in `bot/signal_engine.py`).
- [ ] Block 4 emits only `TradeIntent` and `SignalHealth`; it imports nothing from Blocks 5/6 and contains no quantity, price, or position logic (code review item).
- [ ] Every emitted intent's `reason` contains all §10.1 mandatory keys; every intent/conflict produces exactly one §10.2 log line, valid JSON.
- [ ] Missing/degraded single features skip only the affected signal; global flags skip the tick; both paths covered by tests and counted in `SignalHealth`.
- [ ] HALT mode (clock backwards threshold) stops all emission until restart and raises a CRITICAL alert.
- [ ] `evaluate` mean latency < 100 µs per tick on the dev machine at 8 signals-evaluations/tick (fits the 10–500 ms end-to-end budget with wide margin); measured by a benchmark test, non-gating but reported.
- [ ] Static config validated against §9 ranges at startup; calibrated parameters appear **only** in the parameter file, static parameters **only** in config (no duplicates).
