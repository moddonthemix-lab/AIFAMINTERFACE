"""The Today page summary."""

from hub import seed


def test_summary_after_demo(hub):
    seed.demo(hub)
    boss = hub.member("boss")
    s = hub.summary(boss, hours=24)
    assert s["counts"]["findings"] >= 3
    assert {t["title"] for t in s["waiting"]["approve_tasks"]} == {"Price a session-backup service for small studios"}
    assert len(s["waiting"]["approve_memories"]) == 2
    assert any(t["title"] == "Test local file-sharing permissions" for t in s["done"])
    assert s["findings"][0]["data"]["status"] == "verified"  # best first
    assert "scholar" in s["agents"]
    # Agents don't see the boss's approval queue or private rooms they're not in.
    b = hub.summary(hub.member("builder"))
    assert b["waiting"]["approve_tasks"] == [] and b["waiting"]["approve_memories"] == []
    assert all(m["channel"] != "scholar+baby" for m in b["findings"] + b["questions"])


def test_summary_api(api, tokens):
    s, r = api("GET", "/api/summary?hours=48", token=tokens["boss"])
    assert s == 200 and r["hours"] == 48 and "waiting" in r
