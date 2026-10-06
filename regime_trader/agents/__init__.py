"""Gemini (Google ADK) agent runtime — the 'intelligent thinking' layer.

PAPER ONLY, permanently. Agents may act (including paper orders) without
human approval, but every order still passes the RiskManager veto, and any
agent failure degrades to the deterministic rules (callers receive None).
"""
