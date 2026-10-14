"""Family memory: agents propose, the boss decides, rooms stay private."""

import pytest

from hub import seed
from hub.core import HubError


def mem(hub, who, text, topic="facts", scope=None):
    return hub.propose_memory(hub.member(who), topic, text, scope)


def test_agent_proposal_waits_for_boss(hub):
    m = mem(hub, "scholar", "Water boils at 100 C at sea level.")
    assert m["status"] == "proposed"
    boss = hub.member("boss")
    assert [i["kind"] for i in hub.inbox(boss, unread_only=True)] == ["memory"]
    # Other agents don't see unapproved memories; the author does.
    assert hub.memories(hub.member("amo")) == []
    assert hub.memories(hub.member("scholar"), status="proposed")[0]["id"] == m["id"]
    assert hub.memories(hub.member("amo"), status="proposed") == []
    hub.decide_memory(boss, m["id"], approve=True, text="Water boils at 100 °C at sea level.")
    got = hub.memories(hub.member("amo"))
    assert got[0]["text"] == "Water boils at 100 °C at sea level." and got[0]["status"] == "active"
    assert hub.inbox(boss, unread_only=True) == []
    assert hub.inbox(hub.member("scholar"))[0]["memory"]["id"] == m["id"]


def test_boss_memories_go_live_and_can_be_edited_archived_deleted(hub):
    boss = hub.member("boss")
    m = mem(hub, "boss", "I like short answers.", topic="boss")
    assert m["status"] == "active"
    hub.update_memory(boss, m["id"], text="I like short answers with sources.")
    assert hub.memories(hub.member("baby"), topic="boss")[0]["text"] == "I like short answers with sources."
    hub.update_memory(boss, m["id"], status="archived")
    assert hub.memories(hub.member("baby")) == []
    hub.delete_memory(boss, m["id"])
    with pytest.raises(HubError):
        hub.update_memory(boss, m["id"], text="x")


def test_decline_and_duplicates_and_limits(hub):
    boss = hub.member("boss")
    m = mem(hub, "baby", "The moon is cheese.")
    hub.decide_memory(boss, m["id"], approve=False)
    assert hub.memories(boss, status="declined")[0]["id"] == m["id"]
    mem(hub, "boss", "We use SQLite.")
    with pytest.raises(HubError) as e:
        mem(hub, "baby", "we use sqlite.")
    assert e.value.status == 409
    with pytest.raises(HubError):
        mem(hub, "baby", "x", topic="gossip")
    with pytest.raises(HubError):
        mem(hub, "baby", "x" * 1001)
    hub.set_agent_paused("baby", True)
    with pytest.raises(HubError) as e:
        mem(hub, "baby", "Paused agents can't propose.")
    assert e.value.status == 403


def test_room_memories_stay_in_the_room(hub):
    boss = hub.member("boss")
    m = mem(hub, "boss", "Study plan: one topic per week.", scope="scholar+baby")
    assert [x["id"] for x in hub.memories(hub.member("baby"))] == [m["id"]]
    assert m["local_only"] is True  # private rooms are local-only
    assert hub.memories(hub.member("builder")) == []
    with pytest.raises(HubError) as e:
        mem(hub, "builder", "Sneaky.", scope="scholar+baby")
    assert e.value.status == 404


def test_memory_api_and_client(api, tokens, server, tmp_path):
    from hub_client import HubClient
    c = HubClient(tokens["scholar"], server, outbox=tmp_path / "o.jsonl")
    p = c.propose_memory("facts", "SQLite supports full-text search with FTS5.")
    assert p["status"] == "proposed"
    assert api("POST", f"/api/memory/{p['id']}/approve", {"approve": True}, tokens["scholar"])[0] == 403
    s, r = api("POST", f"/api/memory/{p['id']}/approve", {"approve": True}, tokens["boss"])
    assert s == 200 and r["status"] == "active"
    assert c.memories(q="FTS5")[0]["id"] == p["id"]
    s, r = api("GET", "/api/memory?status=wrong", token=tokens["amo"])
    assert s == 400
    s, b = api("GET", "/api/brief", token=tokens["boss"])
    assert b["approve"] == 0


def test_demo_memories_and_wipe(hub):
    seed.demo(hub)
    boss = hub.member("boss")
    assert len(hub.memories(boss)) == 3
    assert len(hub.memories(boss, status="proposed")) == 2
    assert hub.memories(hub.member("builder"), status="all") == [
        x for x in hub.memories(hub.member("builder"), status="all") if x["scope"] is None]
    seed.wipe_demo(hub)
    assert hub.memories(boss, status="all") == []
