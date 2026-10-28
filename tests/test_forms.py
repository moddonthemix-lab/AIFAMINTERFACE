"""Shared forms: definitions, checking, routines, the Amo ➜ Trader hand-off, and the brain."""

import json

import pytest

from hub.core import HubError

SETUP = {"symbol": "AAPL", "timeframe": "Daily", "pattern": "2-1-2 (inside bar)", "direction": "long",
         "trigger": "182.40", "stop": 179.8, "targets": "185.10, 187.50", "invalidation": None,
         "continuity": "M up, W up", "confidence": "high", "why": "Inside bar at weekly support"}


def test_builtin_trading_forms(hub):
    names = [f["name"] for f in hub.forms()]
    assert names == ["setup_scorecard", "trade_report", "trade_setup"]
    f = hub.form("trade_setup", with_schema=True)
    s = f["json_schema"]
    item = s["properties"]["items"]["items"]
    assert item["additionalProperties"] is False and set(item["required"]) == {x["name"] for x in f["fields"]}
    assert item["properties"]["direction"]["enum"] == ["long", "short"]
    assert "anyOf" in item["properties"]["invalidation"]  # optional = nullable


def test_form_on_a_message_is_checked_and_cleaned(hub):
    m = hub.post_message(hub.member("amo"), "trades", "post", data={"form": {"name": "trade_setup", "items": [SETUP]}})
    item = m["data"]["form"]["items"][0]
    assert item["trigger"] == 182.4 and item["targets"] == [185.1, 187.5] and item["timeframe"] == "daily"
    assert "AAPL" in m["text"]  # readable text made from the form
    bad = dict(SETUP, direction="sideways")
    with pytest.raises(HubError) as e:
        hub.post_message(hub.member("amo"), "trades", "post", data={"form": {"name": "trade_setup", "items": [bad]}})
    assert "direction" in e.value.message
    with pytest.raises(HubError):
        hub.post_message(hub.member("amo"), "trades", "post",
                         data={"form": {"name": "trade_setup", "items": [dict(SETUP, extra=1)]}})
    with pytest.raises(HubError):  # no setups needs a note
        hub.post_message(hub.member("amo"), "trades", "post", data={"form": {"name": "trade_setup", "items": []}})
    m = hub.post_message(hub.member("amo"), "trades", "post",
                         data={"form": {"name": "trade_setup", "items": [], "note": "NO SETUPS: chop"}})
    assert m["data"]["form"]["note"] == "NO SETUPS: chop"


def test_routine_requires_form_and_hands_it_to_trader(hub):
    r = hub.create_routine({"title": "Top setups for tomorrow", "assignee": "amo", "channel": "trades",
                            "form": "trade_setup", "then_assignee": "trader", "then_title": "Trade plan",
                            "then_form": "trade_report"})
    assert r["form"] == "trade_setup" and r["then"]["form"] == "trade_report"
    t = hub.run_routine(r["id"], force=True)
    assert t["expects_form"] == "trade_setup"
    amo = hub.member("amo")
    with pytest.raises(HubError) as e:  # plain text isn't enough for this task
        hub.post_message(amo, "trades", "result", data={"task_id": t["id"], "summary": "AAPL looks good"})
    assert "trade_setup" in e.value.message
    msg = hub.post_message(amo, "trades", "result",
                           data={"task_id": t["id"], "form": {"name": "trade_setup", "items": [SETUP]}})
    plan = msg["followup_task"]
    assert plan["assignee"] == "trader" and plan["expects_form"] == "trade_report"
    task_msg = hub.message(plan["message_id"])
    carried = json.loads(task_msg["data"])["form"]
    assert carried["items"][0]["symbol"] == "AAPL" and carried["items"][0]["trigger"] == 182.4
    assert hub.task_dict(hub._task(plan["id"]))["form"] == carried  # agents read it straight off the task
    # Trader answers with the trade report form.
    done = hub.post_message(hub.member("trader"), "trades", "result", data={"task_id": plan["id"], "form": {
        "name": "trade_report", "items": [{"symbol": "AAPL", "outcome": "taken", "entry": 182.4, "exit": 185.1,
                                           "pnl_usd": 26.5, "r_multiple": 1.0, "mode": "paper"}]}})
    assert done["task"]["status"] == "done" and done["data"]["form"]["items"][0]["outcome"] == "TAKEN"
    assert hub.task_dict(hub._task(plan["id"]))["result"]["form"]["name"] == "trade_report"


def test_seeded_trading_routines_ask_for_forms(hub):
    by_title = {r["title"]: r for r in hub.routines(hub.member("boss"))}
    assert by_title["Grade today's setups"]["form"] == "setup_scorecard"
    assert by_title["Daily trading report"]["form"] == "trade_report"


def test_boss_can_add_edit_and_delete_forms(api, tokens):
    form = {"name": "weekly_review", "title": "Weekly review", "list": False,
            "fields": [{"name": "wins", "type": "number", "required": True},
                       {"name": "mood", "type": "choice", "choices": "good, ok, bad"},
                       {"name": "lessons", "type": "list"}]}
    assert api("POST", "/api/admin/forms", form, tokens["amo"])[0] == 403
    s, f = api("POST", "/api/admin/forms", form, tokens["boss"])
    assert s == 200 and f["fields"][1]["choices"] == ["good", "ok", "bad"]
    s, f = api("GET", "/api/forms/weekly_review", token=tokens["scholar"])
    assert f["json_schema"]["properties"]["items"]["items"]["properties"]["lessons"]["anyOf"][0]["type"] == "array"
    assert api("POST", "/api/admin/forms", {"name": "Bad Name!", "fields": []}, tokens["boss"])[0] == 400
    assert api("DELETE", "/api/admin/forms/trade_setup", None, tokens["boss"])[0] == 400  # built-in
    assert api("DELETE", "/api/admin/forms/weekly_review", None, tokens["boss"])[0] == 200


def test_brain_fills_a_form_with_structured_output(server, tokens, hub, tmp_path):
    from types import SimpleNamespace as NS

    import family_brain as fb
    from hub_client import HubClient

    hub.set_budget("amo", 5)
    answer = json.dumps({"items": [SETUP], "note": ""})

    class Claude:
        def __init__(self):
            self.calls = []
            self.messages = NS(create=self.create)
            self.beta = NS(messages=NS(create=self.create))

        def create(self, **kw):
            self.calls.append(kw)
            return NS(model=kw["model"], stop_reason="end_turn", stop_details=None, content=[NS(type="text", text=answer)],
                      usage=NS(input_tokens=500, output_tokens=200, cache_creation_input_tokens=0,
                               cache_read_input_tokens=0, server_tool_use=None))

    c = HubClient(tokens["amo"], server, outbox=tmp_path / "o.jsonl")
    claude = Claude()
    brain = fb.FamilyBrain(c, system="You are Amo.", cloud_client=claude, internet_check=lambda: True)
    t = brain.fill_form("trade_setup", "Best setups for tomorrow from this scan: ...", job="research")
    call = claude.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema" and "tools" not in call
    assert t.form["name"] == "trade_setup" and t.form["items"][0]["symbol"] == "AAPL"
    # And the hub accepts what the brain produced.
    m = c.post("trades", "", form=t.form)
    assert m["data"]["form"]["items"][0]["trigger"] == 182.4
