"""Routines: repeating tasks the hub creates on schedule by itself.

"Scholar: every Monday at 09:00, find 3 new income ideas." The task is created
as if the boss had asked (so it starts right away). If the previous task from
the same routine isn't finished yet, that run is skipped so work never piles up.
Times use the hub's clock, or the time zone in HUB_TZ (e.g. Europe/London).
"""

import datetime as dt
import json
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


def when_text(ts):
    """'Thu Oct 29, 4:00 PM' in the hub's time zone."""
    d = now_dt(ts)
    return f"{d:%a %b} {d.day}, {d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


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
        d["form"] = r["form"]
        d["then"] = {"assignee": r["then_assignee"], "title": r["then_title"], "details": r["then_details"],
                     "channel": r["then_channel"], "approval": bool(r["then_approval"]),
                     "form": r["then_form"], "result_at": r["then_result_at"],
                     "cover": bool(r["then_cover"])} if r["then_assignee"] else None
        return d

    def chain_followup(self, tid):
        """A routine's task just finished: start its "then" step with the result. Returns the new task or None."""
        t = self._task(tid)
        if t is None or not t["routine_id"] or t["chain_from"] or t["followup_task_id"] or t["status"] != "done":
            return None
        r = self.q1("SELECT * FROM routines WHERE id=?", (t["routine_id"],))
        if r is None or not r["then_assignee"] or self.member(r["then_assignee"]) is None:
            return None
        files, carried_form = [], None
        if t["result_message_id"]:
            res = self.message(t["result_message_id"])
            text = res["text"]
            data = json.loads(res["data"] or "{}")
            extra = data.get("artifacts") or []
            if extra:
                text += "\nArtifacts: " + ", ".join(extra)
            if r["then_channel"] == t["channel"]:
                files = data.get("files") or []
            carried_form = data.get("form")
        else:
            last = self.q1("SELECT text FROM messages WHERE reply_to=? AND type='task_update' ORDER BY id DESC LIMIT 1",
                           (t["message_id"],))
            text = last["text"] if last and last["text"] else "(finished without a written result)"
        who = self.member(t["assignee"])["name"]
        details = f"{r['then_details']}\n\n--- From {who}'s result (task #{t['id']}) ---\n{text}"
        limit = self.settings["max_text"]
        if len(details) > limit:
            details = details[:limit - 40].rstrip() + "\n… (cut short; see task #%d)" % t["id"]
        data = {"title": r["then_title"], "details": details, "assignee": r["then_assignee"], "priority": r["priority"]}
        if files:
            data["files"] = files
        if carried_form:
            data["form"] = carried_form  # the next agent gets the exact data, not just the text
        if r["then_form"]:
            data["expects_form"] = r["then_form"]
        msg = self.post_message(self.member("boss"), r["then_channel"], "task_request", data=data, enforce=False,
                                routine_id=r["id"], chain_from=t["id"], proposed=bool(r["then_approval"]),
                                result_after=(next_run({"every": "weekday", "at": r["then_result_at"]}, time.time())
                                              if r["then_result_at"] else None),
                                must_cover=bool(r["then_cover"] and carried_form))
        with self.tx() as c:
            c.execute("UPDATE tasks SET followup_task_id=? WHERE id=?", (msg["task_id"], t["id"]))
        return msg["task"]

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
        out = {"title": title, "details": details or title, "assignee": assignee["id"], "channel": ch["name"],
               "priority": priority, "every": every, "at": at, "weekday": weekday, "hours": hours,
               "then_assignee": None, "then_title": None, "then_details": None, "then_channel": None,
               "then_approval": 0, "form": None, "then_form": None, "then_result_at": None, "then_cover": 0}
        if f.get("form"):
            out["form"] = self.form(f["form"])["name"]
        nxt = (f.get("then_assignee") or "").strip().lower()
        if nxt:
            who = self.member(nxt)
            if who is None or who["kind"] != "agent":
                raise HubError(400, "the 'then' step must go to an agent")
            then_ch = self.channel(f.get("then_channel") or ch["name"])
            if then_ch is None or not self.can_see(who, then_ch):
                raise HubError(400, f"@{who['id']} can't see the channel for the 'then' step")
            then_title = (f.get("then_title") or "").strip() or f"Next: {title}"
            then_details = (f.get("then_details") or "").strip()
            if len(then_title) > MAX_TITLE or len(then_details) > self.settings["max_text"]:
                raise HubError(400, "the 'then' step's title or details are too long")
            out.update(then_assignee=who["id"], then_title=then_title, then_details=then_details or then_title,
                       then_channel=then_ch["name"], then_approval=int(bool(f.get("then_approval"))),
                       then_form=self.form(f["then_form"])["name"] if f.get("then_form") else None)
            result_at = (f.get("then_result_at") or "").strip()
            if result_at and not AT_RE.match(result_at):
                raise HubError(400, "'then_result_at' must be a time like 16:00")
            out["then_result_at"] = result_at or None
            out["then_cover"] = int(bool(f.get("then_cover")))
            if out["then_cover"] and not out["then_form"]:
                raise HubError(400, "'answer every item' needs an answer form for the 'then' step")
        return out

    def create_routine(self, fields, demo=False):
        r = self._clean_routine(fields)
        now = time.time()
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO routines(title, details, assignee, channel, priority, every, at, weekday, hours, "
                "next_run, created_at, demo, then_assignee, then_title, then_details, then_channel, then_approval, "
                "form, then_form, then_result_at, then_cover) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["title"], r["details"], r["assignee"], r["channel"], r["priority"], r["every"], r["at"],
                 r["weekday"], r["hours"], next_run(r, now), now, int(demo), r["then_assignee"], r["then_title"],
                 r["then_details"], r["then_channel"], r["then_approval"], r["form"], r["then_form"],
                 r["then_result_at"], r["then_cover"]))
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
                      "weekday=?, hours=?, enabled=?, next_run=?, then_assignee=?, then_title=?, then_details=?, "
                      "then_channel=?, then_approval=?, form=?, then_form=?, then_result_at=?, then_cover=? WHERE id=?",
                      (r["title"], r["details"], r["assignee"], r["channel"], r["priority"], r["every"], r["at"],
                       r["weekday"], r["hours"], enabled, next_run(r, time.time()), r["then_assignee"],
                       r["then_title"], r["then_details"], r["then_channel"], r["then_approval"], r["form"],
                       r["then_form"], r["then_result_at"], r["then_cover"], rid))
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
            **({"expects_form": r["form"]} if r["form"] else {}),
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
