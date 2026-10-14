"""Cloud budgets."""

import pytest

from hub.core import HubError


def test_no_budget_means_local_only(hub):
    s = hub.budget_check(hub.member("scholar"), 0.01)
    assert s["allowed"] is False and "no cloud budget" in s["reason"]


def test_budget_flow_and_warnings(hub):
    hub.set_budget("scholar", 1.00)
    scholar = hub.member("scholar")
    assert hub.budget_check(scholar, 0.5)["allowed"]
    hub.report_usage(scholar, "claude-sonnet-5-5", 1000, 500, 0.79)
    assert hub.inbox(hub.member("boss"), unread_only=True) == []
    hub.report_usage(hub.member("scholar"), "claude-sonnet-5-5", 1000, 500, 0.02)  # crosses 80%
    notes = [i["note"] for i in hub.inbox(hub.member("boss"), unread_only=True)]
    assert any("80%" in n for n in notes)
    st = hub.budget_status(hub.member("scholar"))
    assert st["spent"] == 0.81 and round(st["remaining"], 2) == 0.19
    assert not hub.budget_check(hub.member("scholar"), 0.5)["allowed"]
    hub.report_usage(hub.member("scholar"), "claude-sonnet-5-5", 0, 0, 0.30)  # crosses 100%
    assert any("all of" in i["note"] for i in hub.inbox(hub.member("boss"), unread_only=True))
    assert not hub.budget_status(hub.member("scholar"))["allowed"]


def test_family_cap_limits_everyone(hub):
    hub.set_budget("scholar", 5)
    hub.set_budget("amo", 5)
    hub.set_budget(None, 1.0)
    hub.report_usage(hub.member("amo"), "claude-haiku-5-5", 0, 0, 0.95)
    s = hub.budget_check(hub.member("scholar"), 0.10)
    assert not s["allowed"] and s["remaining"] == pytest.approx(0.05)


def test_paused_agent_cannot_spend_and_validation(hub):
    hub.set_budget("baby", 2)
    hub.set_agent_paused("baby", True)
    assert not hub.budget_check(hub.member("baby"), 0.01)["allowed"]
    with pytest.raises(HubError):
        hub.set_budget("boss", 5)
    with pytest.raises(HubError):
        hub.set_budget("baby", -1)
    with pytest.raises(HubError):
        hub.report_usage(hub.member("amo"), "x", 0, 0, 1000)


def test_budget_api(api, tokens, hub):
    assert api("POST", "/api/admin/budget", {"member": "scholar", "usd": 3}, tokens["amo"])[0] == 403
    s, r = api("POST", "/api/admin/budget", {"member": "scholar", "usd": 3}, tokens["boss"])
    assert s == 200 and next(a for a in r["agents"] if a["member"] == "scholar")["budget"] == 3
    s, c = api("POST", "/api/budget/check", {"estimate_usd": 0.05}, tokens["scholar"])
    assert c["allowed"]
    s, u = api("POST", "/api/budget/usage", {"model": "claude-haiku-5-5", "input_tokens": 100,
                                             "output_tokens": 50, "cost_usd": 0.001}, tokens["scholar"])
    assert u["spent"] == 0.001
    s, me = api("GET", "/api/budget", token=tokens["scholar"])
    assert me["budget"] == 3 and "agents" not in me
    s, summ = api("GET", "/api/summary", token=tokens["boss"])
    assert any(a["member"] == "scholar" and a["spent"] == 0.001 for a in summ["cloud"]["agents"])
