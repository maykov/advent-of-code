# Block 3 — Feature / Pattern Engine: Implementation Specification

Status: v1.0 (implementation-ready)
Conforms to: `docs/design.md` §3 (Block 3), §4 (cross-cutting rules), §5 (technology assumptions).

---

## 1. Overview and responsibilities

The Feature / Pattern Engine transforms the raw `BookState` and `TradePrint` streams from
Block 2 into a dense, named **`FeatureVector`** per instrument on a fixed sampling grid.
Every feature is an explicit algebraic or statistical construction, incrementally
computable, and fully deterministic given the input event stream.

### 1.1 Responsibilities

1. Subscribe to `BookState`, `TradePrint`, and `BookIntegrity` events on the internal bus.
2. Maintain per-instrument incremental calculators for the five v1 feature families:
   order-flow imbalance algebra, rolling low-rank (PCA) structure, cross-sectional
   pair residuals, temporal/regime statistics, and trade-tape statistics.
3. Normalize designated features via rolling z-scores.
4. Emit exactly one `FeatureVector{instrument, ts, features, quality_flags}` per
   instrument per sampling tick (Δ_fast = 100 ms of event time), with a **stable key set**
   (every configured feature key present in every vector; unavailable values are `NaN`).
5. Emit `FeatureStats{name, rolling_mean, rolling_std, ...}` periodically to Block 9 for
   normalization drift monitoring.
6. Propagate data-quality problems as `quality_flags`, never as exceptions or silence.

### 1.2 Explicit non-responsibilities

The block does **not**:

- Make signal decisions, apply thresholds, or produce `TradeIntent` (Block 4).
- Size, gate, or veto anything (Block 5). It never sees positions or PnL.
- Talk to any external API, send orders, or touch the broker (Blocks 1/6).
- Maintain the order book (Block 2). It treats `BookState` as ground truth and never
  reconstructs depth from deltas.
- Calibrate parameters. All windows/weights come from versioned config (Block 8 promotes
  them); the engine only evaluates.
- Use the wall clock. All time logic is **event time** (`BookState.ts` /
  `TradePrint.ts_local`), so a replay through Block 8 is bit-identical (design rule §4.1).

### 1.3 Inputs / outputs (contract recap)

| Direction | Message | Notes |
|---|---|---|
| In | `BookState{instrument, ts, bids[N], asks[N], mid, microprice, spread, seq}` | event-driven **and** sampled (≥ 1 per 100 ms per live instrument, guaranteed by Block 2) |
| In | `TradePrint{instrument, ts_exchange, ts_local, price, size, aggressor_side, seq}` | `aggressor_side ∈ {BUY, SELL}` |
| In | `BookIntegrity{instrument, ok, crossed_book, staleness_ms}` | drives `STALE_INPUT` / gap handling |
| Out | `FeatureVector` (see §7.1) | to Block 4 and the journal |
| Out | `FeatureStats` (see §7.2) | to Block 9, every `stats_emit_every_s` |

All timestamps in this block are `int` nanoseconds since the Unix epoch, UTC.
`bids`/`asks` are price-descending / price-ascending arrays of `(price: float, size: float)`
with `N ≥ K` levels (config `levels_k`, default K = 10). Sizes are in instrument units
(shares/contracts); prices in quote currency.

---

## 2. Time, sampling grids, and windows

Three deterministic grids, all derived from event time:

| Grid | Period | Used by | Trigger rule |
|---|---|---|---|
| **Fast grid** | Δ_fast = 100 ms | FeatureVector emission; PCA sampling; OFI/tape binning | see below |
| **Bar grid** | Δ_bar = 1 s | log-mid returns → VR, ACF, RV; cross-sectional OLS | fast-grid ticks where `ts_grid % Δ_bar == 0` |
| **Cadences** | multiples of grid ticks | PCA recompute, OLS refit, stats emission | tick counters |

**Emission rule (deterministic, no timers).** Per instrument keep `next_emit_ts`
(initialized to `floor(first_event_ts / Δ_fast) · Δ_fast + Δ_fast`). After processing any
`BookState` with `ts ≥ next_emit_ts`:

1. Set `ts_grid = floor(ts / Δ_fast) · Δ_fast` (the boundary just passed).
2. Snapshot all calculators, emit one `FeatureVector` with `ts = ts_grid`.
3. Set `next_emit_ts = ts_grid + Δ_fast`.

