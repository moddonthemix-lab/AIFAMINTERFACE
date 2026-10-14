"""Backups and restore."""

import sqlite3

from hub import backup
from hub.core import Hub


def test_backup_keep_and_restore(hub, monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_BACKUP_KEEP", "2")
    hub.post_message(hub.member("boss"), "general", "post", "before the backup")
    first = hub.backup()
    assert first["name"].startswith("hub-") and hub.last_backup_age() < 60
    assert hub.backup_if_due() is None  # not due again yet
    hub.post_message(hub.member("boss"), "general", "post", "after the backup")
    # Make names unique (they are per second) and check only 2 are kept.
    import time
    for _ in range(2):
        time.sleep(1.05)
        hub.backup()
    assert len(hub.backups()) == 2

    # Restore the oldest kept one into a fresh home.
    oldest = hub.backup_dir() / hub.backups()[-1]["name"]
    home2 = tmp_path / "restored"
    h2 = Hub(home2)  # an existing hub there...
    h2.conn.close()  # ...that is stopped, as restore requires
    safety = backup.restore(home2, oldest)
    assert safety.exists()
    texts = [r[0] for r in sqlite3.connect(str(home2 / "hub.db")).execute("SELECT text FROM messages")]
    assert "after the backup" in texts


def test_backup_names_are_checked(hub):
    import pytest
    from hub.core import HubError
    with pytest.raises(HubError):
        hub.backup_path("../hub.db")


def test_backup_api(api, tokens, hub):
    assert api("POST", "/api/admin/backups", {}, tokens["amo"])[0] == 403
    s, b = api("POST", "/api/admin/backups", {}, tokens["boss"])
    assert s == 200
    s, lst = api("GET", "/api/admin/backups", token=tokens["boss"])
    assert lst["backups"][0]["name"] == b["name"]
    s, raw = api("GET", f"/api/admin/backups/{b['name']}", token=tokens["boss"])
    assert s == 200 and raw[:15] == b"SQLite format 3"
    assert api("GET", "/api/admin/backups/..%2Fhub.db", token=tokens["boss"])[0] in (400, 404)
