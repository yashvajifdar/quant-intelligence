# ADR-0002: Four-Factor Composite Weighting

**Status:** Accepted
**Date:** 2026-08-12
**Deciders:** Yash Vajifdar

---

## Context

`compute_combined_factor_score` was initially built with two factors: momentum (60%) and
low-volatility (40%). This reflected the available signal data at the time — the
`fundamentals` table had not been populated.

`etl/ingest_fundamentals.py` and `signals/factors.py` now implement two additional factor
functions: `compute_value_scores` (P/E, P/B, EV/EBITDA composite) and
`compute_quality_scores` (ROE, gross margin, inverse debt/equity, FCF composite).

This ADR decides how to blend all four factors into a single composite rank and why.

---

## Options considered

**Option A: Equal weight (25/25/25/25)**
- Simple to explain; no implicit bias toward any factor
- Ignores the well-documented differential alpha contribution of each factor
- Momentum's higher alpha decays faster than quality/value; equal weighting sacrifices its contribution

**Option B: Tilt toward momentum, add value/quality symmetrically (40/20/20/20)**
- Preserves momentum's documented alpha dominance
- Value and quality equally weighted — does not reflect quality's stronger empirical standing in recent decades

**Option C: Momentum-heavy with quality tilt, smaller value weight (40/25/20/15)**
- Momentum: largest weight, consistent with AQR and Jegadeesh & Titman findings that momentum
  explains the most cross-sectional return variation
- Quality: second-highest, added evidence from Novy-Marx (2013) showing quality adds alpha
  orthogonal to momentum; also mitigates momentum crash risk during recoveries
- Low-vol: defensive; explicit platform target is max drawdown < -20%; low-vol reduces
  realized vol of the composite
- Value: lowest weight; value has underperformed for a decade relative to its historical
  premium (Fama & French 1993 vs realized 2010–2023); including it at a low weight
  maintains factor diversification without overweighting a signal in a weak regime

---

## Decision

**Option C — 40% momentum / 25% quality / 20% low-vol / 15% value.**

The weights are not arbitrary: each percentage point reflects relative academic evidence
strength and practical alpha contribution over the prior decade:

| Factor | Weight | Primary reason |
|---|---|---|
| Momentum | 40% | Strongest single alpha source; Jegadeesh & Titman (1993), AQR (2012) |
| Quality | 25% | Orthogonal alpha to momentum; guards against crashes; Novy-Marx (2013) |
| Low-vol | 20% | Reduces realized portfolio vol; supports max-drawdown target of < −20% |
| Value | 15% | Factor diversification; underweighted due to decade-long premium compression |

Weights sum to exactly 100% and are parameterized in the function signature so they can be
changed without touching the engine or API layer.

---

## Implementation

`signals/factors.py` — `compute_combined_factor_score` now merges all four factor DataFrames
on `ticker` (inner join — tickers missing any factor are excluded from the composite), computes
a weighted average using the weights above, and exposes `value_score` and `quality_score` in
the returned DataFrame for downstream display and engine use.

Tickers without fundamentals data (because the weekly fundamentals ETL has not yet been run,
or yfinance returned insufficient data for that ticker) are dropped from the composite
rather than imputed. The quality report from `etl/loader.py --with-fundamentals` shows
how many tickers were successfully loaded.

---

## Consequences

**What becomes easier:**
- The composite now captures four independent risk premia rather than two
- Quality inclusion reduces momentum crash exposure during market recoveries
- Value inclusion provides counter-cyclical diversification against momentum

**What to watch:**
- Tickers with null fundamentals data are excluded from the composite; if the fundamentals
  ETL fails for a large fraction of the universe, the composite degrades to a subset of tickers
- Value underperformance may persist — monitor whether value factor inclusion improves or
  hurts composite Sharpe over 12 months of paper trading
- If the composite shrinks materially (< 400 tickers) due to fundamentals gaps, consider
  falling back to the two-factor composite for the missing tickers rather than excluding them

**What to revisit:**
- After 6 months of paper trading, compute the realized factor contribution and adjust weights
  if one factor is dragging the composite down systematically
- If fundamentals data quality improves (e.g., switching from yfinance to Polygon or
  Compustat), reconsider whether to expand value metrics to P/S
