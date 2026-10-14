"""Proposed tasks and per-agent task autonomy."""

import pytest

from hub.core import HubError


def request(hub, who, assignee="builder", parent=None, title="Do a thing"):
    data = {"title": title, "details": "details", "assignee": assignee}
    if parent is not None:
        data["parent_task"] = parent
    return hub.post_message(hub.member(who), "tasks", "task_request", data=data)


def inbox_kinds(hub, who):
    return [i["kind"] for i in hub.inbox(hub.member(who), unread_only=True)]


def test_agent_task_waits_for_approval(hub):
    t = request(hub, "amo")["task"]
    assert t["status"] == "proposed"
    assert "approval" in inbox_kinds(hub, "boss")
    assert inbox_kinds(hub, "builder") == []  # not told yet
    # Nobody but the boss can move it.
    with pytest.raises(HubError) as e:
        hub.update_task(hub.member("builder"), t["id"], "accepted")
    assert e.value.status == 409
    hub.decide_task(hub.member("boss"), t["id"], approve=True)
    assert hub.task(hub.member("boss"), t["id"])["status"] == "open"
    assert inbox_kinds(hub, "builder") == ["assignment"]
    assert "approval" not in inbox_kinds(hub, "boss")  # cleared once decided
    hub.update_task(hub.member("builder"), t["id"], "accepted")


def test_boss_can_decline(hub):
    t = request(hub, "amo")["task"]
    hub.decide_task(hub.member("boss"), t["id"], approve=False, note="Not now.")
    assert hub.task(hub.member("boss"), t["id"])["status"] == "declined"
    assert inbox_kinds(hub, "builder") == []
    with pytest.raises(HubError):
        hub.decide_task(hub.member("boss"), t["id"], approve=True)  # no longer proposed


def test_boss_tasks_start_right_away(hub):
    assert request(hub, "boss")["task"]["status"] == "open"
    assert inbox_kinds(hub, "builder") == ["assignment"]


def test_steps_of_an_active_job_start_right_away(hub):
    job = request(hub, "boss", assignee="amo")["task"]
    step = request(hub, "amo", assignee="builder", parent=job["id"])["task"]
    assert step["status"] == "open"
    # ...but not when the parent isn't Amo's own active job.
    other = request(hub, "boss", assignee="scholar")["task"]
    assert request(hub, "amo", parent=other["id"], title="x2")["task"]["status"] == "proposed"
    hub.update_task(hub.member("boss"), job["id"], "done")
    assert request(hub, "amo", parent=job["id"], title="x3")["task"]["status"] == "proposed"


def test_free_and_strict_agents(hub):
    hub.update_member("scholar", task_autonomy="free")
    assert request(hub, "scholar")["task"]["status"] == "open"
    job = request(hub, "boss", assignee="baby")["task"]
    hub.update_member("baby", task_autonomy="strict")
    assert request(hub, "baby", parent=job["id"])["task"]["status"] == "proposed"
    with pytest.raises(HubError):
        hub.update_member("baby", task_autonomy="wild")


def test_approve_over_http_is_boss_only(api, tokens, hub):
    t = request(hub, "amo")["task"]
    assert api("POST", f"/api/tasks/{t['id']}/approve", {"approve": True}, tokens["amo"])[0] == 403
    s, r = api("POST", f"/api/tasks/{t['id']}/approve", {"approve": True}, tokens["boss"])
    assert s == 200 and r["task"]["status"] == "open"
    s, _ = api("POST", "/api/admin/members/amo", {"task_autonomy": "free"}, tokens["boss"])
    s, m = api("GET", "/api/members", token=tokens["amo"])
    assert {x["id"]: x for x in m["members"]}["amo"]["task_autonomy"] == "free"
    s, b = api("GET", "/api/brief", token=tokens["boss"])
    assert "approve" in b
