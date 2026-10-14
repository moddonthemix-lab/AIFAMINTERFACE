"""👍 / 👎 feedback on messages."""

import pytest

from hub.core import HubError


def finding(hub, who="scholar", claim="Godot is free and open source."):
    return hub.post_message(hub.member(who), "discoveries", "finding",
                            data={"claim": claim, "status": "reported", "confidence": 0.6})


def test_boss_feedback_reaches_the_author(hub):
    f = finding(hub)
    boss = hub.member("boss")
    assert hub.react(boss, f["id"], 1) == {"up": 1, "down": 0, "boss": 1}
    item = hub.inbox(hub.member("scholar"), unread_only=True)[0]
    assert item["kind"] == "feedback" and "useful" in item["note"]
    hub.react(boss, f["id"], -1)
    fb = hub.feedback(hub.member("scholar"))
    assert [x["value"] for x in fb] == [-1] and fb[0]["message"]["id"] == f["id"]
    hub.react(boss, f["id"], 0)
    assert hub.feedback(hub.member("scholar")) == []
    assert hub.thread(boss, f["id"])["root"]["reactions"] == {"up": 0, "down": 0, "boss": 0}


def test_reaction_rules(hub):
    f = finding(hub)
    with pytest.raises(HubError):
        hub.react(hub.member("scholar"), f["id"], 1)    # own message
    with pytest.raises(HubError):
        hub.react(hub.member("boss"), f["id"], 5)       # bad value
    hub.react(hub.member("amo"), f["id"], 1)            # agents can react; no feedback inbox
    assert hub.inbox(hub.member("scholar"), unread_only=True) == []
    note = hub.post_message(hub.member("scholar"), "scholar+baby", "post", "secret")
    with pytest.raises(HubError) as e:
        hub.react(hub.member("builder"), note["id"], 1)   # can't see the room
    assert e.value.status == 404


def test_feedback_over_http_and_client(api, tokens, server, hub, tmp_path):
    from hub_client import HubClient
    f = finding(hub)
    s, r = api("POST", f"/api/messages/{f['id']}/react", {"value": 1}, tokens["boss"])
    assert s == 200 and r["boss"] == 1
    c = HubClient(tokens["scholar"], server, outbox=tmp_path / "o.jsonl")
    assert c.feedback()[0]["value"] == 1
