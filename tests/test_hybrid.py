"""Hybrid features: local-only channels, the offline outbox, autostart files."""

import plistlib
import socket

from hub import autostart, db
from hub.core import Hub
from hub_client import HubClient, HubOffline


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- local-only channels
def test_private_rooms_are_local_only_by_default(api, tokens):
    s, r = api("GET", "/api/channels", token=tokens["scholar"])
    chans = {c["name"]: c for c in r["channels"]}
    assert chans["scholar+baby"]["local_only"] is True
    assert chans["general"]["local_only"] is False
    s, m = api("POST", "/api/channels/scholar+baby/messages", {"type": "question", "text": "private q?"},
               tokens["baby"])
    assert m["local_only"] is True
    s, m = api("POST", "/api/channels/general/messages", {"type": "question", "text": "public q?"}, tokens["baby"])
    assert m["local_only"] is False


def test_boss_switches_local_only(api, tokens):
    assert api("POST", "/api/admin/channels/business", {"local_only": True}, tokens["amo"])[0] == 403
    assert api("POST", "/api/admin/channels/business", {"local_only": True}, tokens["boss"])[0] == 200
    s, r = api("GET", "/api/channels", token=tokens["amo"])
    assert {c["name"]: c for c in r["channels"]}["business"]["local_only"] is True
    s, m = api("POST", "/api/channels/business/messages", {"type": "question", "text": "money?"}, tokens["amo"])
    assert m["local_only"] is True
    s, _ = api("POST", "/api/admin/channels", {"name": "open-room", "private": True, "members": ["amo"],
                                               "local_only": False}, tokens["boss"])
    s, r = api("GET", "/api/channels", token=tokens["boss"])
    assert {c["name"]: c for c in r["channels"]}["open-room"]["local_only"] is False


def test_old_database_gets_upgraded(tmp_path):
    # A database from before local_only existed: the private room becomes local-only on upgrade.
    conn = db.connect(tmp_path / "hub.db")
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO channels(name, purpose, private, created_at) VALUES ('room', '', 1, 0)")
    conn.execute("INSERT INTO channels(name, purpose, private, created_at) VALUES ('pub', '', 0, 0)")
    conn.close()
    hub = Hub(tmp_path)
    assert hub.channel("room")["local_only"] == 1 and hub.channel("pub")["local_only"] == 0


# ---------------------------------------------------------------- client_id: no double posts
def test_resending_the_same_client_id_does_not_post_twice(api, tokens, hub):
    body = {"type": "question", "text": "only once?", "client_id": "abc123"}
    s, first = api("POST", "/api/channels/general/messages", body, tokens["baby"])
    s, again = api("POST", "/api/channels/general/messages", body, tokens["baby"])
    assert s == 200 and again["id"] == first["id"] and again["already_received"] is True
    assert hub.q1("SELECT COUNT(*) FROM messages WHERE text='only once?'")[0] == 1


# ---------------------------------------------------------------- outbox
def test_outbox_queues_while_offline_and_sends_later(tmp_path, hub, tokens):
    from hub.server import make_server
    import threading

    port = free_port()
    box = tmp_path / "outbox.jsonl"
    client = HubClient(tokens["scholar"], f"http://127.0.0.1:{port}", timeout=2, outbox=box)

    # Hub is down: messages are queued, in order.
    r1 = client.ask("general", "first while offline?")
    r2 = client.post_finding("discoveries", "found offline", "reported", 0.5)
    assert r1["queued"] and r2["queued"] and client.outbox_size() == 2
    try:
        client.me()
        raise AssertionError("should be offline")
    except HubOffline:
        pass

    # Hub comes back: the next send flushes the queue first, keeping the order.
    srv = make_server(hub, "127.0.0.1", port)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    try:
        r3 = client.ask("general", "third, now online?")
        assert "id" in r3 and client.outbox_size() == 0
        texts = [r["text"] for r in hub.q("SELECT text FROM messages WHERE author='scholar' ORDER BY id")]
        assert texts == ["first while offline?", "found offline", "third, now online?"]
        # Flushing again sends nothing twice.
        assert client.flush_outbox() == {"sent": 0, "left": 0, "rejected": []}
    finally:
        srv.shutdown()
        srv.server_close()


def test_outbox_drops_messages_the_hub_refuses(tmp_path, server, tokens):
    box = tmp_path / "outbox.jsonl"
    client = HubClient(tokens["builder"], server, outbox=box)
    # Simulate a queued message that is no longer allowed (plain post outside a thread in #business).
    client._queue("/api/channels/business/messages", {"type": "post", "text": "chatter", "client_id": "x1"})
    client._queue("/api/channels/general/messages", {"type": "question", "text": "fine?", "client_id": "x2"})
    r = client.flush_outbox()
    assert r["sent"] == 1 and r["left"] == 0 and r["rejected"][0]["status"] == 422
    assert client.rejected


def test_outbox_can_be_turned_off(server, tokens):
    client = HubClient(tokens["builder"], "http://127.0.0.1:1", timeout=1, outbox=False)
    try:
        client.ask("general", "hello?")
        raise AssertionError("should raise")
    except HubOffline:
        pass


# ---------------------------------------------------------------- autostart
def test_autostart_files(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_HOME", str(tmp_path))
    env = autostart.settings("0.0.0.0")
    plist = plistlib.loads(autostart.launchd_plist(env).encode())
    assert plist["ProgramArguments"][1:] == ["-m", "hub", "serve"]
    assert plist["EnvironmentVariables"]["HUB_HOST"] == "0.0.0.0" and plist["KeepAlive"] is True
    unit = autostart.systemd_unit(env)
    assert "-m hub serve" in unit and 'Environment="HUB_HOST=0.0.0.0"' in unit and "Restart=always" in unit


# ---------------------------------------------------------------- brain tag
def test_brain_tag(api, tokens, server, tmp_path):
    s, m = api("POST", "/api/channels/general/messages",
               {"type": "question", "text": "local?", "data": {"brain": "local"}}, tokens["amo"])
    assert m["data"]["brain"] == "local"
    assert api("POST", "/api/channels/general/messages",
               {"type": "question", "text": "x?", "data": {"brain": "moon"}}, tokens["amo"])[0] == 400
    c = HubClient(tokens["scholar"], server, outbox=tmp_path / "o.jsonl")
    c.brain = "cloud"
    f = c.post_finding("discoveries", "cloud fact", "reported", 0.5)
    assert f["data"]["brain"] == "cloud"
