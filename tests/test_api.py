"""The API: tokens, every message type, privacy, tasks, inbox, events, search, brief."""

import threading


def post(api, token, channel, body):
    return api("POST", f"/api/channels/{channel}/messages", body, token)


def test_health_is_public_but_everything_else_needs_a_token(api):
    assert api("GET", "/api/health")[0] == 200
    assert api("GET", "/api/me")[0] == 401
    assert api("GET", "/api/me", token="hub_fake_token")[0] == 401
    assert api("GET", "/")[0] == 200


def test_me_and_members(api, tokens):
    s, me = api("GET", "/api/me", token=tokens["builder"])
    assert s == 200 and me["member"]["id"] == "builder"
    assert "scholar+baby" not in me["channels"]
    s, r = api("GET", "/api/members", token=tokens["builder"])
    ids = {m["id"] for m in r["members"]}
    assert {"amo", "scholar", "builder", "baby", "butler", "boss"} <= ids
    builder = next(m for m in r["members"] if m["id"] == "builder")
    assert builder["status"] == "online"


def test_every_message_type(api, tokens):
    b, a = tokens["builder"], tokens["amo"]
    s, m = post(api, b, "general", {"type": "post", "text": "hello family"})
    assert s == 200 and m["author"] == "builder"
    s, q = post(api, a, "technical-help", {"type": "question", "text": "@builder can you check this?",
                                           "data": {"to": "builder"}})
    assert s == 200 and q["data"]["to"] == "builder"
    s, t = post(api, a, "tasks", {"type": "task_request", "data": {
        "title": "Fix it", "details": "Please fix the thing", "assignee": "builder", "priority": "high"}})
    assert s == 200 and t["task"]["status"] == "open"
    tid = t["task"]["id"]
    s, u = post(api, b, "tasks", {"type": "task_update", "data": {"task_id": tid, "status": "in_progress",
                                                                 "note": "on it"}})
    assert s == 200 and u["task"]["status"] == "in_progress" and u["reply_to"] == t["id"]
    s, r = post(api, b, "tasks", {"type": "result", "data": {"task_id": tid, "summary": "fixed",
                                                            "artifacts": ["commit abc123"]}})
    assert s == 200 and r["task"]["status"] == "done"
    assert r["task"]["result"]["artifacts"] == ["commit abc123"]
    s, f = post(api, tokens["scholar"], "discoveries", {"type": "finding", "data": {
        "claim": "Water is wet", "status": "verified", "confidence": 0.9, "sources": ["https://example.org"]}})
    assert s == 200 and f["data"]["status"] == "verified"
    s, v = post(api, tokens["butler"], "tasks", {"type": "review", "text": "Nice work",
                                                "data": {"target": f"task:{tid}", "verdict": "praise"}})
    assert s == 200 and v["data"]["target"] == f"task:{tid}"


def test_top_level_fields_work_for_curl(api, tokens):
    s, m = post(api, tokens["scholar"], "discoveries",
                {"type": "finding", "claim": "x is y", "status": "reported", "confidence": 0.5})
    assert s == 200 and m["data"]["claim"] == "x is y"


def test_cannot_post_as_someone_else(api, tokens):
    s, m = post(api, tokens["builder"], "general", {"type": "post", "text": "I am amo", "author": "amo"})
    assert s == 200 and m["author"] == "builder"


def test_system_and_review_permissions(api, tokens):
    assert post(api, tokens["builder"], "general", {"type": "system", "text": "fake"})[0] == 400
    s, _ = post(api, tokens["builder"], "general", {"type": "post", "text": "hi"})
    s, r = post(api, tokens["builder"], "general", {"type": "review", "text": "great",
                                                    "data": {"target": 1, "verdict": "praise"}})
    assert s == 403


def test_validation_errors(api, tokens):
    b = tokens["builder"]
    assert post(api, b, "discoveries", {"type": "finding", "data": {
        "claim": "x", "status": "verified", "confidence": 0.9, "sources": []}})[0] == 400  # verified needs a source
    assert post(api, b, "discoveries", {"type": "finding", "data": {
        "claim": "x", "status": "maybe", "confidence": 0.9}})[0] == 400
    assert post(api, b, "discoveries", {"type": "finding", "data": {
        "claim": "x", "status": "reported", "confidence": 3}})[0] == 400
    assert post(api, b, "tasks", {"type": "task_request", "data": {"title": "t"}})[0] == 400
    assert post(api, b, "tasks", {"type": "task_request", "data": {
        "title": "t", "details": "d", "assignee": "nobody"}})[0] == 400
    assert post(api, b, "general", {"type": "shout", "text": "x"})[0] == 400
    assert post(api, b, "general", {"type": "post", "text": "x" * 5000})[0] == 400
    assert post(api, b, "nowhere", {"type": "post", "text": "x"})[0] == 404


def test_task_permissions(api, tokens):
    s, t = post(api, tokens["amo"], "tasks", {"type": "task_request", "data": {
        "title": "T", "details": "D", "assignee": "builder"}})
    tid = t["task"]["id"]
    # Someone else can't move Builder's task.
    assert api("POST", f"/api/tasks/{tid}/update", {"status": "done"}, tokens["scholar"])[0] == 403
    # Blocked needs a reason.
    assert api("POST", f"/api/tasks/{tid}/update", {"status": "blocked"}, tokens["builder"])[0] == 400
    s, u = api("POST", f"/api/tasks/{tid}/update", {"status": "blocked", "note": "need a key"}, tokens["builder"])
    assert s == 200 and u["task"]["status"] == "blocked"
    # The boss can move anything, including back to open.
    s, u = api("POST", f"/api/tasks/{tid}/update", {"status": "open"}, tokens["boss"])
    assert s == 200 and u["task"]["status"] == "open"
    s, r = api("GET", "/api/tasks?assignee=builder&status=open", token=tokens["boss"])
    assert [x["id"] for x in r["tasks"]] == [tid]
    s, full = api("GET", f"/api/tasks/{tid}", token=tokens["amo"])
    assert len(full["updates"]) == 2


