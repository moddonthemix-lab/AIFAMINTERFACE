import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hub import seed  # noqa: E402
from hub.core import Hub  # noqa: E402
from hub.server import make_server  # noqa: E402

AGENTS = ("amo", "scholar", "builder", "baby", "butler")


@pytest.fixture
def hub(tmp_path):
    h = Hub(tmp_path / "home")
    seed.setup(h)
    return h


@pytest.fixture
def free(hub):
    """Let every agent start tasks without approval (for tests about other features)."""
    for a in AGENTS:
        hub.update_member(a, task_autonomy="free")


@pytest.fixture
def tokens(hub):
    t = {a: hub.reset_token(a) for a in AGENTS}
    t["boss"] = hub.read_boss_token()
    return t


class Api:
    def __init__(self, base):
        self.base = base

    def __call__(self, method, path, body=None, token=None, timeout=10):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw[:1] in (b"{", b"[") else raw
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def server(hub):
    srv = make_server(hub, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def api(server):
    return Api(server)
