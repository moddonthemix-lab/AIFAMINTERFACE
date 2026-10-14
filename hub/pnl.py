"""Profit and loss (P&L): the daily numbers behind Trader's journal and its chart.

An agent records one row per day and mode ("paper" for pretend money, "live"
for real money): that day's P&L, optionally the account value and the number of
trades. The #trades channel shows the running total as a chart.
"""

import re
import time

MODES = ("paper", "live")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LIMIT = 1_000_000_000


class PnlMixin:
    """Mixed into core.Hub."""

    def report_pnl(self, member, pnl_usd, day=None, mode="paper", equity_usd=None, trades=0, note="",
                   for_member=None):
        from .core import HubError
        from .routines import now_dt
        who = member
        if for_member and for_member != member["id"]:
            if member["kind"] != "boss":
                raise HubError(403, "only the boss can record P&L for someone else")
            who = self.member(for_member)
            if who is None:
                raise HubError(404, f"no member '{for_member}'")
        if mode not in MODES:
            raise HubError(400, "'mode' must be 'paper' (pretend money) or 'live' (real money)")
        day = day or now_dt().strftime("%Y-%m-%d")
        if not DAY_RE.match(str(day)):
            raise HubError(400, "'day' must look like 2026-10-14")
        try:
            pnl = round(float(pnl_usd), 2)
            equity = None if equity_usd in (None, "") else round(float(equity_usd), 2)
            trades = int(trades or 0)
        except (TypeError, ValueError):
            raise HubError(400, "P&L, account value and trades must be numbers") from None
        if abs(pnl) > LIMIT or (equity is not None and abs(equity) > LIMIT) or not 0 <= trades <= 100_000:
            raise HubError(400, "those numbers are out of range")
        with self.tx() as c:
            c.execute("INSERT INTO pnl(member, day, mode, pnl_usd, equity_usd, trades, note, updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(member, day, mode) DO UPDATE SET "
                      "pnl_usd=excluded.pnl_usd, equity_usd=excluded.equity_usd, trades=excluded.trades, "
                      "note=excluded.note, updated_at=excluded.updated_at",
                      (who["id"], day, mode, pnl, equity, trades, (note or "")[:500], time.time()))
            ch = "trades" if self.channel("trades") is not None else None
            self._event(c, "pnl", {"member": who["id"], "day": day, "mode": mode}, channel=ch,
                        member=None if ch else "boss")
        return self.pnl(member, who["id"], mode=mode)

    def pnl(self, member, of=None, mode=None, days=365):
        """Daily P&L rows (oldest first) and totals, for one member (default: whoever has data)."""
        if of is None:
            r = self.q1("SELECT member FROM pnl ORDER BY updated_at DESC LIMIT 1")
            of = r["member"] if r else "trader"
        modes = [m["mode"] for m in self.q("SELECT DISTINCT mode FROM pnl WHERE member=?", (of,))]
        if mode is None:
            mode = "live" if "live" in modes else "paper"
        days = max(1, min(int(days or 365), 3650))
        rows = self.q("SELECT * FROM (SELECT * FROM pnl WHERE member=? AND mode=? ORDER BY day DESC LIMIT ?) "
                      "ORDER BY day", (of, mode, days))
        running = 0.0
        if rows:  # the running total includes days before the window shown
            running = round(self.q1("SELECT COALESCE(SUM(pnl_usd), 0) FROM pnl WHERE member=? AND mode=? AND day<?",
                                    (of, mode, rows[0]["day"]))[0], 2)
        out = []
        for r in rows:
            running = round(running + r["pnl_usd"], 2)
            out.append({"day": r["day"], "pnl_usd": r["pnl_usd"], "total_usd": running, "equity_usd": r["equity_usd"],
                        "trades": r["trades"], "note": r["note"]})
        traded = [d for d in out if d["trades"] > 0 or d["pnl_usd"] != 0]
        wins = [d for d in traded if d["pnl_usd"] > 0]
        return {
            "member": of, "mode": mode, "modes": modes, "days": out,
            "totals": {
                "total_usd": running,
                "last_day": out[-1] if out else None,
                "days_reported": len(out),
                "days_traded": len(traded),
                "win_days": len(wins),
                "best_day": max(out, key=lambda d: d["pnl_usd"]) if out else None,
                "worst_day": min(out, key=lambda d: d["pnl_usd"]) if out else None,
            },
        }
