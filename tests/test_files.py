"""File attachments."""

import pytest

from hub.core import HubError
from hub_client import HubClient

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


def test_upload_attach_and_privacy(hub):
    scholar, builder, boss = hub.member("scholar"), hub.member("builder"), hub.member("boss")
    f = hub.upload_file(scholar, "discoveries", "../../etc/report<1>.pdf", "application/pdf", b"%PDF-1.4 hello")
    assert f["name"] == "report_1_.pdf" and f["size"] == 14 and not f["preview"]
    m = hub.post_message(scholar, "discoveries", "finding", data={
        "claim": "See report", "status": "reported", "confidence": 0.5, "files": [f["id"]]})
    assert m["attachments"][0]["id"] == f["id"]
    assert hub.read_file(builder, f["id"])[1] == b"%PDF-1.4 hello"
    # Someone else's file, or a file from another channel, can't be attached.
    with pytest.raises(HubError):
        hub.post_message(builder, "discoveries", "finding", data={
            "claim": "Mine now", "status": "reported", "confidence": 0.5, "files": [f["id"]]})
    with pytest.raises(HubError):
        hub.post_message(scholar, "general", "question", "Look?", data={"files": [f["id"]]})
    # Files in a private room stay there.
    secret = hub.upload_file(scholar, "scholar+baby", "notes.txt", "text/plain", b"secret")
    with pytest.raises(HubError) as e:
        hub.read_file(builder, secret["id"])
    assert e.value.status == 404
    with pytest.raises(HubError):
        hub.upload_file(builder, "scholar+baby", "x.txt", "text/plain", b"x")
    hub.delete_file(f["id"])
    with pytest.raises(HubError):
        hub.read_file(boss, f["id"])


def test_limits(hub):
    hub.update_settings({"max_file_mb": 1})
    with pytest.raises(HubError) as e:
        hub.upload_file(hub.member("amo"), "general", "big.bin", None, b"x" * (1024 * 1024 + 1))
    assert e.value.status == 413
    with pytest.raises(HubError):
        hub.upload_file(hub.member("amo"), "general", "empty.txt", None, b"")


def test_files_over_http(server, tokens, tmp_path, api):
    img = tmp_path / "chart.png"
    img.write_bytes(PNG)
    c = HubClient(tokens["scholar"], server, outbox=False)
    f = c.upload_file(img, "discoveries")
    assert f["preview"] and f["mime"] == "image/png"
    m = c.post_finding("discoveries", "Chart attached", "reported", 0.5, files=[f])
    assert m["attachments"][0]["name"] == "chart.png"
    out = c.download_file(f["id"], tmp_path / "copy.png")
    assert out.read_bytes() == PNG
    # Downloads never render as pages; images only inline when asked.
    import urllib.request
    req = urllib.request.Request(f"{server}/api/files/{f['id']}", headers={"Authorization": f"Bearer {tokens['amo']}"})
    with urllib.request.urlopen(req) as r:
        assert r.headers["Content-Type"] == "application/octet-stream"
        assert r.headers["Content-Disposition"].startswith("attachment")
        assert "sandbox" in r.headers["Content-Security-Policy"]
    req = urllib.request.Request(f"{server}/api/files/{f['id']}?inline=1",
                                 headers={"Authorization": f"Bearer {tokens['amo']}"})
    with urllib.request.urlopen(req) as r:
        assert r.headers["Content-Type"] == "image/png"
    assert api("GET", f"/api/files/{f['id']}")[0] == 401
