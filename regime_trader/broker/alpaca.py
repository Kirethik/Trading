"""Alpaca broker wrapper. Paper by default; live is gated twice.

- Startup health check, automatic retry with backoff on every API call.
- Limit orders by default with timed cancel/retry escalation.
- Bracket (entry + stop + optional take-profit) for equities; crypto gets
  plain limit orders with software stops (Alpaca crypto has no brackets).
- Stops only tighten. Clean cancel-all / close-all.
- client_order_id carries the signal_id: signal -> risk -> order -> fill is
  one traceable chain in the logs and on the Alpaca side.
- Real-time fill stream with auto-reconnect; startup position reconciliation.
"""
from __future__ import annotations

import threading
import time

from ..config import get_config
from ..data.provider import is_crypto
from ..monitor.log_setup import get_logger
from ..utils import utc_now_iso

log = get_logger("trades")


class LiveTradingRefused(RuntimeError):
    pass


class AlpacaBroker:
    def __init__(self, paper: bool | None = None, live_confirmation: str | None = None):
        cfg = get_config()
        self.cfg = cfg
        self.paper = cfg.alpaca_paper if paper is None else paper
        if not self.paper:
            phrase = cfg.broker.live_confirmation_phrase
            if live_confirmation != phrase:
                raise LiveTradingRefused(
                    f"Live trading requires the exact confirmation phrase {phrase!r}. "
                    "Paper trading is the default for a reason."
                )
            log.critical("LIVE TRADING MODE CONFIRMED at %s", utc_now_iso())
        self.retry_backoff = list(cfg.broker.retry_backoff_seconds)
        self.max_retries = cfg.broker.max_retries
        self._client = None
        self._stream_thread: threading.Thread | None = None
        self._stream_stop = threading.Event()

    # ----------------------------------------------------------- connection
    @property
    def client(self):
        if self._client is None:
            from alpaca.trading.client import TradingClient

            self._client = TradingClient(
                self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key, paper=self.paper
            )
        return self._client

    def _call(self, fn, *args, **kwargs):
        """Every API call retries with backoff before giving up."""
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last = exc
                if attempt < self.max_retries:
                    wait = self.retry_backoff[min(attempt, len(self.retry_backoff) - 1)]
                    log.warning("API call %s failed (%s); retry in %ss", fn.__name__, exc, wait)
                    time.sleep(wait)
        raise last  # type: ignore[misc]

    def health_check(self) -> dict:
        acct = self._call(self.client.get_account)
        clock = self._call(self.client.get_clock)
        info = {
            "status": str(acct.status),
            "paper": self.paper,
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "market_open": bool(clock.is_open),
            "next_open": str(clock.next_open),
            "next_close": str(clock.next_close),
        }
        log.info("broker health check OK", extra={"ctx": info})
        return info

    def is_market_open(self) -> bool:
        return bool(self._call(self.client.get_clock).is_open)

    # ------------------------------------------------------------ portfolio
    def account_equity(self) -> tuple[float, float]:
        acct = self._call(self.client.get_account)
        return float(acct.equity), float(acct.cash)

    def positions(self) -> dict[str, dict]:
        out = {}
        for pos in self._call(self.client.get_all_positions):
            sym = pos.symbol
            out[sym] = {
                "qty": float(pos.qty),
                "avg_entry": float(pos.avg_entry_price),
                "market_value": float(pos.market_value),
                "unrealized_pl": float(pos.unrealized_pl),
            }
        return out

    def reconcile(self, tracked: dict[str, float]) -> dict:
        """Compare what we think we hold against what Alpaca says we hold."""
        actual = self.positions()
        report = {"matched": [], "qty_mismatch": [], "untracked_at_broker": [], "missing_at_broker": []}
        for sym, qty in tracked.items():
            broker_sym = sym.replace("/", "")  # Alpaca reports BTC/USD as BTCUSD
            a = actual.get(sym) or actual.get(broker_sym)
            if a is None:
                report["missing_at_broker"].append(sym)
            elif abs(a["qty"] - qty) > max(1e-6, abs(qty) * 0.001):
                report["qty_mismatch"].append({"symbol": sym, "tracked": qty, "broker": a["qty"]})
            else:
                report["matched"].append(sym)
        tracked_norm = {s.replace("/", "") for s in tracked} | set(tracked)
        for sym in actual:
            if sym not in tracked_norm:
                report["untracked_at_broker"].append(sym)
        if report["qty_mismatch"] or report["missing_at_broker"] or report["untracked_at_broker"]:
            log.warning("reconciliation differences", extra={"ctx": report})
        else:
            log.info("reconciliation clean: %d positions match", len(report["matched"]))
        return report

    # ---------------------------------------------------------------- orders
    def submit_entry(
        self,
        symbol: str,
        qty: float,
        limit_price: float,
        stop_loss: float,
        take_profit: float | None = None,
        signal_id: str = "",
    ):
        """Bracket limit entry for stocks; plain limit for crypto (soft stop).
        Refuses to send anything without a stop attached or tracked."""
        if stop_loss is None or stop_loss <= 0:
            raise ValueError("submit_entry without a stop is forbidden")
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        coid = (signal_id or f"rt-{int(time.time())}")[:48]
        if is_crypto(symbol):
            req = LimitOrderRequest(
                symbol=symbol, qty=round(qty, 6), side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC, limit_price=round(limit_price, 2),
                client_order_id=coid,
            )
        else:
            whole = int(qty)  # brackets need whole shares
            if whole < 1:
                log.info("entry for %s skipped: sized below 1 share", symbol)
                return None
            req = LimitOrderRequest(
                symbol=symbol, qty=whole, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, limit_price=round(limit_price, 2),
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
                take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)) if take_profit else None,
                client_order_id=coid,
            )
        order = self._call(self.client.submit_order, req)
        log.info(
            "entry submitted", extra={"ctx": {
                "signal_id": signal_id, "order_id": str(order.id), "symbol": symbol,
                "qty": float(order.qty or 0), "limit": limit_price, "stop": stop_loss,
            }},
        )
        return order

    def submit_exit(self, symbol: str, qty: float, limit_price: float | None = None, signal_id: str = ""):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        coid = (signal_id or f"rt-x-{int(time.time())}")[:48]
        tif = TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY
        if limit_price:
            req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                                    time_in_force=tif, limit_price=round(limit_price, 2),
                                    client_order_id=coid)
        else:
            req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                                     time_in_force=tif, client_order_id=coid)
        order = self._call(self.client.submit_order, req)
        log.info("exit submitted", extra={"ctx": {"signal_id": signal_id, "symbol": symbol, "qty": qty}})
        return order

    def execute_with_retry(self, submit_fn, symbol: str, get_price_fn, **kwargs):
        """Timed cancel/retry: place limit, wait, cancel and re-place with a
        wider offset; the last attempt crosses the spread aggressively."""
        cfg = self.cfg.broker
        offsets = [cfg.limit_offset_bps * (i + 1) for i in range(cfg.max_retries)]
        for attempt, offset in enumerate(offsets):
            price = get_price_fn() * (1 + offset / 10_000.0)
            order = submit_fn(symbol=symbol, limit_price=price, **kwargs)
            if order is None:
                return None
            deadline = time.time() + cfg.order_timeout_seconds
            while time.time() < deadline:
                state = self._call(self.client.get_order_by_id, order.id)
                if str(state.status) in ("OrderStatus.FILLED", "filled"):
                    return state
                if str(state.status) in ("OrderStatus.CANCELED", "canceled", "OrderStatus.REJECTED", "rejected"):
                    break
                time.sleep(2)
            try:
                self._call(self.client.cancel_order_by_id, order.id)
            except Exception:
                pass
            log.info("order timed out, retry %d/%d wider", attempt + 1, len(offsets))
        return None

    # ----------------------------------------------------------------- stops
    def open_stop_orders(self, symbol: str | None = None) -> list:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol] if symbol else None)
        orders = self._call(self.client.get_orders, req)
        return [o for o in orders if o.order_type and "stop" in str(o.order_type).lower()]

    def tighten_stop(self, symbol: str, new_stop: float) -> bool:
        """Raise the resting stop. Never loosens; returns True if modified."""
        from alpaca.trading.requests import ReplaceOrderRequest

        for order in self.open_stop_orders(symbol):
            current = float(order.stop_price or 0)
            if new_stop > current:
                self._call(
                    self.client.replace_order_by_id, order.id,
                    ReplaceOrderRequest(stop_price=round(new_stop, 2)),
                )
                log.info("stop tightened", extra={"ctx": {"symbol": symbol, "from": current, "to": new_stop}})
                return True
            log.info("stop NOT loosened for %s (%.2f -> %.2f refused)", symbol, current, new_stop)
        return False

    # ------------------------------------------------------------- cleanup
    def cancel_all(self) -> int:
        res = self._call(self.client.cancel_orders)
        log.info("cancelled %d open orders", len(res))
        return len(res)

    def close_all(self) -> None:
        """Flatten everything. Deliberately NOT called on graceful shutdown —
        shutdown leaves positions and their stops in place."""
        self._call(self.client.close_all_positions, cancel_orders=True)
        log.warning("close_all executed: portfolio flattened")

    # ------------------------------------------------------------ streaming
    def start_fill_stream(self, on_fill) -> None:
        """Trade-update stream in a daemon thread with reconnect backoff.
        on_fill(event_dict) is called for every fill/partial fill."""

        def runner():
            backoff = 1
            while not self._stream_stop.is_set():
                try:
                    from alpaca.trading.stream import TradingStream

                    stream = TradingStream(
                        self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key, paper=self.paper
                    )

                    async def handler(data):
                        try:
                            event = {
                                "event": str(data.event),
                                "symbol": data.order.symbol,
                                "filled_qty": float(data.order.filled_qty or 0),
                                "filled_avg_price": float(data.order.filled_avg_price or 0),
                                "client_order_id": data.order.client_order_id,
                                "status": str(data.order.status),
                            }
                            on_fill(event)
                        except Exception as exc:
                            log.error("fill handler error: %s", exc)

                    stream.subscribe_trade_updates(handler)
                    backoff = 1
                    stream.run()  # blocks until disconnect
                except Exception as exc:
                    log.warning("trade stream lost (%s); reconnect in %ss", exc, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)

        self._stream_thread = threading.Thread(target=runner, daemon=True, name="fill-stream")
        self._stream_thread.start()

    def stop_fill_stream(self) -> None:
        self._stream_stop.set()
