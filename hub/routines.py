"""Routines: repeating tasks the hub creates on schedule by itself.

"Scholar: every Monday at 09:00, find 3 new income ideas." The task is created
as if the boss had asked (so it starts right away). If the previous task from
the same routine isn't finished yet, that run is skipped so work never piles up.
Times use the hub's clock, or the time zone in HUB_TZ (e.g. Europe/London).
"""

import datetime as dt
import os
import re
import time

EVERY = {"day": "Every day", "weekday": "Weekdays", "week": "Every week", "hours": "Every few hours"}
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
AT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
ACTIVE = ("proposed", "open", "accepted", "in_progress", "blocked")


def tz():
    name = os.environ.get("HUB_TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:  # unknown zone or no tz database: fall back to the machine's clock
            pass
    return None


def now_dt(ts=None):
    zone = tz()
    ts = time.time() if ts is None else ts
    return dt.datetime.fromtimestamp(ts, zone) if zone else dt.datetime.fromtimestamp(ts)


def next_run(r, after):
    """The first time this routine is due strictly after `after` (a timestamp)."""
    if r["every"] == "hours":
        return after + max(1, int(r["hours"])) * 3600
    h, m = (int(x) for x in r["at"].split(":"))
    base = now_dt(after)
    for add in range(0, 9):
        day = base.date() + dt.timedelta(days=add)
        cand = base.replace(year=day.year, month=day.month, day=day.day, hour=h, minute=m, second=0, microsecond=0)
        if cand.timestamp() <= after:
            continue
        wd = cand.weekday()
        if r["every"] == "weekday" and wd > 4:
            continue
        if r["every"] == "week" and wd != int(r["weekday"]):
            continue
        return cand.timestamp()
    return after + 86400  # unreachable in practice


def describe(r):
    if r["every"] == "hours":
        return f"Every {r['hours']} hours" if int(r["hours"]) != 1 else "Every hour"
    if r["every"] == "week":
        return f"Every {DAYS[int(r['weekday'])]} at {r['at']}"
    return f"{EVERY[r['every']]} at {r['at']}"


class RoutinesMixin:
    """Mixed into core.Hub."""

    def routine_dict(self, r):
        d = {k: r[k] for k in ("id", "title", "details", "assignee", "channel", "priority", "every", "at",
                               "weekday", "hours", "next_run", "last_run", "last_task_id", "skipped", "created_at")}
        d["enabled"] = bool(r["enabled"])
        d["demo"] = bool(r["demo"])
        d["schedule"] = describe(r)
        return d

    def _routine(self, rid):
        from .core import HubError
        r = self.q1("SELECT * FROM routines WHERE id=?", (rid,))
        if r is None:
            raise HubError(404, f"no routine {rid}")
        return r

    def routines(self, member):
        if member["kind"] == "boss":
            rows = self.q("SELECT * FROM routines ORDER BY enabled DESC, next_run")
        else:
            rows = self.q("SELECT * FROM routines WHERE assignee=? ORDER BY enabled DESC, next_run", (member["id"],))
        return [self.routine_dict(r) for r in rows]

    def _clean_routine(self, f):
        from .core import HubError
        from .messages import MAX_TITLE, PRIORITIES
        title = (f.get("title") or "").strip()
        if not title or len(title) > MAX_TITLE:
            raise HubError(400, f"a routine needs a title (at most {MAX_TITLE} characters)")
        details = (f.get("details") or "").strip()
        if len(details) > self.settings["max_text"]:
            raise HubError(400, "details are too long")
        assignee = self.member((f.get("assignee") or "").lower())
        if assignee is None or assignee["kind"] != "agent":
            raise HubError(400, "a routine must be assigned to an agent")
        ch = self.channel(f.get("channel") or "tasks")
        if ch is None:
            raise HubError(400, f"no channel #{f.get('channel')}")
        if not self.can_see(assignee, ch):
            raise HubError(400, f"@{assignee['id']} is not in #{ch['name']}")
        every = f.get("every") or "day"
        if every not in EVERY:
            raise HubError(400, f"'every' must be one of: {', '.join(EVERY)}")
        at = (f.get("at") or "09:00").strip()
        if not AT_RE.match(at):
            raise HubError(400, "'at' must be a time like 09:00")
        at = "%02d:%s" % (int(at.split(":")[0]), at.split(":")[1])
        try:
            weekday, hours = int(f.get("weekday") or 0), int(f.get("hours") or 24)
        except (TypeError, ValueError):
            raise HubError(400, "'weekday' and 'hours' must be numbers") from None
        if not 0 <= weekday <= 6:
            raise HubError(400, "'weekday' is 0 (Monday) to 6 (Sunday)")
        if not 1 <= hours <= 168:
            raise HubError(400, "'hours' must be between 1 and 168")
        priority = f.get("priority") or "normal"
        if priority not in PRIORITIES:
            raise HubError(400, f"'priority' must be one of: {', '.join(PRIORITIES)}")
        return {"title": title, "details": details or title, "assignee": assignee["id"], "channel": ch["name"],
                "priority": priority, "every": every, "at": at, "weekday": weekday, "hours": hours}

    def create_routine(self, fields, demo=False):
        r = self._clean_routine(fields)
        now = time.time()
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO routines(title, details, assignee, channel, priority, every, at, weekday, hours, "
                "next_run, created_at, demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["title"], r["details"], r["assignee"], r["channel"], r["priority"], r["every"], r["at"],
                 r["weekday"], r["hours"], next_run(r, now), now, int(demo)))
            self._event(c, "routine", {"id": cur.lastrowid}, member="boss")
        return self.routine_dict(self._routine(cur.lastrowid))

    def update_routine(self, rid, fields):
        old = self._routine(rid)
        merged = {k: old[k] for k in old.keys()}
        merged.update({k: v for k, v in fields.items() if v is not None})
        r = self._clean_routine(merged)
        enabled = int(bool(merged["enabled"]))
        with self.tx() as c:
            c.execute("UPDATE routines SET title=?, details=?, assignee=?, channel=?, priority=?, every=?, at=?, "
                      "weekday=?, hours=?, enabled=?, next_run=? WHERE id=?",
                      (r["title"], r["details"], r["assignee"], r["channel"], r["priority"], r["every"], r["at"],
                       r["weekday"], r["hours"], enabled, next_run(r, time.time()), rid))
            self._event(c, "routine", {"id": rid}, member="boss")
        return self.routine_dict(self._routine(rid))

    def delete_routine(self, rid):
        self._routine(rid)
        with self.tx() as c:
            c.execute("DELETE FROM routines WHERE id=?", (rid,))
            self._event(c, "routine", {"id": rid, "deleted": True}, member="boss")

    def run_routine(self, rid, now=None, force=False):
        """Create this routine's task now. Returns the task, or None if the last one isn't finished."""
        r = self._routine(rid)
        now = time.time() if now is None else now
        last = self._task(r["last_task_id"]) if r["last_task_id"] else None
        if last is not None and last["status"] in ACTIVE and not force:
            with self.tx() as c:
                c.execute("UPDATE routines SET skipped=skipped+1, next_run=? WHERE id=?", (next_run(r, now), rid))
            return None
        msg = self.post_message(self.member("boss"), r["channel"], "task_request", data={
            "title": r["title"], "details": r["details"], "assignee": r["assignee"], "priority": r["priority"],
        }, enforce=False, demo=bool(r["demo"]), routine_id=rid)
        with self.tx() as c:
            c.execute("UPDATE routines SET last_run=?, last_task_id=?, next_run=? WHERE id=?",
                      (now, msg["task_id"], next_run(r, now), rid))
            self._event(c, "routine", {"id": rid}, member="boss")
        return msg["task"]

    def run_due_routines(self, now=None):
        """Called by the scheduler. Runs each enabled routine that is due (once, even if runs were missed)."""
        now = time.time() if now is None else now
        made = []
        for r in self.q("SELECT id FROM routines WHERE enabled=1 AND next_run<=?", (now,)):
            try:
                t = self.run_routine(r["id"], now)
            except Exception as e:  # one broken routine must not stop the others
                import sys
                sys.stderr.write(f"hub: routine {r['id']} failed: {e}\n")
                with self.tx() as c:  # try again in an hour instead of every few seconds
                    c.execute("UPDATE routines SET next_run=? WHERE id=?", (now + 3600, r["id"]))
                continue
            if t is not None:
                made.append(t)
        return made
