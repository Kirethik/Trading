"""Governance / Optimization / Validation layer.

A continuous, governed research loop on top of the trading core:

  Data -> Research -> Optimization (Sonnet) -> INDEPENDENT Validation (Sonnet)
       -> overfitting detection -> reports -> Governance (Judge agent, final authority)
       -> paper reallocation (through the risk veto, never around it).

Rules-first hybrid: deterministic thresholds decide the unambiguous cases;
the Claude Code agents only arbitrate borderline / conflict / allocation calls,
and the daily job degrades to deterministic rules if they are unavailable.
"""
