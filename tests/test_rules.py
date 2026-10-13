"""Anti-chatter rules."""

import pytest

from hub.core import HubError


def send(hub, who, channel, type_, text=None, data=None, reply_to=None):
    return hub.post_message(hub.member(who), channel, type_, text, data, reply_to)


def finding(n):
    return {"claim": f"fact number {n}", "status": "reported", "confidence": 0.5}


def test_rate_limit_per_minute(hub):
    hub.update_settings({"rate_per_minute": 3})
    for i in range(3):
        send(hub, "scholar", "discoveries", "finding", data=finding(i))
    with pytest.raises(HubError) as e:
        send(hub, "scholar", "discoveries", "finding", data=finding(99))
    assert e.value.status == 429 and e.value.rule == "rate_minute"
    # Logged, with one visible system note.
    assert hub.rule_log()[0]["rule"] == "rate_minute"
    notes = hub.q("SELECT * FROM messages WHERE type='system' AND channel='discoveries'")
    assert len(notes) == 1


def test_rate_limit_returns_429_over_http(api, tokens, hub):
    hub.update_settings({"rate_per_minute": 2})
    body = {"type": "question", "text": "q"}
    for i in range(2):
        assert api("POST", "/api/channels/general/messages", {**body, "text": f"q{i}"}, tokens["baby"])[0] == 200
    s, r = api("POST", "/api/channels/general/messages", {**body, "text": "q3"}, tokens["baby"])
    assert s == 429 and r["rule"] == "rate_minute"


def test_rate_limit_per_day(hub):
    hub.update_settings({"rate_per_day": 2})
    send(hub, "scholar", "discoveries", "finding", data=finding(1))
    send(hub, "scholar", "discoveries", "finding", data=finding(2))
    with pytest.raises(HubError) as e:
        send(hub, "scholar", "discoveries", "finding", data=finding(3))
    assert e.value.rule == "rate_day"


def test_boss_is_never_limited(hub):
    hub.update_settings({"rate_per_minute": 1, "rate_per_day": 1})
    for i in range(10):
        send(hub, "boss", "business", "post", f"boss message {i}")
    send(hub, "boss", "business", "post", "same")
    send(hub, "boss", "business", "post", "same")


def test_plain_posts_are_restricted(hub):
    with pytest.raises(HubError) as e:
        send(hub, "builder", "business", "post", "just chatting")
    assert e.value.status == 422
    # Replies to a question or task are always fine.
    q = send(hub, "amo", "business", "question", "what do you think?")
    send(hub, "builder", "business", "post", "I think yes", reply_to=q["id"])


def test_free_posts_per_day(hub):
    hub.update_settings({"free_posts_per_day": 2})
    send(hub, "baby", "general", "post", "hi 1")
    send(hub, "baby", "scholar+baby", "post", "hi 2")
    with pytest.raises(HubError) as e:
        send(hub, "baby", "general", "post", "hi 3")
    assert e.value.rule == "free_posts"


def test_duplicate_guard(hub):
    send(hub, "baby", "general", "question", "Why is the sky blue?")
    with pytest.raises(HubError) as e:
        send(hub, "baby", "general", "question", "Why is the sky blue?")
    assert e.value.status == 409


def test_length_limit(hub):
    hub.update_settings({"max_text": 100})
    with pytest.raises(HubError) as e:
        send(hub, "baby", "general", "question", "x" * 101)
    assert e.value.status == 400


def test_loop_breaker_pauses_thread_and_tells_boss(hub):
    hub.update_settings({"loop_limit": 4})
    q = send(hub, "amo", "technical-help", "question", "Should we use tabs or spaces?")
    send(hub, "builder", "technical-help", "post", "spaces", reply_to=q["id"])
    send(hub, "amo", "technical-help", "post", "tabs", reply_to=q["id"])
    send(hub, "builder", "technical-help", "post", "spaces!", reply_to=q["id"])  # 4th agent message → pause
    state = hub.q1("SELECT * FROM threads WHERE root_id=?", (q["id"],))
    assert state["paused"] == 1 and state["needs_boss"] == 1
    boss_inbox = hub.inbox(hub.member("boss"))
    assert boss_inbox[0]["kind"] == "needs_boss" and boss_inbox[0]["message"]["id"] == q["id"]
    with pytest.raises(HubError) as e:
        send(hub, "amo", "technical-help", "post", "TABS", reply_to=q["id"])
    assert e.value.status == 423
    # The boss steps in: that unpauses the thread.
    send(hub, "boss", "technical-help", "post", "Spaces. Done.", reply_to=q["id"])
    assert hub.q1("SELECT paused FROM threads WHERE root_id=?", (q["id"],))[0] == 0
    send(hub, "amo", "technical-help", "post", "ok boss", reply_to=q["id"])


def test_progress_resets_loop_counter(hub):
    hub.update_settings({"loop_limit": 3})
    q = send(hub, "amo", "technical-help", "question", "Does X work?")
    send(hub, "builder", "technical-help", "post", "checking", reply_to=q["id"])
    send(hub, "builder", "technical-help", "finding", data=finding(1), reply_to=q["id"])
    send(hub, "amo", "technical-help", "post", "great", reply_to=q["id"])
    send(hub, "builder", "technical-help", "post", "thanks", reply_to=q["id"])
    assert hub.q1("SELECT paused FROM threads WHERE root_id=?", (q["id"],)) is None


def test_one_agent_alone_is_not_a_loop(hub):
    hub.update_settings({"loop_limit": 3})
    q = send(hub, "amo", "technical-help", "question", "Anyone?")
    send(hub, "amo", "technical-help", "post", "bump 1", reply_to=q["id"])
    send(hub, "amo", "technical-help", "post", "bump 2", reply_to=q["id"])
    assert hub.q1("SELECT paused FROM threads WHERE root_id=?", (q["id"],)) is None


def test_paused_agent_cannot_post(hub):
    hub.set_agent_paused("baby", True)
    with pytest.raises(HubError) as e:
        send(hub, "baby", "general", "question", "can I talk?")
    assert e.value.status == 403
    hub.set_agent_paused("baby", False)
    send(hub, "baby", "general", "question", "can I talk?")


def test_boss_can_pause_and_unpause_a_thread(api, tokens, hub):
    q = send(hub, "amo", "general", "question", "Thoughts?")
    assert api("POST", "/api/admin/pause", {"thread": q["id"], "paused": True}, tokens["boss"])[0] == 200
    s, r = api("POST", "/api/channels/general/messages", {"type": "post", "text": "mine", "reply_to": q["id"]},
               tokens["builder"])
    assert s == 423
    api("POST", "/api/admin/pause", {"thread": q["id"], "paused": False}, tokens["boss"])
    s, r = api("POST", "/api/channels/general/messages", {"type": "post", "text": "mine", "reply_to": q["id"]},
               tokens["builder"])
    assert s == 200
