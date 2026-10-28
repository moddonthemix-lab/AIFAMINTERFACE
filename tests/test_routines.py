"""Routines: repeating tasks."""

import datetime as dt
import time

import pytest

from hub import routines
from hub.core import HubError


def ts(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi).timestamp()


def test_next_run_rules():
    mon_8 = ts(2026, 10, 12, 8, 0)  # a Monday
    r = {"every": "day", "at": "09:00", "weekday": 0, "hours": 24}
    assert routines.next_run(r, mon_8) == ts(2026, 10, 12, 9, 0)
    assert routines.next_run(r, ts(2026, 10, 12, 9, 0)) == ts(2026, 10, 13, 9, 0)
    fri_10 = ts(2026, 10, 16, 10, 0)
    assert routines.next_run(dict(r, every="weekday"), fri_10) == ts(2026, 10, 19, 9, 0)  # skips the weekend
    assert routines.next_run(dict(r, every="week", weekday=2), mon_8) == ts(2026, 10, 14, 9, 0)
    assert routines.next_run(dict(r, every="hours", hours=6), mon_8) == mon_8 + 6 * 3600
    assert routines.describe(dict(r, every="week", weekday=0)) == "Every Monday at 09:00"


def test_routine_creates_tasks_and_skips_when_unfinished(hub):
    r = hub.create_routine({"title": "Weekly ideas", "assignee": "scholar", "channel": "business",
                            "every": "day", "at": "09:00"})
    assert r["schedule"] == "Every day at 09:00" and r["next_run"] > time.time()
    later = r["next_run"] + 1
    made = hub.run_due_routines(later)
    assert len(made) == 1 and made[0]["status"] == "open" and made[0]["routine_id"] == r["id"]
    assert made[0]["requester"] == "boss" and made[0]["assignee"] == "scholar"
    assert [i["kind"] for i in hub.inbox(hub.member("scholar"))] == ["assignment"]
    # Next day: the first task isn't done, so the run is skipped.
    assert hub.run_due_routines(later + 86400) == []
    assert hub.routines(hub.member("boss"))[0]["skipped"] == 1
    hub.update_task(hub.member("scholar"), made[0]["id"], "done", "done")
    assert len(hub.run_due_routines(later + 2 * 86400)) == 1


def test_disabled_routines_and_manual_run(hub):
    r = hub.create_routine({"title": "Check", "assignee": "builder", "every": "hours", "hours": 2})
    hub.update_routine(r["id"], {"enabled": False})
    assert hub.run_due_routines(time.time() + 10 * 3600) == []
    assert hub.run_routine(r["id"], force=True)["title"] == "Check"


def test_routine_validation_and_privacy(hub):
    with pytest.raises(HubError):
        hub.create_routine({"title": "x", "assignee": "boss"})
    with pytest.raises(HubError):
        hub.create_routine({"title": "x", "assignee": "builder", "channel": "scholar+baby"})
    with pytest.raises(HubError):
        hub.create_routine({"title": "x", "assignee": "builder", "at": "25:00"})
    with pytest.raises(HubError):
        hub.create_routine({"title": "x", "assignee": "builder", "every": "month"})
    hub.create_routine({"title": "Mine", "assignee": "baby"})
    assert [x["title"] for x in hub.routines(hub.member("baby"))] == ["Mine"]
    assert [x["title"] for x in hub.routines(hub.member("amo"))] == ["Grade today's setups"]  # seeded, paused


def test_routines_api(api, tokens):
    assert api("POST", "/api/routines", {"title": "x", "assignee": "amo"}, tokens["amo"])[0] == 403
    s, r = api("POST", "/api/routines", {"title": "Daily check-in", "assignee": "amo", "every": "weekday",
                                         "at": "8:30"}, tokens["boss"])
    assert s == 200 and r["at"] == "08:30" and r["schedule"] == "Weekdays at 08:30"
    s, run = api("POST", f"/api/routines/{r['id']}/run", {}, tokens["boss"])
    assert run["task"]["title"] == "Daily check-in"
    s, lst = api("GET", "/api/routines", token=tokens["amo"])
    assert lst["routines"][0]["last_task_id"] == run["task"]["id"] and "hub_time" in lst
    assert api("DELETE", f"/api/routines/{r['id']}", None, tokens["boss"])[0] == 200


def test_then_step_hands_the_result_to_the_next_agent(hub):
    r = hub.create_routine({"title": "Tomorrow's top setups", "assignee": "amo", "channel": "trades",
                            "every": "weekday", "at": "19:00",
                            "then_assignee": "trader", "then_title": "Trade plan",
                            "then_details": "Trade these setups if they trigger and you can trade them."})
    assert r["then"]["assignee"] == "trader" and r["then"]["approval"] is False
    t = hub.run_routine(r["id"], force=True)
    amo = hub.member("amo")
    hub.update_task(amo, t["id"], "in_progress", "scanning")
    msg = hub.post_message(amo, "trades", "result", data={"task_id": t["id"],
                           "summary": "SETUP 1 symbol: AAPL pattern: 2-1-2 trigger: 182.40"})
    nxt = msg["followup_task"]
    assert nxt["assignee"] == "trader" and nxt["title"] == "Trade plan" and nxt["status"] == "open"
    assert "AAPL" in nxt["details"] and nxt["chain_from"] == t["id"] and nxt["routine_id"] == r["id"]
    assert hub.task(hub.member("boss"), t["id"])["followup_task_id"] == nxt["id"]
    assert "assignment" in [i["kind"] for i in hub.inbox(hub.member("trader"))]
    # Finishing the follow-up doesn't start another one, and the step happens only once.
    done = hub.post_message(hub.member("trader"), "trades", "result",
                            data={"task_id": nxt["id"], "summary": "Took AAPL, +1.2R"})
    assert "followup_task" not in done
    assert hub.chain_followup(t["id"]) is None


def test_then_step_can_wait_for_the_boss(hub):
    r = hub.create_routine({"title": "Setups", "assignee": "amo", "channel": "trades",
                            "then_assignee": "trader", "then_approval": True})
    t = hub.run_routine(r["id"], force=True)
    msg = hub.post_message(hub.member("amo"), "trades", "result", data={"task_id": t["id"], "summary": "list"})
    assert msg["followup_task"]["status"] == "proposed" and msg["followup_task"]["title"] == "Next: Setups"
    assert "approval" in [i["kind"] for i in hub.inbox(hub.member("boss"))]


def test_then_step_validation_and_editing(hub):
    with pytest.raises(HubError):
        hub.create_routine({"title": "x", "assignee": "amo", "then_assignee": "boss"})
    with pytest.raises(HubError):  # builder can't see the private room
        hub.create_routine({"title": "x", "assignee": "amo", "then_assignee": "builder",
                            "then_channel": "scholar+baby"})
    r = hub.create_routine({"title": "Setups", "assignee": "amo", "channel": "trades"})
    r = hub.update_routine(r["id"], {"then_assignee": "trader", "details": "New wording", "at": "18:30"})
    assert r["then"]["assignee"] == "trader" and r["details"] == "New wording" and r["at"] == "18:30"
    r = hub.update_routine(r["id"], {"then_assignee": ""})
    assert r["then"] is None