Because Block 2 emits a sampled `BookState` at least every 100 ms while the feed is live,
this yields ≈ 10 vectors/s per instrument. If the feed stalls, no vectors are emitted
(downstream sees the gap via absence + Block 2's `BookIntegrity`); on resumption the first
vectors carry `FEED_GAP` (§8.3).

**Windows.** Every window in this spec is either (a) a count of grid samples (suffix
`samples`) or (b) a duration realized as `duration / bin` fast-grid bins (suffix `s`).
Both are exact and deterministic. Symbols:

| Symbol | Meaning |
|---|---|
| `t` | index of the current fast-grid tick (per instrument) |
| `K` | number of book levels used per side (config `levels_k`) |
| `p^b_i, q^b_i` | price and size at bid level `i ∈ 1..K` (level 1 = best) |
| `p^a_i, q^a_i` | price and size at ask level `i` |
| `m_t` | mid at tick `t` (from `BookState.mid`) |
| `r_t` | 1-second log return, `r_t = ln(m_t) − ln(m_{t−1})` on the bar grid |

---

## 3. Feature catalog and naming convention

### 3.1 Naming convention

Feature keys are stable, lowercase, dot-separated ASCII strings:

```
{family}.{metric}[.l{level}][.k{K}][.q{lag}][.w{window}][.{partner}][.z]
```

- `family ∈ {ofi, book, pca, xs, tmp, tape}`.
- `w{window}`: `w1s`, `w10s`, `w600` (bare number = bar-grid samples).
- `.z` suffix = rolling z-score of the same-named raw feature (§6). Keys are **fixed at
  startup from config** and never change during a run (stable key set).
- `{partner}` (cross-sectional only): the partner instrument symbol with every character
  outside `[a-z0-9]` replaced by `_` and lowercased (`BRK.B` → `brk_b`).

### 3.2 v1 feature catalog

All features below are emitted per instrument in every `FeatureVector`. "z" column: a
`.z`-suffixed rolling z-score is additionally emitted. Bounded ratio features are not
z-scored.

| Key | Family | Definition § | Units | Range | z |
|---|---|---|---|---|---|
| `ofi.l1.w1s` | OFI | 5.1 | size units | ℝ | yes |
| `ofi.l1.w10s` | OFI | 5.1 | size units | ℝ | yes |
| `ofi.dw.w1s` | OFI | 5.1 | size units | ℝ | yes |
| `ofi.dw.w10s` | OFI | 5.1 | size units | ℝ | yes |
| `book.qi.l1` | OFI | 5.2 | — | [−1, 1] | no |
| `book.dwi.k{K}` | OFI | 5.2 | — | [−1, 1] | no |
| `pca.f1`, `pca.f2`, `pca.f3` | PCA | 5.3 | σ-units | ℝ | yes |
| `pca.resid_energy` | PCA | 5.3 | — | [0, 1] | no |
| `pca.eff_rank` | PCA | 5.3 | — | [1, 2K] | no |
| `xs.{partner}.beta` | XS | 5.4 | — | ℝ | no |
| `xs.{partner}.resid_z` | XS | 5.4 | — | ℝ (clamped) | no (is a z) |
| `tmp.vr.q8.w600` | Temporal | 5.5 | — | [0, ∞) | yes |
| `tmp.acf.l1.w120` | Temporal | 5.5 | — | [−1, 1] | no |
| `tmp.rv.w120` | Temporal | 5.5 | return / √bar | [0, ∞) | yes |
| `tape.sv.w10s` | Tape | 5.6 | size units | ℝ | yes |
| `tape.ai.w10s` | Tape | 5.6 | — | [−1, 1] | no |
| `tape.ai.w60s` | Tape | 5.6 | — | [−1, 1] | no |
| `tape.vpin` | Tape | 5.6 | — | [0, 1] | no |

With defaults (K = 10, 3 PCA factors, 1 partner) this is 18 raw + 9 z = **27 keys** per
instrument.

### 3.3 Signal-contract export aliases (Block 4 contract)

Block 4's spec (§2.3 of `04-signal-engine.md`) consumes a fixed set of eight
flat-named (underscore) feature keys. The engine MUST additionally emit these exact
keys in every `FeatureVector`. They are the only exception to the dot grammar of §3.1
and are part of the stable key set fixed at startup. Each is defined below in terms of
this spec's calculators; where a definition needs an extra window/parameterization, the
calculator runs a second accumulator for it (same incremental algorithm, different
parameters — the added cost per event is O(1) each).

| Contract key | Definition | Source calculator |
|---|---|---|
| `ofi_l5_2s_z` | Rolling z (§6) of the depth-weighted OFI window sum (§5.1) computed with `K = 5`, decay `λ` from `contract.ofi_level_decay` (default 0.7), window `W = 2 s` (20 fast-grid bins). A second `(K=5, w2s)` accumulation alongside the defaults. | OfiCalculator |
| `qimb_l3_z` | Rolling z of `book.dwi.k3` — the depth-weighted imbalance of §5.2 computed additionally at `K = 3`. Exception to the "bounded features are not z-scored" rule: Block 4 consumes the anomaly magnitude, not the raw ratio. | BookImbalanceCalculator |
| `ret_10s_z` | Rolling z of `ln(mid_t / mid_{t−100})` on the 100 ms fast grid (100 bars = 10 s). NaN + `WARMUP` until 100 valid grid mids are buffered; a grid tick with an invalid mid (one-sided/crossed book) resets the buffer per §8. | TemporalCalculator |
| `vr_60s` | Alias of `tmp.vr.q8.w600` (600 fast-grid bars ≡ 60 s). Emitted under both keys with identical values. | TemporalCalculator |
| `pair_resid_z` | Alias of `xs.{partner}.resid_z` for the instrument's single configured partner. Instruments with no configured partner MUST NOT emit this key (Block 4 §8 then skips the pair signal — this is the one contract key whose absence is legal). | CrossSectionalEngine |
| `lr_resid_energy_z` | Rolling z of `pca.resid_energy` (§5.3). Same bounded-feature exception as `qimb_l3_z`. | RollingPcaCalculator |
| `spread_bps` | `10^4 · (a¹ − b¹) / mid` from the most recent valid `BookState`. NaN + `ONE_SIDED_BOOK` when either side is empty; never z-scored. | BookImbalanceCalculator |
| `vpin_50` | VPIN of §5.6 computed with `vpin_n_buckets = 50` (a second ring buffer alongside the configured default). NaN + `WARMUP` until 50 completed buckets. | TapeCalculator |

Config: the block `contract_exports` (see §10) has `enabled: true` (default; MUST be
true in any deployment that runs Block 4) and `ofi_level_decay: 0.7`. With exports
enabled, the per-instrument key count of §3.2 grows by 8 contract keys plus the 5
internal raw accumulators backing them (`ofi.dw5.w2s`, `book.dwi.k3`, `tmp.ret.w100`,
`pca.resid_energy` reused, `tape.vpin50`); memory bounds of §12 already include this.
The z-score normalizer treats every `_z`-suffixed contract key exactly like a `.z` key
(§6: window 3600, min 300 samples, clamp ±8).

---

## 4. Architecture and public API

### 4.1 Composition

```
FeatureEngine (one asyncio task)
 ├─ InstrumentPipeline["AAPL"]
 │   ├─ OfiCalculator            (BookState)
 │   ├─ BookImbalanceCalculator  (BookState)
 │   ├─ RollingPcaCalculator     (BookState, fast grid)
 │   ├─ TemporalCalculator       (bar grid: VR + ACF + RV)
 │   ├─ TapeCalculator           (TradePrint: SV + AI + VPIN)
 │   └─ Normalizer               (rolling z per configured key)
 ├─ InstrumentPipeline["MSFT"] …
 └─ CrossSectionalEngine         (one per configured pair; bar grid)
```

Per-instrument calculators are composed inside an `InstrumentPipeline`; cross-sectional
calculators live outside the pipelines (they consume two instruments' bar-grid mids) and
**inject** their outputs into both legs' snapshots.

### 4.2 Calculator protocol

```python
from typing import Protocol

class FeatureCalculator(Protocol):
    """Incremental calculator. All methods are synchronous and non-blocking."""

    names: tuple[str, ...]  # feature keys this calculator produces (fixed at init)

    def on_book(self, book: BookState) -> None: ...
    def on_trade(self, trade: TradePrint) -> None: ...
    def on_bar(self, ts_grid: int, mid: float) -> None: ...   # bar-grid tick
    def snapshot(self, ts_grid: int,
                 out: dict[str, float],
                 flags: dict[str, QualityFlag]) -> None: ...  # write values + per-key flags
    def reset(self, reason: QualityFlag) -> None: ...         # feed gap / integrity failure
```

Calculators that do not consume a given event type implement it as a no-op.
`snapshot` must write **every** key in `names` (value or `NaN`) and never raise.

### 4.3 Public classes

```python
class FeatureEngine:
    def __init__(self, config: FeatureEngineConfig, bus: EventBus) -> None: ...

    async def run(self) -> None:
        """Single consumer loop. Reads the bus subscription queue and dispatches.
        Cancelling the task is the shutdown path; no other lifecycle methods."""

    # --- internal, synchronous, called only from run() ---
    def _on_book_state(self, ev: BookState) -> None: ...
    def _on_trade(self, ev: TradePrint) -> None: ...
    def _on_integrity(self, ev: BookIntegrity) -> None: ...
    def _emit(self, instrument: str, ts_grid: int) -> None: ...


class InstrumentPipeline:
    def __init__(self, instrument: str, config: FeatureEngineConfig) -> None: ...
    def on_book(self, ev: BookState) -> list[FeatureVector]: ...   # 0 or 1 vectors
    def on_trade(self, ev: TradePrint) -> None: ...
    def reset(self, reason: QualityFlag) -> None: ...


class CrossSectionalEngine:
    def __init__(self, pairs: list[PairConfig], config: FeatureEngineConfig) -> None: ...
    def on_bar(self, instrument: str, ts_grid: int, mid: float) -> None: ...
    def inject(self, instrument: str, ts_grid: int,
               out: dict[str, float], flags: dict[str, QualityFlag]) -> None: ...


class Normalizer:
    def __init__(self, keys: tuple[str, ...], cfg: ZScoreConfig) -> None: ...
    def update_and_score(self, out: dict[str, float],
                         flags: dict[str, QualityFlag]) -> None: ...
    def stats(self) -> list[FeatureStats]: ...
```

### 4.4 Concurrency model

- **One asyncio task**, no threads, no locks. All calculator work happens synchronously
  inside `FeatureEngine.run()` in bus-delivery order (which is journal order — required
  for determinism, design §4.1).
- Input queue is the bus's bounded queue (size from bus config). The engine must keep
  per-event work within the budget in §12 so the queue never backs up; it never awaits
  inside event handling.
- Output: `bus.publish(FeatureVector)` / `bus.publish(FeatureStats)` — non-blocking put
  on the bus's bounded output queue; if full, the bus's overflow policy applies (engine
  does not implement its own).
- NumPy is used only inside `RollingPcaCalculator.recompute()`; everything else is plain
  float arithmetic (avoids per-event array allocation).

### 4.5 Update flow

Per incoming `BookState` for instrument `I`:

1. If integrity for `I` is bad (`ok == False` or `crossed_book`), set the pipeline's
   sticky `STALE_INPUT` flag and **skip calculator updates** (do not corrupt state with a
   crossed book); still run the emission check in step 4 so the cadence is preserved.
2. Call `on_book` on all book-consuming calculators (OFI, imbalance, PCA sample buffer).
3. If `ev.ts` crosses a bar-grid boundary not yet processed, call `on_bar(ts_bar, mid)`
   on the temporal calculator and forward `(I, ts_bar, mid)` to `CrossSectionalEngine`.
   If multiple bar boundaries were skipped (event gap > 1 s), process only the latest and
   mark `FEED_GAP` on the temporal/xs windows (missing bars are **not** interpolated;
   the return spanning the gap is discarded, see §8.3).
4. If `ev.ts ≥ next_emit_ts`: build the snapshot dict (all calculators → `Normalizer` →
   `CrossSectionalEngine.inject`), OR-reduce per-key flags into vector-level
   `quality_flags`, publish `FeatureVector`, advance `next_emit_ts`.

Per incoming `TradePrint`: route to `TapeCalculator.on_trade` only (trades never trigger
emission; the 100 ms `BookState` sampling guarantees emission cadence).

Per `BookIntegrity`: `ok == True` clears sticky `STALE_INPUT`; a transition through
`staleness_ms > gap_reset_ms` (default 2000) calls `pipeline.reset(FEED_GAP)`.

---

## 5. Mathematical definitions

Every subsection gives: definition, incremental update, per-event complexity, and
degenerate-case behavior. General rule: any division uses guard `denom < eps_denom`
(config, default `1e-12`) → output `NaN` + `NUMERIC_FALLBACK` flag, unless a subsection
states a specific fallback.

### 5.1 Multi-level order-flow imbalance (OFI) — Cont–Kukanov–Stoikov

For each pair of consecutive `BookState`s for the same instrument (previous state `′`,
current unprimed), define per level `i ∈ 1..K` the bid- and ask-side order-flow
increments:

$$
e^{b}_{i} \;=\; q^{b}_{i}\,\mathbf{1}\!\left[p^{b}_{i} \ge p'^{b}_{i}\right]
        \;-\; q'^{b}_{i}\,\mathbf{1}\!\left[p^{b}_{i} \le p'^{b}_{i}\right]
$$

$$
e^{a}_{i} \;=\; -\,q^{a}_{i}\,\mathbf{1}\!\left[p^{a}_{i} \le p'^{a}_{i}\right]
        \;+\; q'^{a}_{i}\,\mathbf{1}\!\left[p^{a}_{i} \ge p'^{a}_{i}\right]
$$

$$
\mathrm{ofi}_i \;=\; e^{b}_{i} + e^{a}_{i}
\qquad\text{(units: size units; positive = net buy-side pressure)}
$$

This is the CKS level-1 OFI applied level-wise: comparisons use the price **at the same
level index** in the previous state. If either side has fewer than `i` levels in either
state, `ofi_i = 0` for that transition and `ONE_SIDED_BOOK` is flagged if level 1 itself
is missing (§8).

**Depth-weighted multi-level OFI** with exponential level decay `λ ≥ 0`
(config `ofi_level_decay`, default 0.7):

$$
w_i = e^{-\lambda (i-1)}, \qquad
\mathrm{ofi}^{dw} = \sum_{i=1}^{K} w_i \,\mathrm{ofi}_i
$$

(weights deliberately **not** normalized; `ofi^dw` stays in size units and its scale is
handled by the z-score).

**Windowed features.** OFI increments are accumulated into the current fast-grid bin
(100 ms). Let `B_j` be the bin sums; then for window `W` seconds
(`n = W / 0.1` bins):

$$
\mathrm{ofi.l1.w}W\mathrm{s}(t) = \sum_{j=t-n+1}^{t} B^{(1)}_j,
\qquad
\mathrm{ofi.dw.w}W\mathrm{s}(t) = \sum_{j=t-n+1}^{t} B^{(dw)}_j
$$

maintained as a ring buffer of bin sums plus a running total (add new bin, subtract
evicted bin). Windows: 1 s (n = 10) and 10 s (n = 100).

*Complexity:* O(K) per `BookState` (level loop), O(1) per bin roll.
*Warm-up:* valid after the window is full **or** `min_warmup_fraction` (default 0.5) of
bins have been observed since start/reset; otherwise `NaN` + `WARMUP`.

### 5.2 Queue imbalance and depth-weighted imbalance

Instantaneous (no window), from the current `BookState`:

$$
\mathrm{qi.l1} \;=\; \frac{q^{b}_{1} - q^{a}_{1}}{q^{b}_{1} + q^{a}_{1}} \in [-1, 1]
$$

$$
\mathrm{dwi.k}K \;=\;
\frac{\sum_{i=1}^{K} w_i \left(q^{b}_{i} - q^{a}_{i}\right)}
     {\sum_{i=1}^{K} w_i \left(q^{b}_{i} + q^{a}_{i}\right)} \in [-1, 1],
\qquad w_i = e^{-\lambda (i-1)}
$$

with the same `λ` as §5.1. Missing levels contribute `q = 0`. If **both** sides are
empty at all K levels, output `NaN` + `ONE_SIDED_BOOK`.

*Complexity:* O(K) per snapshot (computed at emission time only, not per event).

### 5.3 Rolling low-rank structure (windowed PCA of the book)

**Book vector.** At each fast-grid tick sample

$$
x_t = \big(q^{b}_{1}, \dots, q^{b}_{K},\; q^{a}_{1}, \dots, q^{a}_{K}\big) \in \mathbb{R}^{2K},
$$

transformed elementwise as \( \tilde{x}_{t,j} = \ln(1 + x_{t,j}) \) (log-depth: tames
heavy-tailed sizes; exact zeros map to 0). Missing levels sample as 0 before the log.

**Book matrix.** `X ∈ ℝ^{T×2K}`: the last `T = pca_window_samples` (default 600 = 60 s)
log-depth vectors, rows = time (fast grid), columns = the 2K levels, stored as a ring
buffer of rows.

**Recompute** (every `pca_recompute_every` = 50 ticks = 5 s, and only when the buffer
holds ≥ `pca_min_samples` = 300 rows):

1. Column means \( \mu_j \) and standard deviations \( \sigma_j \) (ddof = 1) over the
   window. Guard: \( \sigma_j < \varepsilon \Rightarrow \sigma_j := 1 \) (column carries
   no information; standardized column becomes ~0).
2. Standardize: \( Z = (X - \mathbf{1}\mu^{\top}) \operatorname{diag}(\sigma)^{-1} \).
3. Covariance \( C = Z^{\top} Z / (T-1) \in \mathbb{R}^{2K \times 2K} \); symmetric
   eigendecomposition \( C = V \Lambda V^{\top} \), eigenvalues
   \( \lambda_1 \ge \dots \ge \lambda_{2K} \ge 0 \).
4. **Sign convention** (determinism): flip each eigenvector `v_j` so that its
   largest-|component| entry is positive; ties broken by lowest index.
5. Cache \( \mu, \sigma, \{v_j\}_{j \le k}, \{\lambda_j\} \) with `k = pca_factors`
   (default 3).

**Per-tick outputs** (using the cached decomposition; between recomputes the cache is up
to one cadence old — this is by design, not an error):

$$
\mathrm{pca.f}j(t) = \left\langle z_t,\, v_j \right\rangle,
\quad j = 1..k,
\qquad z_t = \operatorname{diag}(\sigma)^{-1}\left(\tilde{x}_t - \mu\right)
$$

$$
\mathrm{pca.resid\_energy}(t) =
1 - \frac{\sum_{j=1}^{k} \langle z_t, v_j\rangle^2}{\lVert z_t \rVert^2}
\;\in [0,1]
\qquad (\lVert z_t \rVert^2 < \varepsilon \Rightarrow 0,\ \texttt{NUMERIC\_FALLBACK})
$$

$$
\mathrm{pca.eff\_rank} =
\frac{\left(\sum_{j} \lambda_j\right)^{2}}{\sum_{j} \lambda_j^{2}}
\quad\text{(participation ratio, recomputed at cadence, constant between recomputes)}
$$

If eigendecomposition fails or \( \sum_j \lambda_j < \varepsilon \) (frozen book):
keep the previous cache, flag `NUMERIC_FALLBACK`; if there is no previous cache, all PCA
keys are `NaN` + `WARMUP`.

*Complexity:* O(2K) per tick (log-transform, store row, project onto k vectors —
O(k·2K)). Recompute: **O(T·(2K)² + (2K)³) at cadence** (defaults: ≈ 600·400 + 8000 ≈
2.5·10⁵ flops every 5 s — within budget, §12). This is the one deliberately super-linear
step permitted by the design ("PCA may be O(levels²) at its recompute cadence").
*Staleness:* if more than `2 × pca_recompute_every` ticks pass without a successful
recompute, flag `PCA_STALE` on all PCA keys.

### 5.4 Cross-sectional relations (rolling OLS pair residual)

Configured as ordered pairs `(y_symbol, x_symbol)` (config `pairs`). On the **bar grid**
(1 s), with `Y_t = ln m^{y}_t`, `X_t = ln m^{x}_t` (last mid of each leg at the bar
boundary; both legs must have a mid at most `pair_staleness_ms` = 2000 old, else the bar
is skipped and `PAIR_MISSING` flagged):

**Estimator.** Rolling OLS with intercept over `W = xs_window_bars` (default 1800 = 30
min):

$$
\hat{\beta} = \frac{W \sum X_t Y_t - \sum X_t \sum Y_t}{W \sum X_t^2 - (\sum X_t)^2},
\qquad
\hat{\alpha} = \bar{Y} - \hat{\beta}\,\bar{X}
$$

maintained via ring buffer + rolling sums \( \sum X, \sum Y, \sum X^2, \sum XY \)
(O(1) per bar). **Refit cadence:** \( \hat\alpha, \hat\beta \) and the residual scale are
re-derived every `xs_refit_every_bars` (default 60 = 1 min); between refits they are held
fixed. At each refit, rolling sums are also recomputed exactly from the ring buffer
(O(W)) to cancel float drift.

**Residual and z-score.** At every bar, with the cached fit:

$$
\varepsilon_t = Y_t - \hat{\alpha} - \hat{\beta} X_t,
\qquad
\mathrm{resid\_z}(t) = \frac{\varepsilon_t - \bar{\varepsilon}}{s_{\varepsilon}}
$$

where \( \bar{\varepsilon}, s_{\varepsilon} \) (ddof = 1) are computed **at refit time**
over the window's residuals under the newly fitted \( \hat\alpha, \hat\beta \)
(note \( \bar\varepsilon = 0 \) exactly at refit, by OLS with intercept; it is stored
anyway for symmetry). Guards: denominator of \( \hat\beta \) < ε (X constant) or
\( s_\varepsilon < \varepsilon \) → `resid_z = NaN` + `NUMERIC_FALLBACK`. `resid_z` is
clamped to `± z_clamp` (default ±8, flag `CLAMPED`).

**Stability gate (v1 cointegration proxy).** v1 deliberately does not run
Johansen/ADF online. Instead the rolling Pearson correlation \( \rho_{XY} \) over the
same window (from the same sums plus \( \sum Y^2 \)) gates the feature: if
\( |\rho_{XY}| < \) `xs_min_abs_corr` (default 0.30) at refit, the pair is considered
unstable; `resid_z` and `beta` emit `NaN` + `PAIR_MISSING` until a refit passes the gate.

**Emission.** The identical values `xs.{partner}.beta` = \( \hat\beta \) and
`xs.{partner}.resid_z` = z are injected into **both** legs' FeatureVectors (partner named
in the key). Sign convention: positive z means `y` is rich relative to
\( \hat\alpha + \hat\beta X \). Block 4 owns the interpretation per leg.

*Complexity:* O(1) per bar per pair; O(W) at refit cadence.
*Warm-up:* `NaN` + `WARMUP` until `W` bars observed and first refit done.

### 5.5 Temporal / regime statistics

All on the bar grid (1 s log-mid returns `r_t`), each with its own ring buffer + rolling
sums; O(1) per bar.

**(a) Variance ratio** `tmp.vr.q8.w600` — Lo–MacKinlay overlapping estimator, lag
`q = vr_lag` (default 8), window `T = vr_window_bars` (default 600 = 10 min), **no**
small-sample bias correction (deterministic and simpler; the z-score absorbs level):

$$
\hat{\mu} = \frac{1}{T}\sum_{t=1}^{T} r_t,
\qquad
\hat{\sigma}^2_1 = \frac{1}{T}\sum_{t=1}^{T} (r_t - \hat{\mu})^2
$$

$$
\hat{\sigma}^2_q = \frac{1}{T - q + 1}
\sum_{t=q}^{T} \Big( \textstyle\sum_{j=0}^{q-1} r_{t-j} \;-\; q\hat{\mu} \Big)^{2},
\qquad
\mathrm{VR}(q) = \frac{\hat{\sigma}^2_q}{q\,\hat{\sigma}^2_1}
$$

VR ≈ 1: random walk; VR > 1: momentum/trending; VR < 1: mean reversion.
Incremental state: ring buffer of `r` (length T), rolling `Σr`, `Σr²`; the q-block sum
\( s_t = s_{t-1} + r_t - r_{t-q} \); ring buffer of `s` (length T−q+1) with rolling
`Σs`, `Σs²`. Then
\( \hat\sigma^2_q = \frac{1}{T-q+1}\left(\Sigma s^2 - 2q\hat\mu\,\Sigma s + (T{-}q{+}1) q^2 \hat\mu^2\right) \).
Guard: \( \hat\sigma^2_1 < \varepsilon \) → `NaN` + `NUMERIC_FALLBACK`.

**(b) Lag-1 autocorrelation** `tmp.acf.l1.w120` — window `W = acf_window_bars`
(default 120 = 2 min):

$$
\hat{\rho}_1 = \frac{\sum_{t=2}^{W} (r_t - \bar{r})(r_{t-1} - \bar{r})}
                    {\sum_{t=1}^{W} (r_t - \bar{r})^{2}}
$$

O(1) exact update via rolling sums: with `S = Σr`, `Q = Σr²`,
`P = Σ_{t=2..W} r_t r_{t−1}` (rolling: add `r_new·r_prev`, subtract the evicted product),
oldest `r_1` and newest `r_W` read from the ring buffer:

$$
\hat{\rho}_1 = \frac{P - \bar{r}\,(2S - r_1 - r_W) + (W-1)\bar{r}^2}{Q - W \bar{r}^2}
$$

Guard: denominator < ε → `NaN` + `NUMERIC_FALLBACK`. Output clamped to [−1, 1]
(flag `CLAMPED` if clamping fired — indicates float noise).

**(c) Realized volatility** `tmp.rv.w120` — root mean square of returns, **no mean
subtraction** (standard RV convention), window `W = rv_window_bars` (default 120):

$$
\mathrm{RV} = \sqrt{\frac{1}{W} \sum_{t=1}^{W} r_t^{2}}
\qquad \text{(units: log-return per } \sqrt{\text{bar}}\text{, bar = 1 s)}
$$

O(1) via rolling `Σr²`. RV of an all-zero window is 0.0 (valid, not a fallback).

*Warm-up for all three:* `NaN` + `WARMUP` until the respective window is full.
Bars discarded due to feed gaps (§8.3) do not enter the buffers; the window refills.

### 5.6 Trade-tape features

Let trade `k` have size \( v_k > 0 \) and sign \( d_k = +1 \) if `aggressor_side == BUY`
else −1 (Block 1 supplies the aggressor flag; **no** Lee–Ready or bulk-volume
classification is performed in v1).

**(a) Signed volume** `tape.sv.w10s` — rolling sum over 10 s, binned per fast-grid bin
exactly like OFI (§5.1):

$$
\mathrm{SV}(t) = \sum_{k :\; t - 10\,\mathrm{s} < ts_k \le t} d_k v_k
\qquad \text{(size units)}
$$

**(b) Aggressor imbalance** `tape.ai.w{W}s`, W ∈ {10, 60}:

$$
\mathrm{AI}_W(t) = \frac{\sum_{k \in W} d_k v_k}{\sum_{k \in W} v_k} \in [-1, 1]
$$

Guard: no trades in window (denominator 0) → **0.0** with `NUMERIC_FALLBACK` (an empty
tape is genuinely "no imbalance"; NaN would needlessly poison downstream).

**(c) VPIN-style toxicity** `tape.vpin` — volume-bucket flow imbalance:

- Bucket size `V = vpin_bucket_volume` (config, per instrument; default 10 000 units).
  Fixed per run — **not** adaptive in v1 (determinism).
- **Bucketing rule (exact):** trades fill the current bucket in arrival order. A trade
  that would overflow the bucket is **split**: the first
  \( V - \text{fill} \) units close the current bucket, the remainder opens the next
  bucket(s) (a single trade may close multiple buckets). Buy/sell volume is accumulated
  per bucket from the split portions using the aggressor sign.
- Per completed bucket `b`: \( \iota_b = |V^{buy}_b - V^{sell}_b| / V \in [0,1] \).
- Feature: mean over the last `n = vpin_n_buckets` (default 20) **completed** buckets:

$$
\mathrm{VPIN} = \frac{1}{n} \sum_{b=1}^{n} \iota_b
$$

*Complexity:* O(1) amortized per trade (splits are rare; a trade spanning `c` buckets
costs O(c)). *Warm-up:* `NaN` + `WARMUP` until `n` buckets completed. Buckets are
time-unbounded by design (VPIN is event-clock, not wall-clock).

---

## 6. Normalization: rolling z-scores

For every key marked "z: yes" in §3.2, the engine also emits `{key}.z`:

$$
z_t = \operatorname{clamp}\!\left(
\frac{f_t - \hat{\mu}_t}{\hat{s}_t},\; -z_{\max},\; +z_{\max}\right),
\qquad z_{\max} = \texttt{z\_clamp} = 8.0
$$

where \( \hat\mu_t, \hat s_t \) are the rolling mean and standard deviation (ddof = 1) of
the **raw** feature over the last `z_window_samples` fast-grid emissions (default 3600 =
6 min), maintained per key via ring buffer + rolling `Σf`, `Σf²`, with an exact O(N)
sum recompute every `z_resync_every` (default 10 000) updates to cancel float drift.

Rules, in order:

1. Raw value is `NaN` → it does **not** enter the buffer; `{key}.z = NaN`, propagate the
   raw key's flags to the z key.
2. Fewer than `z_min_samples` (default 300) values in buffer → `{key}.z = NaN` + `WARMUP`.
3. \( \hat s_t < \) `eps_sigma` (default 1e−12) → `{key}.z = 0.0` + `NUMERIC_FALLBACK`
   (constant feature ⇒ no deviation).
4. Clamping fired → flag `CLAMPED`.

The `Normalizer` also serves `FeatureStats{instrument, name, ts, rolling_mean=μ̂,
rolling_std=ŝ, n_samples}` for every z-scored key, published every `stats_emit_every_s`
(default 10 s of event time) to Block 9.

---

## 7. Data structures

```python
from dataclasses import dataclass
from enum import IntFlag


class QualityFlag(IntFlag):
    OK               = 0
    WARMUP           = 1 << 0   # window not yet filled / min samples not reached
    STALE_INPUT      = 1 << 1   # BookIntegrity not ok / crossed book upstream
    ONE_SIDED_BOOK   = 1 << 2   # a required book side/level absent
    FEED_GAP         = 1 << 3   # window was reset or a bar was dropped due to a gap
    PAIR_MISSING     = 1 << 4   # partner leg stale/absent, or stability gate failed
    NUMERIC_FALLBACK = 1 << 5   # guarded division / degenerate variance / eig failure
    PCA_STALE        = 1 << 6   # PCA cache older than 2 recompute cadences
    CLAMPED          = 1 << 7   # output hit a configured clamp


@dataclass(frozen=True, slots=True)
class FeatureVector:
    instrument: str
    ts: int                                  # ns since epoch UTC; fast-grid boundary
    features: dict[str, float]               # STABLE key set; NaN when unavailable
    quality_flags: QualityFlag               # OR of all per-feature flags this tick
    feature_flags: dict[str, QualityFlag]    # per-key flags; only non-OK keys present
    seq: int                                 # per-instrument monotone emission counter


@dataclass(frozen=True, slots=True)
class FeatureStats:
    instrument: str
    name: str                                # raw feature key (not the .z key)
    ts: int
    rolling_mean: float
    rolling_std: float
    n_samples: int
```

**NaN / warm-up semantics (normative):**

- The key set of `features` is identical for every vector of a given instrument for the
  whole run, and is derivable from config alone (Block 4 may build index maps once).
- `NaN` means "not computable this tick"; the reason is always in `feature_flags[key]`.
  A key is never silently omitted and a value is never fabricated except the explicit
  fallbacks in §5/§6 (which always carry `NUMERIC_FALLBACK`).
- `quality_flags == OK` ⟺ every feature is finite and un-flagged. Block 4 may use this
  as a cheap gate.
- All floats are IEEE-754 doubles; `+inf/−inf` must never be emitted (guards in §5
  make this impossible; the property test in §11 enforces it).

---

## 8. Error handling

| # | Condition | Detection | Behavior | Flags on affected keys |
|---|---|---|---|---|
| E1 | Warm-up (any window not full) | per-calculator counters | emit `NaN` for affected keys, keep updating | `WARMUP` |
| E2 | One-sided / empty book side | `len(bids)==0 or len(asks)==0` at level 1 | skip OFI transition (treat as no event), `qi/dwi = NaN`; PCA samples missing levels as 0 | `ONE_SIDED_BOOK` |
| E3 | Crossed / stale book | `BookIntegrity{ok=False}` or `crossed_book` | freeze calculator updates until `ok=True`; keep emitting (values from last good state) | `STALE_INPUT` |
| E4 | Feed gap (staleness > `gap_reset_ms`, or Block 2 re-snapshot) | `BookIntegrity.staleness_ms`; bar-boundary jump > 1 bar | `pipeline.reset(FEED_GAP)`: clear OFI/tape/temporal buffers and PCA row buffer; keep z-score buffers (levels are still comparable) and drop the gap-spanning return | `FEED_GAP` then `WARMUP` |
| E5 | Partner leg missing/stale (pairs) | mid older than `pair_staleness_ms` at bar | skip the bar for that pair; emit `NaN` for `xs.*` | `PAIR_MISSING` |
| E6 | Pair stability gate fails | \|ρ\| < `xs_min_abs_corr` at refit | `xs.*` = `NaN` until a passing refit | `PAIR_MISSING` |
| E7 | Zero variance / zero denominator | guard `< eps` before every division | per §5: VR/ACF/resid_z → `NaN`; AI → 0.0; z-score → 0.0; qi/dwi both-sides-empty → `NaN` | `NUMERIC_FALLBACK` |
| E8 | Eigendecomposition failure / non-finite covariance | `numpy.linalg.LinAlgError`, `isfinite` check | keep previous PCA cache; if none, PCA keys `NaN` | `NUMERIC_FALLBACK` (+ `WARMUP` if no cache) |
| E9 | PCA cache too old | tick counter | keep projecting on stale cache | `PCA_STALE` |
| E10 | Non-finite input (`NaN`/`inf` price or size in an event) | `isfinite` on ingest | drop the event, log at WARNING with seq, count in engine metrics | `STALE_INPUT` on next emission |
| E11 | Out-of-order event (`ts` < last seen for instrument) | ts comparison | drop, log at WARNING (Block 2 guarantees ordering; this is defensive) | none |
| E12 | Unknown instrument in event | dict lookup | drop, log once per instrument per run | none |

Errors never raise out of `run()`; any unexpected exception in a calculator is caught at
the dispatch level, logged with the triggering event, the calculator is `reset(FEED_GAP)`,
and processing continues (fail-safe direction, design §4.3 — degraded features flow to
Block 4 flagged, and Block 4 must not trade on flagged features).

---

## 9. Algorithms (numbered pseudocode, with per-event complexity)

### 9.1 `OfiCalculator` — O(K) per BookState, O(1) per bin

```
state: prev_levels (2K (price,size) or None), bins_1s: Ring[10], bins_10s: Ring[100],
       cur_bin_l1, cur_bin_dw, cur_bin_ts, totals for both windows, weights w[1..K]
on_book(b):
  1. if b.bids empty or b.asks empty: flag ONE_SIDED_BOOK; prev_levels = None; return
  2. if prev_levels is None: prev_levels = levels(b); return       # first state anchors
  3. bin_ts = floor(b.ts / 100ms); if bin_ts > cur_bin_ts: roll_bins()  # (≤ window) rolls
  4. for i in 1..K present in both states:
       e_b = q_b[i]*(p_b[i] >= p'_b[i]) - q'_b[i]*(p_b[i] <= p'_b[i])
       e_a = -q_a[i]*(p_a[i] <= p'_a[i]) + q'_a[i]*(p_a[i] >= p'_a[i])
       ofi_i = e_b + e_a
       if i == 1: cur_bin_l1 += ofi_i
       cur_bin_dw += w[i] * ofi_i
  5. prev_levels = levels(b)
roll_bins():   # per empty bin between cur_bin_ts and bin_ts, capped at window length
  6. push cur_bin into rings; totals += cur_bin - evicted; zero cur_bin; advance cur_bin_ts
snapshot(): out[ofi.*] = totals (or NaN+WARMUP if bins_seen < warmup threshold)
reset(): clear everything, prev_levels = None
```

### 9.2 `BookImbalanceCalculator` — O(K) at snapshot only

```
1. keep a reference to the latest good BookState (updated O(1) in on_book)
2. snapshot(): compute qi.l1 and dwi.kK from that state per §5.2 with denominator guards
```

### 9.3 `RollingPcaCalculator` — O(k·2K) per tick; O(T·(2K)² + (2K)³) at cadence

```
state: rows: Ring[T] of float64[2K], tick_count, cache{mu, sigma, V[k], lambdas} or None,
       last_recompute_tick
on_book(b): keep latest state reference (O(1))
on_emit_tick(ts_grid):                       # called by pipeline before snapshot
  1. x = log1p(sizes at K bid + K ask levels of latest state, missing→0); rows.push(x)
  2. tick_count += 1
  3. if tick_count - last_recompute_tick >= pca_recompute_every
        and len(rows) >= pca_min_samples:  recompute()
recompute():
  4. X = rows as (T×2K) array; mu = mean(X,0); sigma = std(X,0,ddof=1); sigma[sigma<eps]=1
  5. C = cov of (X-mu)/sigma; (lambdas, V) = eigh(C) sorted desc; sign-fix each v_j (§5.3)
  6. on LinAlgError/non-finite: keep old cache, set NUMERIC_FALLBACK, return
  7. cache = {...}; last_recompute_tick = tick_count
snapshot():
  8. if cache is None: PCA keys = NaN + WARMUP; return
  9. z = (log1p(latest levels) - mu) / sigma
 10. f_j = dot(z, v_j) for j=1..k;  n2 = dot(z, z)
 11. resid_energy = 1 - sum(f_j^2)/n2   (n2<eps → 0.0 + NUMERIC_FALLBACK)
 12. eff_rank = (Σλ)²/Σλ²; if tick_count-last_recompute_tick > 2·cadence: flag PCA_STALE
```

### 9.4 `PairCalculator` (inside `CrossSectionalEngine`) — O(1) per bar, O(W) per refit

```
state: rings X[W], Y[W]; sums SX,SY,SXX,SXY,SYY; fit{alpha,beta,eps_mean,eps_std,rho} or
       None; bars_since_refit
on_bar(ts, mid_y, mid_x):                    # both legs fresh, else PAIR_MISSING & skip
  1. push (lnX, lnY); update the five rolling sums (add new, subtract evicted)
  2. bars_since_refit += 1
  3. if len == W and bars_since_refit >= xs_refit_every_bars: refit()
refit():
  4. recompute the five sums exactly from the rings (float-drift resync)
  5. beta = (W·SXY − SX·SY)/(W·SXX − SX²)     # denom<eps → fit=None, NUMERIC_FALLBACK
  6. alpha = SY/W − beta·SX/W
  7. eps_i = Y_i − alpha − beta·X_i for the whole ring; eps_mean, eps_std (ddof=1)
  8. rho = (W·SXY−SX·SY)/sqrt((W·SXX−SX²)(W·SYY−SY²)); if |rho|<xs_min_abs_corr:
       fit=None, flag PAIR_MISSING; else store fit; bars_since_refit = 0
inject(ts, out, flags):                       # called for BOTH legs at emission
  9. if fit is None: xs.* = NaN (+WARMUP or PAIR_MISSING); return
 10. eps = lnY_latest − alpha − beta·lnX_latest
 11. z = clamp((eps − eps_mean)/eps_std, ±z_clamp)   # eps_std<eps → NaN+NUMERIC_FALLBACK
 12. out[xs.{p}.beta] = beta; out[xs.{p}.resid_z] = z
```

### 9.5 `TemporalCalculator` — O(1) per bar

```
state: prev_ln_mid; rings r[T_vr], s[T_vr−q+1], prod-ring for ACF window;
       sums Σr, Σr² (per window length needed), Σs, Σs², P
on_bar(ts, mid):
  1. if prev_ln_mid is None or gap-flagged: prev_ln_mid = ln(mid); return   # drop return
  2. r_new = ln(mid) − prev_ln_mid; prev_ln_mid = ln(mid)
  3. push r_new into each window ring; update Σr, Σr² (add/subtract evicted)
  4. s_new = s_prev + r_new − r[t−q] (0 while filling); push; update Σs, Σs²
  5. ACF: P += r_new·r_prev − evicted_product (products stored in their own ring)
snapshot(): evaluate VR, ρ̂₁, RV from the sums per §5.5 with guards; WARMUP until full
```

### 9.6 `TapeCalculator` — O(1) amortized per TradePrint

```
state: fast-grid bins (signed vol, abs vol) for w10s/w60s with rolling totals (as §9.1);
       vpin{fill, vbuy, vsell, iota_ring[n], Σiota}
on_trade(tr):
  1. d = +1 if BUY else −1; v = tr.size; roll bins to tr.ts_local
  2. cur_bin.signed += d·v; cur_bin.abs += v
  3. rem = v
  4. while rem > 0:                                  # bucket split loop
       take = min(rem, V − fill)
       (vbuy if d>0 else vsell) += take; fill += take; rem −= take
       if fill == V: iota = |vbuy − vsell|/V; push to ring (update Σiota); vbuy=vsell=fill=0
snapshot(): sv/ai from totals (ai: denom 0 → 0.0+NUMERIC_FALLBACK);
            vpin = Σiota/n, WARMUP until n complete buckets
```

### 9.7 `Normalizer` — O(1) per key per emission

```
per key: ring f[N], Σf, Σf², count, updates_since_resync
1. if isnan(raw): out[key.z]=NaN; copy raw key's flags; return
2. push raw; update sums; count = min(count+1, N); resync sums exactly every z_resync_every
3. if count < z_min_samples: out[key.z]=NaN + WARMUP; return
4. mu = Σf/count; var = (Σf² − count·mu²)/(count−1); var = max(var, 0.0)
5. if sqrt(var) < eps_sigma: out[key.z] = 0.0 + NUMERIC_FALLBACK
   else z = (raw−mu)/sqrt(var); clamp to ±z_clamp (flag CLAMPED); out[key.z] = z
```

---

## 10. Configuration

Loaded from the run's versioned YAML (design §4.4) under key `feature_engine`. All
defaults below are the normative v1 defaults.

```yaml
feature_engine:
  fast_grid_ms: 100            # int, [10, 1000].    FeatureVector emission period
  bar_grid_ms: 1000            # int, multiple of fast_grid_ms, [100, 10000]
  levels_k: 10                 # int, [1, 50].       K book levels per side
  eps_denom: 1.0e-12           # float, > 0.         universal division guard
  gap_reset_ms: 2000           # int, [200, 60000].  staleness that triggers reset
  min_warmup_fraction: 0.5     # float, (0, 1].      OFI/tape early-validity fraction

  ofi:
    windows_s: [1, 10]         # list[int], each [1, 300]
    level_decay: 0.7           # float λ, [0, 5].    shared with dwi

  pca:
    factors: 3                 # int k, [1, 10], k <= 2*levels_k
    window_samples: 600        # int T, [100, 10000] fast-grid ticks (600 = 60 s)
    min_samples: 300           # int, [50, window_samples]
    recompute_every: 50        # int ticks, [10, 1000] (50 = 5 s)

  cross_sectional:
    window_bars: 1800          # int W, [300, 20000] (1800 = 30 min)
    refit_every_bars: 60       # int, [10, window_bars]
    min_abs_corr: 0.30         # float, [0, 1).      stability gate
    pair_staleness_ms: 2000    # int, [500, 10000]
    pairs:                     # list of ordered [y, x]; a symbol may appear in many pairs
      - [AAPL, MSFT]
      - [SPY, QQQ]

  temporal:
    vr_lag: 8                  # int q, [2, 32]
    vr_window_bars: 600        # int T, [10*q, 10000]
    acf_window_bars: 120       # int, [30, 5000]
    rv_window_bars: 120        # int, [30, 5000]

  tape:
    ai_windows_s: [10, 60]     # list[int], each [1, 600]
    sv_window_s: 10            # int, [1, 600]
    vpin_bucket_volume:        # float per instrument, > 0 (size units)
      default: 10000.0
      AAPL: 20000.0
    vpin_n_buckets: 20         # int n, [5, 200]

  contract_exports:            # Block 4 signal-contract aliases (§3.3)
    enabled: true              # bool; MUST be true when Block 4 runs
    ofi_level_decay: 0.7       # float λ for the K=5 contract OFI, [0, 5]

  normalization:
    z_window_samples: 3600     # int N, [600, 100000] fast-grid emissions (3600 = 6 min)
    z_min_samples: 300         # int, [30, z_window_samples]
    z_clamp: 8.0               # float, [3, 20]
    eps_sigma: 1.0e-12         # float, > 0
    z_resync_every: 10000      # int, [1000, 10^7]

  stats_emit_every_s: 10       # int, [1, 300].      FeatureStats cadence (event time)
```

Validation at startup (fail fast, refuse to run): types and ranges as annotated;
`bar_grid_ms % fast_grid_ms == 0`; every pair symbol is in the instrument universe; the
generated feature-key set contains no duplicates. Config is bound into frozen
`FeatureEngineConfig` / `PairConfig` / `ZScoreConfig` dataclasses with full type hints.

---

## 11. Test plan

Unit tests feed hand-built `BookState`/`TradePrint` sequences directly into calculators
(no bus). All expected values below are exact or given to the stated precision.

### T1 — OFI, hand-computed (worked example)

K = 2, λ = 0.7 ⇒ w₁ = 1, w₂ = e^(−0.7) = 0.496585…; all four states within one 1 s window.

| State | Bids (p, q) | Asks (p, q) |
|---|---|---|
| S0 | (100.00, 50), (99.99, 80) | (100.02, 40), (100.03, 60) |
| S1 | (100.00, 70), (99.99, 80) | unchanged |
| S2 | (100.01, 10), (100.00, 70) | unchanged |
| S3 | unchanged | (100.02, 90), (100.03, 60) |

Per-transition, per §5.1:

- S0→S1: level 1 bid: prices equal ⇒ e¹_b = 70 − 50 = 20; ask e¹_a = −40 + 40 = 0;
  level 2 all unchanged ⇒ 0. **ofi₁ = 20, ofi₂ = 0; dw = 20.**
- S1→S2: level 1 bid price ↑ (100.01 > 100.00) ⇒ e¹_b = 10·1 − 70·0 = 10;
  level 2 bid price ↑ (100.00 > 99.99) ⇒ e²_b = 70·1 − 80·0 = 70; asks 0.
  **ofi₁ = 10, ofi₂ = 70; dw = 10 + 0.496585·70 = 44.7610.**
- S2→S3: level 1 ask: prices equal ⇒ e¹_a = −90 + 40 = −50. **ofi₁ = −50; dw = −50.**

Assert windowed sums after S3: `ofi.l1.w1s = 20 + 10 − 50 = −20.0` (exact);
`ofi.dw.w1s = 20 + 44.7610 − 50 = 14.7610` (tol 1e−4). Also assert `book.qi.l1` at S3 =
(10 − 90)/(10 + 90) = **−0.8** exactly, and `book.dwi.k2` =
(1·10 + 0.496585·70 − 1·90 − 0.496585·60) / (1·100 + 0.496585·130)
= −75.03415 / 164.55605 = **−0.45598** (tol 1e−5).

### T2 — OFI eviction

Same as T1 but S3 arrives 1.2 s after S1: assert the S0→S1 contribution has been evicted
from `ofi.l1.w1s` but is still present in `ofi.l1.w10s`.

### T3 — Variance ratio, hand-computed (worked example)

q = 2, T = 8, per §5.5(a), no bias correction.

- Alternating returns r = (1, −1, 1, −1, 1, −1, 1, −1): μ̂ = 0, σ̂²₁ = 1; every 2-block
  sum is 0 ⇒ σ̂²_q = 0 ⇒ **VR = 0.0** (perfect mean reversion), exact.
- Trending returns r = (1, 1, 1, 1, −1, −1, −1, −1): μ̂ = 0, σ̂²₁ = 1; the 7 overlapping
  2-sums are (2, 2, 2, 0, −2, −2, −2), squares sum 24 ⇒ σ̂²_q = 24/7 ⇒
  **VR = 24/14 = 12/7 = 1.714285…** (tol 1e−9).

### T4 — ACF and RV, hand-computed

r = (1, −1, 1, −1), W = 4: r̄ = 0, denominator 4, numerator (−1)(1)+(1)(−1)+(−1)(1) = −3
⇒ **ρ̂₁ = −0.75** exact. RV over r = (10bp, −10bp, 20bp, 0), W = 4:
√((1 + 1 + 4 + 0)·10⁻⁶/4) = **1.224745·10⁻³** (tol 1e−9).

### T5 — Rolling OLS pair residual, hand-computed

W = 4, X = (1, 2, 3, 4), Y = (3.1, 4.9, 7.2, 8.8) (treat directly as ln-mids):
ΣX = 10, ΣY = 24, ΣX² = 30, ΣXY = 69.7 ⇒ **β̂ = 38.8/20 = 1.94**, **α̂ = 1.15**.
Residuals (0.01, −0.13, 0.23, −0.11), mean 0 exactly, s_ε = √(0.082/3) = 0.165328
⇒ z of the last bar = −0.11/0.165328 = **−0.66534** (tol 1e−5). Assert both legs receive
identical `xs.*.beta` / `xs.*.resid_z` values.

### T6 — VPIN bucketing, hand-computed

V = 100, n = 2. Trades: B60, S30, B50, S80, B40.
Bucket 1: B60 + S30 + first 10 of B50 ⇒ ι₁ = |70 − 30|/100 = 0.40.
Bucket 2: remaining B40 + first 60 of S80 ⇒ ι₂ = |40 − 60|/100 = 0.20.
Bucket 3 (incomplete): S20 + B40. Assert **VPIN = (0.40 + 0.20)/2 = 0.30** exactly, and
`WARMUP` before bucket 2 completes. Also: single B250 trade must close 2 full buckets
(ι = 1.0 each) from an empty state.

### T7 — Aggressor imbalance & signed volume

Trades in 10 s: B100, S40 ⇒ `tape.sv.w10s = 60`, `tape.ai.w10s = 60/140 = 0.428571`
(tol 1e−9). Empty window ⇒ `ai = 0.0` with `NUMERIC_FALLBACK`.

### T8 — Rolling z-score, hand-computed

N = 5, min = 5, values (1, 2, 3, 4, 5): μ̂ = 3, ŝ = √2.5 = 1.581139 ⇒ z of 5 =
**1.264911** (tol 1e−6). With min = 5 and only 4 values: `NaN` + `WARMUP`. Constant
input (7, 7, 7, 7, 7): z = **0.0** + `NUMERIC_FALLBACK`. Value 10⁶ after (1..5):
z clamps to **+8.0** + `CLAMPED`.

### T9 — PCA on a synthetic rank-1 book

Construct T = 300 samples where all 2K log-depth columns equal `c·s_t` for a common
scalar series `s_t` (plus one constant column to exercise the σ→1 guard). After
recompute: `pca.eff_rank` ≈ 1 (tol 0.05), `pca.resid_energy` ≈ 0 (tol 1e−6), `pca.f1`
finite; sign convention: largest-|component| entry of v₁ positive. Determinism: run
twice, assert bit-identical outputs.

### T10 — Degenerate cases (one per family)

| Family | Input | Expected |
|---|---|---|
| OFI | first BookState ever (no previous) | no increment; window `WARMUP` |
| OFI/imbalance | asks empty | ofi transition skipped; `qi/dwi = NaN` + `ONE_SIDED_BOOK` |
| PCA | frozen book (all columns constant) | σ-guard ⇒ z = 0 vector ⇒ `resid_energy = 0.0` + `NUMERIC_FALLBACK`; no crash in `eigh` |
| XS | X constant (β denominator 0) | `xs.* = NaN` + `NUMERIC_FALLBACK` |
| XS | ρ = 0.1 < 0.30 gate | `xs.* = NaN` + `PAIR_MISSING` |
| Temporal | constant mid (all r = 0) | VR, ACF `NaN` + `NUMERIC_FALLBACK`; RV = 0.0 valid |
| Tape | no trades ever | sv = 0, ai = 0 + `NUMERIC_FALLBACK`, vpin `NaN` + `WARMUP` |
| Normalizer | raw NaN stream | z stays `NaN`, buffer never grows |

### T11 — Gap handling

Feed 30 s of data, then a `BookIntegrity{staleness_ms: 5000}`, then resume: assert
OFI/temporal/tape buffers reset (`FEED_GAP` then `WARMUP`), the gap-spanning return is
absent from temporal windows, and z-score buffers were **kept**.

### T12 — Emission contract

Replay a 10 s event file: exactly one `FeatureVector` per 100 ms boundary per instrument,
`ts` on the grid, `seq` gapless, and the key set identical in every vector and equal to
the config-derived set.

### T13 — Property tests (hypothesis)

Random valid event streams (books uncrossed, sizes ≥ 0, ts monotone):
(a) every emitted value is finite or `NaN` — never ±inf; (b) `NaN` ⟺ a per-key flag is
set; (c) all `.z` values ∈ [−8, 8]; (d) `qi/dwi/ai ∈ [−1, 1]`, `vpin/resid_energy ∈
[0, 1]`, `eff_rank ∈ [1, 2K]`, `VR ≥ 0`, `RV ≥ 0`; (e) determinism: same stream twice ⇒
byte-identical vector sequence; (f) calculators never raise.

### T14 — Cross-check against batch reference

For a 5-minute random stream, compare every incremental feature against a slow
NumPy/pandas batch recomputation from scratch at 20 random ticks; max abs diff < 1e−9
(< 1e−6 for PCA projections).

---

## 12. Performance budget and memory bounds

Targets on the reference machine (single core, Python 3.11 + NumPy), per instrument,
measured by the benchmark harness in CI:

| Path | Budget |
|---|---|
| `BookState` handling (no emission) | p50 ≤ 20 µs, p99 ≤ 100 µs |
| `TradePrint` handling | p50 ≤ 5 µs, p99 ≤ 50 µs |
| Emission tick (snapshot + normalize + publish) | p50 ≤ 60 µs, p99 ≤ 300 µs |
| PCA recompute (K = 10, T = 600, every 5 s) | ≤ 5 ms |
| Pair refit (W = 1800, per pair, every 60 s) | ≤ 2 ms |
| End-to-end added latency (event in → vector out) | ≤ 1 ms p99, excluding recompute ticks |

Recompute/refit run inline in the asyncio task; the 5 ms PCA spike every 5 s is within
the 10–500 ms system reaction budget (design §1.2) and must be verified not to starve the
input queue at 10 000 events/s.

**Memory (defaults, per instrument, float64):** PCA rows 600·20·8 ≈ 96 KB; OFI/tape bins
≤ (100 + 600 + 100)·3·8 ≈ 20 KB; temporal rings ≈ 3·600·8 ≈ 15 KB; z-score buffers
(9 z-scored keys) 9·3600·8 ≈ 260 KB; per pair 5·1800·8 ≈ 72 KB. **Hard bound: ≤ 2 MB per instrument plus
≤ 128 KB per configured pair**, allocated once at startup (all rings preallocated; the
steady state performs zero per-event heap allocation except the emission dict, which may
be built from a preallocated key list).

---

## 13. Acceptance criteria

- [ ] All §11 tests implemented and green, including the exact worked values T1, T3–T8.
- [ ] Emits exactly one `FeatureVector` per instrument per 100 ms of event time while the
      feed is live; key set stable and config-derived (T12).
- [ ] Every §3.2 feature implemented exactly per §5 formulas; batch cross-check T14 passes.
- [ ] All per-event paths are O(1) or O(K); the only super-linear work is PCA recompute
      and pair refit at their configured cadences (verified by the benchmark harness
      against §12 budgets).
- [ ] `NaN`-with-flag semantics: no ±inf ever emitted; every `NaN` carries a per-key
      flag; `quality_flags` is the OR of per-key flags (property tests T13).
- [ ] Deterministic replay: identical input journal ⇒ byte-identical `FeatureVector`
      stream (T9, T13e); no wall-clock reads anywhere in the block.
- [ ] Feed-gap behavior per E4/T11: reset + `FEED_GAP`/`WARMUP`, z-buffers preserved.
- [ ] `FeatureStats` published every `stats_emit_every_s` for every z-scored key.
- [ ] Startup config validation rejects out-of-range values and unknown pair symbols.
- [ ] Memory within §12 bounds; zero steady-state per-event allocation confirmed with
      `tracemalloc` in the benchmark.
- [ ] No imports from Blocks 4–7; the block consumes only `BookState`, `TradePrint`,
      `BookIntegrity` and produces only `FeatureVector`, `FeatureStats`.
