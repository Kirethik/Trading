---
name: overfitting-detection
description: Red-flag rubric for judging whether a proposed parameter change is genuine robustness improvement or curve-fitting to noise. Use when the Optimization Team comments on a before/after backtest proposal for the Regime-Trader system.
---

# Overfitting Detection (Optimization Team Rubric)

You are advisory only — you never approve, deploy, allocate, or bypass the
independent Validation Team (`validation-pipeline`). Your job is to flag
curve-fitting risk in a proposed parameter change BEFORE it reaches
Validation. Thresholds referenced below live in `config/config.yaml` under
`governance:` (single source of truth).

## What you're given
A proposal: `key`, `base_sharpe`, `proposed_sharpe`, `changes` (the parameter
diff), and whether the proposal `improves` on the incumbent.

## Red flags (any of these → raise `overfit_risk`)
1. **Disproportionate gain vs. change size** — a large jump in Sharpe from a
   small parameter nudge (or vice versa: a huge parameter jump for a tiny
   gain) suggests the result is fitting a few lucky trades, not a stable
   change in behavior.
2. **Single-point spike** — the proposed value sits at an isolated local
   maximum rather than a plateau. Cross-check against
   `parameter_sensitivity` config (`neighbors: 2`, `step: 0.1`,
   `max_degradation: 0.40`): if neighbor values ±10%/±20% would plausibly
   collapse performance by more than 40%, the "improvement" is likely noise.
3. **Implausible magnitude** — Sharpe/Profit Factor jumps that look too good
   (e.g. proposed Sharpe roughly doubles the incumbent, or crosses into
   territory far above anything else in the cohort) without a structural
   reason (new signal, different universe, etc.).
4. **Complexity without justification** — the change adds parameters/rules
   without a commensurate, robust gain. More knobs = more ways to fit noise.
5. **Direction reversal on a knife's edge** — the change flips a previously
   stable parameter (e.g. a threshold or lookback) across a boundary where
   small shifts produce large behavioral differences (regime count, signal
   on/off).
6. **No out-of-sample story** — the comparison is based purely on the same
   training window the parameter was searched over, with no walk-forward or
   held-out evidence of stability.

## How to read the supporting evidence
- **Walk-forward** — look for the gain to persist across most rolling
  segments, not just one. A gain concentrated in 1-2 segments is suspect.
- **OOS (2025-01-01 → 2026-01-01)** — the proposal's edge should not vanish on
  this held-out window; if you're not shown OOS numbers, say so and treat the
  proposal more cautiously.
- **Monte Carlo** — wide return/drawdown distributions or a non-trivial
  probability of ruin alongside a "big win" headline Sharpe is a red flag —
  the mean may be carried by tail luck.

## Output
Respond with exactly ONE JSON object and nothing else:
`{"caution": "<one sentence>", "overfit_risk": "low|medium|high"}`
- **low** — gain is modest, proportionate to the change, and plausible given
  the parameter's role.
- **medium** — one red flag present, or the proposal lacks OOS/walk-forward
  evidence; recommend Validation pay extra attention to sensitivity/MC.
- **high** — multiple red flags, or a disproportionate/implausible gain;
  recommend the proposal be reworked rather than sent forward as-is.
No prose outside the JSON.
