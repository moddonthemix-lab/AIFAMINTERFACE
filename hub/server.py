"""The web server: serves the one-page interface and the JSON API.

Standard library only (http.server). Every /api/ call except /api/health needs
`Authorization: Bearer <token>`. The author of a message always comes from the
token, never from the request body.
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, config
from .core import HubError

STATIC = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

ROUTES = []


def route(method, pattern, auth="member"):
    """Register a handler. auth: None (public), "member" (any token) or "boss"."""
    def wrap(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), auth, fn))
        return fn
    return wrap


def _int(q, key, default=None):
    v = q.get(key)
    if v in (None, ""):
        return default
    try:
        return int(v)
    except ValueError:
        raise HubError(400, f"'{key}' must be a number") from None


# ---------------------------------------------------------------------- API
@route("GET", "/api/health", auth=None)
def health(h, me, q, body):
    return {"ok": True, "name": "Family Hub", "version": __version__, "time": time.time()}


@route("GET", "/api/me")
def get_me(h, me, q, body):
    return h.hub.me(me)


@route("GET", "/api/members")
def get_members(h, me, q, body):
    return {"members": h.hub.members()}


@route("GET", "/api/channels")
def get_channels(h, me, q, body):
    return {"channels": h.hub.channels(me)}


@route("GET", r"/api/channels/(?P<name>[^/]+)/messages")
def get_messages(h, me, q, body, name):
    return {"messages": h.hub.list_messages(me, name, after=_int(q, "after"), before=_int(q, "before"),
                                            limit=_int(q, "limit", 50), top_only=q.get("top") == "1")}


@route("POST", r"/api/channels/(?P<name>[^/]+)/messages")
def post_message(h, me, q, body, name):
    # Accept fields either inside "data" or at the top level, for easy curl use.
    data = body.get("data")
    data = dict(data) if isinstance(data, dict) else {} if data is None else data
    if isinstance(data, dict):
        for k, v in body.items():
            if k not in ("type", "text", "data", "reply_to", "author", "client_id") and k not in data:
                data[k] = v
    return h.hub.post_message(me, name, body.get("type") or "post", body.get("text"), data, body.get("reply_to"),
                              client_id=body.get("client_id"))


@route("GET", r"/api/threads/(?P<mid>\d+)")
def get_thread(h, me, q, body, mid):
    return h.hub.thread(me, int(mid))


@route("GET", "/api/tasks")
def get_tasks(h, me, q, body):
    return {"tasks": h.hub.tasks(me, q.get("assignee"), q.get("status"), q.get("channel"))}


@route("GET", r"/api/tasks/(?P<tid>\d+)")
def get_task(h, me, q, body, tid):
    return h.hub.task(me, int(tid))


@route("POST", r"/api/tasks/(?P<tid>\d+)/update")
def post_task_update(h, me, q, body, tid):
    return h.hub.update_task(me, int(tid), body.get("status"), body.get("note") or "", body.get("client_id"))


@route("POST", r"/api/tasks/(?P<tid>\d+)/approve", auth="boss")
def post_task_approve(h, me, q, body, tid):
    return h.hub.decide_task(me, int(tid), bool(body.get("approve", True)), body.get("note") or "")


@route("GET", "/api/inbox")
def get_inbox(h, me, q, body):
    return {"items": h.hub.inbox(me, unread_only=q.get("unread") == "1"), "unread": h.hub.unread_count(me)}


@route("POST", "/api/inbox/read")
def post_inbox_read(h, me, q, body):
    ids = None if body.get("all") or body.get("ids") is None else body.get("ids")
    if ids is not None and not isinstance(ids, list):
        raise HubError(400, "'ids' must be a list")
    return {"unread": h.hub.mark_read(me, ids)}


@route("GET", "/api/events")
def get_events(h, me, q, body):
    after = _int(q, "after")
    if after is None:
        return {"events": [], "last": h.hub.last_event_id()}
    events, last = h.hub.wait_events(me, after, _int(q, "wait", 0))
    return {"events": events, "last": last}


@route("GET", "/api/search")
def get_search(h, me, q, body):
    return {"messages": h.hub.search(me, q.get("q", ""), limit=_int(q, "limit", 30))}


@route("GET", "/api/brief")
def get_brief(h, me, q, body):
    return h.hub.brief(me)  # already compact JSON bytes


@route("GET", "/api/activity")
def get_activity(h, me, q, body):
    return {"messages": h.hub.recent_activity(me, _int(q, "limit", 10))}


@route("GET", "/api/stats")
def get_stats(h, me, q, body):
    s = h.hub.stats()
    if me["kind"] != "boss":
        s.pop("settings", None)
    return s


# ---------------------------------------------------------------------- admin (boss only)
@route("GET", "/api/admin/overview", auth="boss")
def admin_overview(h, me, q, body):
    return {"settings": h.hub.settings, "rule_log": h.hub.rule_log(),
            "paused_threads": h.hub.paused_threads(), "members": h.hub.members()}


@route("PUT", "/api/admin/settings", auth="boss")
def admin_settings(h, me, q, body):
    return {"settings": h.hub.update_settings(body)}


@route("POST", "/api/admin/agents", auth="boss")
def admin_add_agent(h, me, q, body):
    token = h.hub.add_member(body.get("id"), body.get("name"), body.get("role"), body.get("emoji") or "🤖",
                             card_url=body.get("card_url"))
    return {"id": body.get("id", "").strip().lower(), "token": token,
            "note": "Copy this token now. It is shown only once."}


@route("POST", r"/api/admin/agents/(?P<mid>[^/]+)/token", auth="boss")
def admin_token(h, me, q, body, mid):
    if mid == "boss":
        raise HubError(400, "reset the boss token from the terminal: hub token boss --reset")
    return {"id": mid, "token": h.hub.reset_token(mid), "note": "Copy this token now. It is shown only once."}


@route("POST", r"/api/admin/members/(?P<mid>[^/]+)", auth="boss")
def admin_update_member(h, me, q, body, mid):
    h.hub.update_member(mid, **{k: body.get(k) for k in ("name", "role", "emoji", "card_url", "task_autonomy")})
    return {"ok": True}


@route("DELETE", r"/api/admin/agents/(?P<mid>[^/]+)", auth="boss")
def admin_remove_agent(h, me, q, body, mid):
    h.hub.remove_member(mid)
    return {"ok": True}


@route("POST", "/api/admin/channels", auth="boss")
def admin_channel(h, me, q, body):
    local = body.get("local_only")
    name = h.hub.create_channel(body.get("name"), body.get("purpose", ""), bool(body.get("private")),
                                body.get("members") or [], local_only=None if local is None else bool(local))
    return {"name": name}


@route("POST", r"/api/admin/channels/(?P<name>[^/]+)", auth="boss")
def admin_update_channel(h, me, q, body, name):
    local = body.get("local_only")
    h.hub.update_channel(name, body.get("purpose"), None if local is None else bool(local))
    return {"ok": True}


@route("POST", r"/api/admin/channels/(?P<name>[^/]+)/members", auth="boss")
def admin_room_members(h, me, q, body, name):
    h.hub.update_room_members(name, body.get("add") or [], body.get("remove") or [])
    return {"ok": True}


@route("POST", "/api/admin/pause", auth="boss")
def admin_pause(h, me, q, body):
    paused = bool(body.get("paused", True))
    if body.get("agent"):
        h.hub.set_agent_paused(body["agent"], paused)
        return {"agent": body["agent"], "paused": paused}
    if body.get("thread"):
        root = h.hub.set_thread_paused(int(body["thread"]), paused)
        return {"thread": root, "paused": paused}
    raise HubError(400, "give either 'agent' or 'thread'")


# ---------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = f"FamilyHub/{__version__}"
    sys_version = ""
    hub = None  # set by make_server

    def log_message(self, fmt, *args):
        if config.debug():
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _token(self, query):
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def _dispatch(self, method):
        url = urlparse(self.path)
        path = unquote(url.path).rstrip("/") or "/"  # e.g. scholar%2Bbaby -> scholar+baby
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, self.server.page, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": PAGE_CSP})
            if method == "GET" and path == "/api/stream":
                return self._stream(query)
            for m, rx, auth, fn in ROUTES:
                match = rx.match(path)
                if match is None or m != method:
                    continue
                me = None
                if auth:
                    me = self.hub.authenticate(self._token(query))
                    if me is None:
                        raise HubError(401, "missing or wrong token. Send 'Authorization: Bearer <your token>'.")
                    if auth == "boss" and me["kind"] != "boss":
                        raise HubError(403, "only the boss can do this")
                body = self._body() if method in ("POST", "PUT") else {}
                result = fn(self, me, query, body, **match.groupdict())
                return self._send(200, result)
            if any(rx.match(path) for _, rx, _, _ in ROUTES):
                raise HubError(405, f"{method} is not allowed here")
            raise HubError(404, f"nothing at {path}")
        except HubError as e:
            payload = {"error": e.message}
            if e.rule:
                payload["rule"] = e.rule
            self._send(e.status, payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # keep the server alive; report a plain message
            sys.stderr.write(f"hub: error on {method} {path}: {e!r}\n")
            try:
                self._send(500, {"error": "something went wrong inside the hub; see the terminal"})
            except OSError:
                pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HubError(413, "request is too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise HubError(400, "the body must be JSON") from None
        if not isinstance(body, dict):
            raise HubError(400, "the body must be a JSON object")
        return body

    def _stream(self, query):
        """Server-Sent Events. Token in the header, or ?token= for browsers' EventSource."""
        me = self.hub.authenticate(self._token(query) or query.get("token"))
        if me is None:
            raise HubError(401, "missing or wrong token")
        after = int(query.get("after") or self.hub.last_event_id())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            while True:
                events, after = self.hub.wait_events(me, after, 15)
                for ev in events:
                    self.wfile.write(f"id: {ev['id']}\nevent: {ev['kind']}\ndata: {json.dumps(ev)}\n\n".encode())
                if not events:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                self.hub.authenticate(self._token(query) or query.get("token"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(hub, host="127.0.0.1", port=8770):
    handler = type("BoundHandler", (Handler,), {"hub": hub})
    server = HubServer((host, port), handler)
    server.page = (STATIC / "index.html").read_bytes()
    return server
