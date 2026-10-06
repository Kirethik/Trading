"""Web dashboard for the Regime-Trader governance system.

A FastAPI app (served by uvicorn on EC2 :80) that reads the same on-disk
artifacts the nightly governance cycle writes and renders them as interactive
charts + tables, behind a password gate. Controls trigger only SAFE,
research-only commands as background subprocesses — the web layer never places
an order and never bypasses the risk veto.
"""
