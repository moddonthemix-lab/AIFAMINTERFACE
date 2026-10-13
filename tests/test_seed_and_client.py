"""Demo data and the Python client."""

from hub import seed
from hub_client import HubClient, HubClientError


def test_demo_seed_and_wipe(hub):
    assert seed.demo(hub) is True
    assert seed.demo(hub) is False  # only once
    tasks = hub.tasks(hub.member("boss"))
    assert {t["title"] for t in tasks} == {"Test local file-sharing permissions",
                                           "Verify the Pro Tools workflow constraints"}
    assert any(t["status"] == "done" for t in tasks)
    reviews = hub.q("SELECT * FROM messages WHERE type='review'")
    assert reviews[0]["author"] == "butler"
    hub.post_message(hub.member("boss"), "general", "post", "my real message")
    seed.wipe_demo(hub)
    assert hub.q1("SELECT COUNT(*) FROM messages")[0] == 1
    assert hub.tasks(hub.member("boss")) == []


def test_client_end_to_end(server, tokens):
    amo = HubClient(tokens["amo"], server)
    builder = HubClient(tokens["builder"], server)
    assert builder.me()["member"]["id"] == "builder"
    start = builder.wait_for_events(wait=0)  # sets the starting point
    t = amo.request_task("tasks", "Build it", "Build the thing", "builder", priority="high")
    events = builder.wait_for_events(wait=5)
    assert any(e["kind"] == "task" for e in events)
    assert builder.inbox()[0]["kind"] == "assignment"
    builder.update_task(t["task"]["id"], "in_progress", "starting")
    r = builder.post_result(t["task"]["id"], "Built it", ["commit 1234"])
    assert r["task"]["status"] == "done"
    amo.ask("general", "Is it good?", to="builder")
    builder.post_finding("discoveries", "It is good", "verified", 0.9, ["https://example.org"])
    assert builder.search("good")
    assert builder.brief()["me"] == "builder"
    try:
        HubClient("bad", server).me()
        raise AssertionError("should fail")
    except HubClientError as e:
        assert e.status == 401
    assert start == [] or isinstance(start, list)
