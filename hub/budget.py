"""Cloud budgets: how much each agent may spend on cloud AI per month.

The boss sets a monthly budget per agent (and optionally a family total) on the
Admin page. Before a cloud call an agent asks "may I spend about $X?"; after it,
the agent reports what it spent. The hub keeps the totals and says no once a
budget is used up, and tells the boss at 80% and 100%.

The API key lives on the agents' machine, so the hub can't physically stop a
misbehaving agent. That's why the boss should also set a spend limit in the
Anthropic Console. Well-behaved agents (family_brain.py) always ask first.
"""

import time

WARN_AT = 0.8


class BudgetMixin:
    """Mixed into core.Hub."""

    def month(self, ts=None):
        from .routines import now_dt
        return now_dt(ts).strftime("%Y-%m")

    def family_budget(self):
        r = self.q1("SELECT value FROM settings WHERE key='family_budget_cents'")
        return int(r["value"]) / 100 if r else 0.0

    def spent(self, member_id=None, month=None):
        month = month or self.month()
        if member_id is None:
            return round(self.q1("SELECT COALESCE(SUM(cost_usd), 0) FROM cloud_usage WHERE month=?", (month,))[0], 4)
        return round(self.q1("SELECT COALESCE(SUM(cost_usd), 0) FROM cloud_usage WHERE month=? AND member=?",
                             (month, member_id))[0], 4)

    def budget_status(self, member):
        """This agent's budget this month (for the boss: everyone's)."""
        month = self.month()
        fam_budget, fam_spent = self.family_budget(), self.spent(month=month)

        def one(m):
            budget = float(m["cloud_budget_usd"] or 0)
            spent = self.spent(m["id"], month)
            room = budget - spent
            if fam_budget > 0:
                room = min(room, fam_budget - fam_spent)
            return {"member": m["id"], "budget": budget, "spent": spent, "remaining": round(max(0.0, room), 4),
                    "allowed": budget > 0 and room > 0 and not m["paused"]}

        out = {"month": month, "family_budget": fam_budget, "family_spent": fam_spent}
        if member["kind"] == "boss":
            out["agents"] = [one(m) for m in self.q("SELECT * FROM members WHERE kind='agent' ORDER BY created_at")]
        else:
            out.update(one(member))
        return out

    def budget_check(self, member, estimate_usd):
        """May this agent spend about `estimate_usd` on a cloud call right now?"""
        from .core import HubError
        try:
            estimate = float(estimate_usd)
        except (TypeError, ValueError):
            raise HubError(400, "'estimate_usd' must be a number") from None
        s = self.budget_status(member)
        if member["kind"] == "boss":
            return {"allowed": True, "remaining": None, "reason": "the boss has no budget"}
        if s["budget"] <= 0:
            reason = "no cloud budget set for you; use your local model (the boss sets budgets on the Admin page)"
        elif member["paused"]:
            reason = "you are paused by the boss"
        elif estimate > s["remaining"]:
            reason = (f"not enough budget left this month (${s['remaining']:.2f} left, about ${estimate:.2f} needed); "
                      "use your local model")
        else:
            return {"allowed": True, "remaining": s["remaining"], "reason": "ok"}
        return {"allowed": False, "remaining": s["remaining"], "reason": reason}

    def report_usage(self, member, model, input_tokens=0, output_tokens=0, cost_usd=0.0, note=None):
        """Record what a cloud call cost. Tells the boss when the agent passes 80% and 100% of its budget."""
        from .core import HubError
        try:
            cost = float(cost_usd)
            tin, tout = int(input_tokens or 0), int(output_tokens or 0)
        except (TypeError, ValueError):
            raise HubError(400, "cost and token counts must be numbers") from None
        if not 0 <= cost <= 100 or tin < 0 or tout < 0:
            raise HubError(400, "cost must be between $0 and $100 and token counts can't be negative")
        month = self.month()
        before = self.spent(member["id"], month)
        with self.tx() as c:
            c.execute("INSERT INTO cloud_usage(member, model, input_tokens, output_tokens, cost_usd, month, note, "
                      "created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (member["id"], str(model or "?")[:60], tin, tout, cost, month, (note or "")[:200], time.time()))
            budget = float(member["cloud_budget_usd"] or 0)
            after = before + cost
            if budget > 0 and member["kind"] == "agent":
                for level, label in ((1.0, "has used all of"), (WARN_AT, "has used 80% of")):
                    if before < budget * level <= after:
                        self._inbox(c, "boss", "budget", note=f"{member['name']} {label} its ${budget:.2f} cloud "
                                                              f"budget for {month} (spent ${after:.2f})")
                        break
            self._event(c, "budget", {"member": member["id"]}, member="boss")
        return self.budget_status(member)

    def set_budget(self, member_id, usd):
        """Set an agent's monthly budget (member_id None: the family total; 0 = no family cap)."""
        from .core import HubError
        try:
            usd = round(float(usd), 2)
        except (TypeError, ValueError):
            raise HubError(400, "the budget must be a number of dollars") from None
        if not 0 <= usd <= 10000:
            raise HubError(400, "the budget must be between $0 and $10,000")
        with self.tx() as c:
            if member_id is None:
                c.execute("INSERT INTO settings(key, value) VALUES ('family_budget_cents', ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(int(round(usd * 100))),))
            else:
                m = self.member(member_id)
                if m is None or m["kind"] != "agent":
                    raise HubError(404, f"no agent '{member_id}'")
                c.execute("UPDATE members SET cloud_budget_usd=? WHERE id=?", (usd, member_id))
            self._event(c, "budget", {"member": member_id}, member="boss")

    def usage_log(self, limit=50):
        return [dict(r) for r in self.q("SELECT * FROM cloud_usage ORDER BY id DESC LIMIT ?", (limit,))]