def test_private_room_is_private(api, tokens):
    s, m = post(api, tokens["scholar"], "scholar+baby", {"type": "post", "text": "secret study notes"})
    assert s == 200
    b = tokens["builder"]
    assert api("GET", "/api/channels/scholar+baby/messages", token=b)[0] == 404
    assert post(api, b, "scholar+baby", {"type": "post", "text": "let me in"})[0] == 404
    assert api("GET", f"/api/threads/{m['id']}", token=b)[0] == 404
    s, r = api("GET", "/api/channels", token=b)
    assert "scholar+baby" not in {c["name"] for c in r["channels"]}
    s, r = api("GET", "/api/search?q=secret", token=b)
    assert r["messages"] == []
    s, r = api("GET", "/api/search?q=secret", token=tokens["baby"])
    assert len(r["messages"]) == 1
    s, r = api("GET", "/api/search?q=secret", token=tokens["boss"])
    assert len(r["messages"]) == 1
    # Events from the room don't reach outsiders.
    s, ev = api("GET", "/api/events?after=0", token=b)
    assert all(e["channel"] != "scholar+baby" for e in ev["events"])


def test_threads_are_one_level_deep(api, tokens):
    s, q = post(api, tokens["amo"], "technical-help", {"type": "question", "text": "how?"})
    s, r1 = post(api, tokens["builder"], "technical-help", {"type": "post", "text": "like this", "reply_to": q["id"]})
    s, r2 = post(api, tokens["amo"], "technical-help", {"type": "post", "text": "thanks", "reply_to": r1["id"]})
    assert r2["reply_to"] == q["id"]
    s, t = api("GET", f"/api/threads/{r1['id']}", token=tokens["amo"])
    assert t["root"]["id"] == q["id"] and len(t["replies"]) == 2
    s, top = api("GET", "/api/channels/technical-help/messages?top=1", token=tokens["amo"])
    assert [m["id"] for m in top["messages"]] == [q["id"]] and top["messages"][0]["reply_count"] == 2


def test_inbox_mentions_assignments_and_replies(api, tokens):
    post(api, tokens["amo"], "general", {"type": "question", "text": "@scholar what do you think?"})
    post(api, tokens["amo"], "tasks", {"type": "task_request", "data": {
        "title": "T", "details": "D", "assignee": "scholar"}})
    s, inbox = api("GET", "/api/inbox?unread=1", token=tokens["scholar"])
    assert {i["kind"] for i in inbox["items"]} == {"mention", "assignment"} and inbox["unread"] == 2
    s, r = api("POST", "/api/inbox/read", {"all": True}, tokens["scholar"])
    assert r["unread"] == 0


def test_long_poll_wakes_up_on_new_message(api, tokens):
    s, start = api("GET", "/api/events", token=tokens["builder"])
    result = {}

    def wait():
        result["r"] = api("GET", f"/api/events?after={start['last']}&wait=10", token=tokens["builder"], timeout=15)

    t = threading.Thread(target=wait)
    t.start()
    post(api, tokens["amo"], "general", {"type": "post", "text": "ping"})
    t.join(5)
    assert not t.is_alive()
    s, r = result["r"]
    assert any(e["kind"] == "message" and e["data"]["text"] == "ping" for e in r["events"])


def test_brief_is_small(api, tokens, hub):
    for i in range(8):
        hub.post_message(hub.member("boss"), "general", "post", "x" * 3000 + str(i))
    for ch in ("technical-help", "business", "discoveries", "tasks", "scholar+baby"):
        hub.post_message(hub.member("boss"), ch, "post", "🎉" * 1000)
    s, body = api("GET", "/api/brief", token=tokens["boss"])
    assert s == 200
    import json
    assert len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()) < 1024


def test_admin_is_boss_only(api, tokens):
    assert api("POST", "/api/admin/agents", {"id": "scout", "name": "Scout"}, tokens["amo"])[0] == 403
    s, r = api("POST", "/api/admin/agents", {"id": "scout", "name": "Scout", "emoji": "🧭"}, tokens["boss"])
    assert s == 200 and r["token"].startswith("hub_scout_")
    assert api("GET", "/api/me", token=r["token"])[1]["member"]["id"] == "scout"
    s, r2 = api("POST", "/api/admin/channels", {"name": "lab", "private": True, "members": ["scout"]}, tokens["boss"])
    assert s == 200
    assert api("GET", "/api/channels/lab/messages", token=tokens["amo"])[0] == 404
    assert api("GET", "/api/channels/lab/messages", token=r["token"])[0] == 200
    # New token replaces the old one.
    s, r3 = api("POST", "/api/admin/agents/scout/token", {}, tokens["boss"])
    assert api("GET", "/api/me", token=r["token"])[0] == 401
    assert api("GET", "/api/me", token=r3["token"])[0] == 200
    assert api("DELETE", "/api/admin/agents/scout", None, tokens["boss"])[0] == 200
    assert api("GET", "/api/me", token=r3["token"])[0] == 401
