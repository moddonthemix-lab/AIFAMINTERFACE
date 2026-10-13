"""The heart of the hub: members, channels, messages, tasks, inbox and events.

Everything the web server and the CLI do goes through the Hub class, so the
rules are enforced in exactly one place. Message content is stored and shown,
never executed or obeyed.
"""

import contextlib
import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from . import db, rules
from .messages import TASK_STATUSES, ValidationError, parse_target, validate

ONLINE_SEC = 120
IDLE_SEC = 900
MENTION_RE = re.compile(r"(?<![\w@])@([a-z][a-z0-9_-]{0,39})", re.I)
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9+_-]{0,39}$")
REVIEWERS = ("butler",)


class HubError(Exception):
    """An error with an HTTP status and a plain-language message."""

    def __init__(self, status, message, rule=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.rule = rule


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(member_id):
    return f"hub_{member_id}_{secrets.token_urlsafe(24)}"


def _loads(s):
    return json.loads(s) if s else {}


class Hub:
    def __init__(self, home):
        self.home = Path(home).expanduser()
        self.home.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.home, 0o700)
        self.db_path = self.home / "hub.db"
        self._local = threading.local()
        self.lock = threading.RLock()
        self.changed = threading.Condition()
        self.started = time.time()
        db.init(self.conn)
        self._settings = None
        self._local_only = None

    # ------------------------------------------------------------------ plumbing
    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = db.connect(self.db_path)
            self._local.depth = 0
        return c

    @contextlib.contextmanager
    def tx(self):
        """A write transaction. Nested calls join the outer one."""
        with self.lock:
            conn = self.conn
            if self._local.depth:
                self._local.depth += 1
                try:
                    yield conn
                finally:
                    self._local.depth -= 1
                return
            conn.execute("BEGIN IMMEDIATE")
            self._local.depth = 1
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            finally:
                self._local.depth = 0
        with self.changed:
            self.changed.notify_all()

    def q(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    # ------------------------------------------------------------------ settings
    @property
    def settings(self):
        if self._settings is None:
            s = dict(rules.DEFAULTS)
            for r in self.q("SELECT key, value FROM settings"):
                if r["key"] in s:
                    s[r["key"]] = int(r["value"])
            self._settings = s
        return self._settings

    def update_settings(self, values):
        clean = {}
        for k, v in (values or {}).items():
            if k not in rules.DEFAULTS:
                raise HubError(400, f"unknown setting '{k}'")
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise HubError(400, f"setting '{k}' must be a whole number") from None
            if v < 1 or v > 1_000_000:
                raise HubError(400, f"setting '{k}' must be between 1 and 1000000")
            clean[k] = v
        with self.tx() as c:
            for k, v in clean.items():
                c.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        self._settings = None
        self._local_only = None
        return self.settings

    # ------------------------------------------------------------------ members & auth
    def member(self, member_id):
        return self.q1("SELECT * FROM members WHERE id=? AND kind!='removed'", (member_id,))

    def authenticate(self, token):
        if not token:
            return None
        m = self.q1("SELECT * FROM members WHERE token_hash=? AND kind!='removed'", (hash_token(token),))
        if m is not None:
            now = time.time()
            if not m["last_seen"] or now - m["last_seen"] > 10:
                with self.tx() as c:
                    c.execute("UPDATE members SET last_seen=? WHERE id=?", (now, m["id"]))
        return m

    @staticmethod
    def presence(last_seen, now=None):
        if not last_seen:
            return "offline"
        age = (now or time.time()) - last_seen
        return "online" if age < ONLINE_SEC else "idle" if age < IDLE_SEC else "offline"

    def member_dict(self, m, now=None):
        return {
            "id": m["id"], "name": m["name"], "role": m["role"], "emoji": m["emoji"], "kind": m["kind"],
            "card_url": m["card_url"], "status": self.presence(m["last_seen"], now),
            "last_seen": m["last_seen"], "paused": bool(m["paused"]), "has_token": bool(m["token_hash"]),
        }

    def members(self):
        now = time.time()
        rows = self.q("SELECT * FROM members WHERE kind!='removed' ORDER BY kind='boss', created_at")
        return [self.member_dict(m, now) for m in rows]

    def add_member(self, member_id, name, role="", emoji="🤖", kind="agent", card_url=None, with_token=True):
        member_id = (member_id or "").strip().lower()
        if not ID_RE.match(member_id) or member_id in ("hub", "all", "everyone"):
            raise HubError(400, "member id must be short lowercase letters/numbers, like 'builder'")
        existing = self.q1("SELECT kind FROM members WHERE id=?", (member_id,))
        if existing is not None and existing["kind"] != "removed":
            raise HubError(409, f"'{member_id}' is already a member")
        token = new_token(member_id) if with_token else None
        with self.tx() as c:
            c.execute("DELETE FROM members WHERE id=?", (member_id,))
            c.execute("INSERT INTO members(id, name, role, emoji, kind, token_hash, card_url, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (member_id, (name or member_id.title())[:60], (role or "")[:200], (emoji or "🤖")[:8],
                       kind, hash_token(token) if token else None, card_url, time.time()))
            self._event(c, "member", {"id": member_id})
        return token

    def update_member(self, member_id, **fields):
        if self.member(member_id) is None:
            raise HubError(404, f"no member '{member_id}'")
        allowed = {"name": 60, "role": 200, "emoji": 8, "card_url": 300}
        with self.tx() as c:
            for k, v in fields.items():
                if k in allowed and v is not None:
                    c.execute(f"UPDATE members SET {k}=? WHERE id=?", (str(v)[:allowed[k]], member_id))
            self._event(c, "member", {"id": member_id})

    def reset_token(self, member_id):
        if self.member(member_id) is None:
            raise HubError(404, f"no member '{member_id}'")
        token = new_token(member_id)
        with self.tx() as c:
            c.execute("UPDATE members SET token_hash=? WHERE id=?", (hash_token(token), member_id))
        if member_id == "boss":
            self._write_boss_token(token)
        return token

    def remove_member(self, member_id):
        m = self.member(member_id)
        if m is None:
            raise HubError(404, f"no member '{member_id}'")
        if m["kind"] == "boss":
            raise HubError(400, "the boss cannot be removed")
        with self.tx() as c:
            c.execute("UPDATE members SET kind='removed', token_hash=NULL WHERE id=?", (member_id,))
            c.execute("DELETE FROM channel_members WHERE member=?", (member_id,))
            self._event(c, "member", {"id": member_id})

    def set_agent_paused(self, member_id, paused):
        m = self.member(member_id)
        if m is None or m["kind"] != "agent":
            raise HubError(404, f"no agent '{member_id}'")
        with self.tx() as c:
            c.execute("UPDATE members SET paused=? WHERE id=?", (int(bool(paused)), member_id))
            self._event(c, "member", {"id": member_id, "paused": bool(paused)})

    def boss_token_file(self):
        return self.home / "boss_token"

    def _write_boss_token(self, token):
        f = self.boss_token_file()
        f.write_text(token + "\n")
        with contextlib.suppress(OSError):
            os.chmod(f, 0o600)

    def ensure_boss(self):
        """Make sure the boss exists and has a token. Returns the token if one was just created."""
        if self.member("boss") is None:
            token = self.add_member("boss", "Boss", "The human. Final say on everything.", "👑", kind="boss")
            self._write_boss_token(token)
            return token
        if not self.member("boss")["token_hash"] or not self.boss_token_file().exists():
            return self.reset_token("boss")
        return None

    def read_boss_token(self):
        f = self.boss_token_file()
        return f.read_text().strip() if f.exists() else None

    # ------------------------------------------------------------------ channels
    def channel(self, name):
        return self.q1("SELECT * FROM channels WHERE name=?", ((name or "").lower().lstrip("#"),))

    def can_see(self, member, channel):
        if channel is None:
            return False
        if member["kind"] == "boss" or not channel["private"]:
            return True
        return self.q1("SELECT 1 FROM channel_members WHERE channel=? AND member=?",
                       (channel["name"], member["id"])) is not None

    def visible_channel_names(self, member):
        if member["kind"] == "boss":
            return [r["name"] for r in self.q("SELECT name FROM channels")]
        return [r["name"] for r in self.q(
            "SELECT name FROM channels WHERE private=0 OR name IN "
            "(SELECT channel FROM channel_members WHERE member=?)", (member["id"],))]

    def channels(self, member):
        names = set(self.visible_channel_names(member))
        out = []
        for ch in self.q("SELECT * FROM channels ORDER BY private, position, name"):
            if ch["name"] not in names:
                continue
            last = self.q1("SELECT MAX(id) FROM messages WHERE channel=?", (ch["name"],))[0]
            out.append({
                "name": ch["name"], "purpose": ch["purpose"], "private": bool(ch["private"]),
                "local_only": bool(ch["local_only"]),
                "members": [r["member"] for r in self.q(
                    "SELECT member FROM channel_members WHERE channel=? ORDER BY member", (ch["name"],))]
                if ch["private"] else None,
                "last_id": last or 0,
            })
        return out

    def create_channel(self, name, purpose="", private=False, members=(), position=100, local_only=None):
        """local_only: agents must keep this channel's messages on local AI models.
        Private rooms are local-only unless you say otherwise."""
        if local_only is None:
            local_only = private
        name = (name or "").strip().lower().lstrip("#")
        if not CHANNEL_RE.match(name):
            raise HubError(400, "channel names use lowercase letters, numbers, - _ and +, like 'study-room'")
        if self.channel(name) is not None:
            raise HubError(409, f"#{name} already exists")
        with self.tx() as c:
            c.execute("INSERT INTO channels(name, purpose, private, position, created_at, local_only) "
                      "VALUES (?,?,?,?,?,?)",
                      (name, (purpose or "")[:200], int(bool(private)), position, time.time(), int(bool(local_only))))
            if private:
                self._set_room_members(c, name, add=members)
            self._event(c, "channel", {"name": name})
        self._local_only = None
        return name

    def update_channel(self, name, purpose=None, local_only=None):
        ch = self.channel(name)
        if ch is None:
            raise HubError(404, f"no channel #{name}")
        with self.tx() as c:
            if purpose is not None:
                c.execute("UPDATE channels SET purpose=? WHERE name=?", (str(purpose)[:200], ch["name"]))
            if local_only is not None:
                c.execute("UPDATE channels SET local_only=? WHERE name=?", (int(bool(local_only)), ch["name"]))
            self._event(c, "channel", {"name": ch["name"]})
        self._local_only = None

    def local_only_channels(self):
        if self._local_only is None:
            self._local_only = {r["name"] for r in self.q("SELECT name FROM channels WHERE local_only=1")}
        return self._local_only

    def _set_room_members(self, c, name, add=(), remove=()):
        for mid in add or ():
            if self.member(mid) is None:
                raise HubError(400, f"no member '{mid}'")
            c.execute("INSERT OR IGNORE INTO channel_members(channel, member) VALUES (?,?)", (name, mid))
        for mid in remove or ():
            c.execute("DELETE FROM channel_members WHERE channel=? AND member=?", (name, mid))

    def update_room_members(self, name, add=(), remove=()):
        ch = self.channel(name)
        if ch is None:
            raise HubError(404, f"no channel #{name}")
        if not ch["private"]:
            raise HubError(400, "only private rooms have a member list; public channels are open to the family")
        with self.tx() as c:
            self._set_room_members(c, ch["name"], add, remove)
            self._event(c, "channel", {"name": ch["name"]})

    # ------------------------------------------------------------------ events
    def _event(self, c, kind, payload, channel=None, member=None):
        c.execute("INSERT INTO events(kind, channel, member, payload, created_at) VALUES (?,?,?,?,?)",
                  (kind, channel, member, json.dumps(payload), time.time()))

    def last_event_id(self):
        return self.q1("SELECT COALESCE(MAX(id), 0) FROM events")[0]

    def events(self, member, after, limit=200):
        """Events after `after` that this member may see. Returns (events, last_id_scanned)."""
        rows = self.q("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit))
        if not rows:
            return [], after
        visible = set(self.visible_channel_names(member))
        out = []
        for r in rows:
            if r["member"] and r["member"] != member["id"]:
                continue
            if r["channel"] and r["channel"] not in visible:
                continue
            out.append({"id": r["id"], "kind": r["kind"], "channel": r["channel"],
                        "data": _loads(r["payload"]), "at": r["created_at"]})
        return out, rows[-1]["id"]

    def wait_events(self, member, after, wait):
        deadline = time.time() + max(0, min(wait, 60))
        while True:
            evs, last = self.events(member, after)
            if evs or time.time() >= deadline:
                return evs, last
            after = last
            with self.changed:
                # Re-check the database at least every 2s so writes from other
                # processes (like `hub post`) show up too.
                self.changed.wait(min(2.0, max(0.05, deadline - time.time())))

    def prune_events(self, keep_days=7):
        with self.tx() as c:
            c.execute("DELETE FROM events WHERE created_at<?", (time.time() - keep_days * 86400,))

    # ------------------------------------------------------------------ messages
    def msg_dict(self, r):
        return {"id": r["id"], "channel": r["channel"], "author": r["author"], "type": r["type"],
                "text": r["text"], "data": _loads(r["data"]), "reply_to": r["reply_to"],
                "created_at": r["created_at"], "demo": bool(r["demo"]),
                "local_only": r["channel"] in self.local_only_channels()}

    def message(self, mid):
        return self.q1("SELECT * FROM messages WHERE id=?", (mid,))

    def _visible_message(self, member, mid):
        m = self.message(mid)
        if m is None or not self.can_see(member, self.channel(m["channel"])):
            raise HubError(404, f"no message {mid}")
        return m

    def list_messages(self, member, channel_name, after=None, before=None, limit=50, top_only=False):
        ch = self.channel(channel_name)
        if not self.can_see(member, ch):
            raise HubError(404, f"no channel #{channel_name}")
        limit = max(1, min(int(limit or 50), 200))
        sql = "SELECT * FROM messages WHERE channel=?"
        args = [ch["name"]]
        if top_only:
            sql += " AND reply_to IS NULL"
        if after is not None:
            sql += " AND id>? ORDER BY id LIMIT ?"
            args += [int(after), limit]
            rows = self.q(sql, args)
        else:
            if before is not None:
                sql += " AND id<?"
                args.append(int(before))
            rows = list(reversed(self.q(sql + " ORDER BY id DESC LIMIT ?", args + [limit])))
        out = [self.msg_dict(r) for r in rows]
        if top_only and out:
            ids = [m["id"] for m in out]
            marks = ",".join("?" * len(ids))
            counts = {r["reply_to"]: (r["n"], r["last"]) for r in self.q(
                f"SELECT reply_to, COUNT(*) n, MAX(created_at) last FROM messages "
                f"WHERE reply_to IN ({marks}) GROUP BY reply_to", ids)}
            paused = {r["root_id"] for r in self.q(
                f"SELECT root_id FROM threads WHERE paused=1 AND root_id IN ({marks})", ids)}
            for m in out:
                n, last = counts.get(m["id"], (0, None))
                m["reply_count"], m["last_reply_at"], m["paused"] = n, last, m["id"] in paused
        return out

    def recent_activity(self, member, limit=10):
        names = self.visible_channel_names(member)
        if not names:
            return []
        marks = ",".join("?" * len(names))
        rows = self.q(f"SELECT * FROM messages WHERE channel IN ({marks}) AND type!='system' "
                      f"ORDER BY id DESC LIMIT ?", names + [max(1, min(int(limit), 50))])
        return [self.msg_dict(r) for r in rows]

    def thread(self, member, mid):
        m = self._visible_message(member, mid)
        root = self.message(m["reply_to"]) if m["reply_to"] else m
        replies = self.q("SELECT * FROM messages WHERE reply_to=? ORDER BY id", (root["id"],))
        state = self.q1("SELECT * FROM threads WHERE root_id=?", (root["id"],))
        task = self.q1("SELECT * FROM tasks WHERE message_id=?", (root["id"],))
        return {
            "root": self.msg_dict(root),
            "replies": [self.msg_dict(r) for r in replies],
            "paused": bool(state and state["paused"]),
            "needs_boss": bool(state and state["needs_boss"]),
            "reason": state["reason"] if state else None,
            "task": self.task_dict(task) if task else None,
        }

    def search(self, member, query, limit=30):
        words = re.findall(r"\w+", query or "")
        if not words:
            return []
        fts = " ".join('"' + w.replace('"', '') + '"*' for w in words[:8])
        names = self.visible_channel_names(member)
        if not names:
            return []
        marks = ",".join("?" * len(names))
        rows = self.q(
            f"SELECT m.* FROM messages_fts f JOIN messages m ON m.id=f.rowid "
            f"WHERE messages_fts MATCH ? AND m.channel IN ({marks}) ORDER BY m.id DESC LIMIT ?",
            [fts] + names + [max(1, min(int(limit), 100))])
        return [self.msg_dict(r) for r in rows]

    def post_message(self, author, channel_name, type_, text=None, data=None, reply_to=None,
                     *, enforce=True, demo=False, created_at=None, client_id=None):
        """Store a message after checking its shape, permissions and the anti-chatter rules.

        `author` is the members row of whoever is posting (set from their token,
        never from the request body).
        """
        is_boss = author["kind"] == "boss"
        if client_id is not None:
            # A message re-sent from an agent's outbox: if it already arrived, return it instead of posting twice.
            client_id = str(client_id)[:64]
            existing = self.q1("SELECT * FROM messages WHERE author=? AND client_id=?", (author["id"], client_id))
            if existing is not None:
                return dict(self.msg_dict(existing), already_received=True)
        if type_ == "system" and author["id"] != "hub":
            raise HubError(400, "only the hub itself posts 'system' messages")
        ch = self.channel(channel_name)
        if not self.can_see(author, ch):
            raise HubError(404, f"no channel #{(channel_name or '').lstrip('#')}")
        try:
            text, data = validate(type_, text, data, self.settings["max_text"])
        except ValidationError as e:
            raise HubError(400, str(e)) from None

        task = None
        if type_ in ("task_update", "result"):
            task = self.q1("SELECT * FROM tasks WHERE id=?", (data["task_id"],))
            if task is None or not self.can_see(author, self.channel(task["channel"])):
                raise HubError(404, f"no task {data['task_id']}")
            if not is_boss and author["id"] != task["assignee"]:
                raise HubError(403, f"only the assignee (@{task['assignee']}) or the boss can update task {task['id']}")
            if type_ == "task_update" and data["status"] == "open" and not is_boss:
                raise HubError(403, "only the boss can reopen a task")
            if task["status"] in ("done", "declined") and not is_boss:
                raise HubError(409, f"task {task['id']} is already {task['status']}")
            # Task updates always live in the task's own thread.
            ch = self.channel(task["channel"])
            reply_to = task["message_id"]

        if type_ == "review" and not (is_boss or author["id"] in REVIEWERS):
            raise HubError(403, "only Butler and the boss post reviews")

        target_author = None
        if type_ == "review":
            kind, ident = parse_target(data["target"])
            if kind == "task":
                t = self.q1("SELECT * FROM tasks WHERE id=?", (ident,))
                if t is None or not self.can_see(author, self.channel(t["channel"])):
                    raise HubError(404, f"no task {ident}")
                target_author = t["assignee"]
            else:
                target_author = self._visible_message(author, ident)["author"]

        if type_ == "task_request":
            assignee = self.member(data["assignee"])
            if assignee is None:
                raise HubError(400, f"no member '{data['assignee']}' to assign the task to")
            if not self.can_see(assignee, ch):
                raise HubError(400, f"@{assignee['id']} is not in #{ch['name']}, so they can't see this task")

        if type_ == "question" and data.get("to") and self.member(data["to"]) is None:
            raise HubError(400, f"no member '{data['to']}'")

        root = None
        if reply_to is not None:
            try:
                parent = self.message(int(reply_to))
            except (TypeError, ValueError):
                raise HubError(400, "'reply_to' must be a message id") from None
            if parent is None or parent["channel"] != ch["name"]:
                raise HubError(400, f"message {reply_to} is not in #{ch['name']}")
            # Threads are one level deep: a reply to a reply joins the same thread.
            root = self.message(parent["reply_to"]) if parent["reply_to"] else parent

        with self.lock:
            if enforce and not is_boss:
                state = self.q1("SELECT * FROM threads WHERE root_id=?", (root["id"],)) if root else None
                try:
                    rules.check(self.conn, self.settings, author, ch, type_, text, data, root, state)
                except rules.RuleViolation as v:
                    self._log_violation(author, ch, v)
                    raise HubError(v.status, v.message, rule=v.rule) from None

            with self.tx() as c:
                ts = created_at or time.time()
                cur = c.execute(
                    "INSERT INTO messages(channel, author, type, text, data, reply_to, created_at, demo, client_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (ch["name"], author["id"], type_, text, json.dumps(data), root["id"] if root else None,
                     ts, int(demo), client_id))
                msg = self.msg_dict(self.message(cur.lastrowid))
                self._event(c, "message", msg, channel=ch["name"])
                notify = {}  # member -> (kind, task_id, note); first reason wins

                def tell(mid, kind, task_id=None, note=None):
                    if mid and mid != author["id"] and mid not in notify:
                        notify[mid] = (kind, task_id, note)

                if type_ == "task_request":
                    tc = c.execute(
                        "INSERT INTO tasks(title, details, requester, assignee, status, priority, due, channel, "
                        "message_id, created_at, updated_at, demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (data["title"], data["details"], author["id"], data["assignee"], "open", data["priority"],
                         data.get("due"), ch["name"], msg["id"], ts, ts, int(demo)))
                    msg["task_id"] = tc.lastrowid
                    tell(data["assignee"], "assignment", tc.lastrowid)
                    self._event(c, "task", self.task_dict(self._task(tc.lastrowid)), channel=ch["name"])
                elif task is not None:
                    status = "done" if type_ == "result" else data["status"]
                    c.execute("UPDATE tasks SET status=?, updated_at=?, result_message_id=COALESCE(?, result_message_id) "
                              "WHERE id=?", (status, ts, msg["id"] if type_ == "result" else None, task["id"]))
                    tell(task["requester"], "task", task["id"], f"task {task['id']} → {status}")
                    self._event(c, "task", self.task_dict(self._task(task["id"])), channel=ch["name"])

                if type_ == "review":
                    tell(target_author, "review")
                if type_ == "question" and data.get("to"):
                    tell(data["to"], "mention")
                for name in MENTION_RE.findall(text):
                    m = self.member(name.lower())
                    if m is not None and self.can_see(m, ch):
                        tell(m["id"], "mention")
                if root is not None:
                    tell(root["author"], "reply")
                    if reply_to is not None and int(reply_to) != root["id"]:
                        tell(self.message(int(reply_to))["author"], "reply")

                for mid, (kind, task_id, note) in notify.items():
                    m = self.member(mid)
                    if m is not None and self.can_see(m, ch):
                        self._inbox(c, mid, kind, msg["id"], task_id, note, demo)

                if root is not None:
                    if is_boss:
                        # A message from the boss clears a paused thread.
                        self._set_thread(c, root["id"], paused=False, ch=ch["name"])
                    elif enforce and type_ not in rules.PROGRESS_TYPES:
                        kinds = {r["id"]: r["kind"] for r in c.execute("SELECT id, kind FROM members")}
                        reason = rules.loop_detected(c, root["id"], self.settings["loop_limit"], kinds)
                        if reason:
                            self._pause_thread(c, root, ch["name"], reason)
                if task is not None or type_ == "task_request":
                    msg["task"] = self.task_dict(self._task(msg.get("task_id") or task["id"]))
        return msg

    def _inbox(self, c, member_id, kind, message_id=None, task_id=None, note=None, demo=False):
        cur = c.execute("INSERT INTO inbox(member, kind, message_id, task_id, note, created_at, demo) "
                        "VALUES (?,?,?,?,?,?,?)", (member_id, kind, message_id, task_id, note, time.time(), int(demo)))
        self._event(c, "inbox", {"id": cur.lastrowid, "kind": kind}, member=member_id)

    def system_note(self, c, channel, text, reply_to=None):
        cur = c.execute("INSERT INTO messages(channel, author, type, text, data, reply_to, created_at) "
                        "VALUES (?, 'hub', 'system', ?, '{}', ?, ?)", (channel, text, reply_to, time.time()))
        self._event(c, "message", self.msg_dict(self.message(cur.lastrowid)), channel=channel)

    def _log_violation(self, author, ch, v):
        now = time.time()
        with self.tx() as c:
            recent = c.execute("SELECT 1 FROM rule_log WHERE member=? AND rule=? AND created_at>?",
                               (author["id"], v.rule, now - 3600)).fetchone()
            c.execute("INSERT INTO rule_log(member, rule, detail, created_at) VALUES (?,?,?,?)",
                      (author["id"], v.rule, v.message, now))
            # One visible note per agent and rule per hour, so the notes don't become chatter themselves.
            if recent is None and v.rule not in ("thread_paused", "agent_paused"):
                self.system_note(c, ch["name"], f"Rule check: @{author['id']} was stopped. {v.message}")

    def rule_log(self, limit=50):
        return [dict(r) for r in self.q("SELECT * FROM rule_log ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ threads
    def _pause_thread(self, c, root, channel, reason):
        self._set_thread(c, root["id"], paused=True, ch=channel, reason=reason)
        self.system_note(c, channel, f"Thread paused: {reason} Waiting for the boss.", reply_to=root["id"])
        self._inbox(c, "boss", "needs_boss", root["id"], note=reason)

    def _set_thread(self, c, root_id, paused, ch, reason=None):
        state = c.execute("SELECT paused FROM threads WHERE root_id=?", (root_id,)).fetchone()
        if not paused and (state is None or not state["paused"]):
            return
        c.execute("INSERT INTO threads(root_id, paused, needs_boss, reason, updated_at) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(root_id) DO UPDATE SET paused=excluded.paused, needs_boss=excluded.needs_boss, "
                  "reason=excluded.reason, updated_at=excluded.updated_at",
                  (root_id, int(paused), int(paused), reason, time.time()))
        if not paused:
            c.execute("UPDATE inbox SET read=1 WHERE kind='needs_boss' AND message_id=?", (root_id,))
        self._event(c, "thread", {"root_id": root_id, "paused": paused, "reason": reason}, channel=ch)

    def set_thread_paused(self, mid, paused, reason="Paused by the boss."):
        m = self.message(mid)
        if m is None:
            raise HubError(404, f"no message {mid}")
        root = self.message(m["reply_to"]) if m["reply_to"] else m
        with self.tx() as c:
            if paused:
                self._set_thread(c, root["id"], True, root["channel"], reason)
                c.execute("UPDATE threads SET needs_boss=0 WHERE root_id=?", (root["id"],))
            else:
                self._set_thread(c, root["id"], False, root["channel"])
        return root["id"]

    def paused_threads(self):
        out = []
        for r in self.q("SELECT * FROM threads WHERE paused=1 ORDER BY updated_at DESC"):
            m = self.message(r["root_id"])
            if m is not None:
                out.append({"root": self.msg_dict(m), "reason": r["reason"], "needs_boss": bool(r["needs_boss"])})
        return out

    # ------------------------------------------------------------------ tasks
    def _task(self, tid):
        return self.q1("SELECT * FROM tasks WHERE id=?", (tid,))

    def task_dict(self, t):
        d = {k: t[k] for k in ("id", "title", "details", "requester", "assignee", "status", "priority", "due",
                               "channel", "message_id", "result_message_id", "created_at", "updated_at")}
        d["demo"] = bool(t["demo"])
        if t["result_message_id"]:
            r = self.message(t["result_message_id"])
            if r is not None:
                d["result"] = {"summary": r["text"], "artifacts": _loads(r["data"]).get("artifacts", [])}
        return d

    def tasks(self, member, assignee=None, status=None, channel=None):
        names = self.visible_channel_names(member)
        if not names:
            return []
        sql = f"SELECT * FROM tasks WHERE channel IN ({','.join('?' * len(names))})"
        args = list(names)
        if assignee:
            sql += " AND assignee=?"
            args.append(assignee)
        if status:
            statuses = [s for s in status.split(",") if s in TASK_STATUSES]
            if statuses:
                sql += f" AND status IN ({','.join('?' * len(statuses))})"
                args += statuses
        if channel:
            sql += " AND channel=?"
            args.append(channel.lstrip("#"))
        return [self.task_dict(t) for t in self.q(sql + " ORDER BY id DESC LIMIT 500", args)]

    def task(self, member, tid):
        t = self._task(tid)
        if t is None or not self.can_see(member, self.channel(t["channel"])):
            raise HubError(404, f"no task {tid}")
        d = self.task_dict(t)
        d["updates"] = [self.msg_dict(r) for r in self.q(
            "SELECT * FROM messages WHERE reply_to=? AND type IN ('task_update','result') ORDER BY id",
            (t["message_id"],))]
        return d

    def update_task(self, member, tid, status, note="", client_id=None):
        t = self._task(tid)
        if t is None:
            raise HubError(404, f"no task {tid}")
        return self.post_message(member, t["channel"], "task_update", note,
                                 {"task_id": tid, "status": status, "note": note}, client_id=client_id)

    # ------------------------------------------------------------------ inbox
    def inbox(self, member, unread_only=False, limit=100):
        sql = "SELECT * FROM inbox WHERE member=?"
        if unread_only:
            sql += " AND read=0"
        out = []
        for r in self.q(sql + " ORDER BY id DESC LIMIT ?", (member["id"], limit)):
            item = {"id": r["id"], "kind": r["kind"], "note": r["note"], "read": bool(r["read"]),
                    "created_at": r["created_at"], "message": None, "task": None}
            if r["message_id"]:
                m = self.message(r["message_id"])
                if m is not None and self.can_see(member, self.channel(m["channel"])):
                    item["message"] = self.msg_dict(m)
            if r["task_id"]:
                t = self._task(r["task_id"])
                if t is not None:
                    item["task"] = self.task_dict(t)
            if item["message"] is None and item["task"] is None:
                continue
            out.append(item)
        return out

    def unread_count(self, member):
        return self.q1("SELECT COUNT(*) FROM inbox WHERE member=? AND read=0", (member["id"],))[0]

    def mark_read(self, member, ids=None):
        with self.tx() as c:
            if ids is None:
                c.execute("UPDATE inbox SET read=1 WHERE member=?", (member["id"],))
            else:
                ids = [int(i) for i in ids][:500]
                if ids:
                    c.execute(f"UPDATE inbox SET read=1 WHERE member=? AND id IN ({','.join('?' * len(ids))})",
                              [member["id"]] + ids)
            self._event(c, "inbox", {"read": True}, member=member["id"])
        return self.unread_count(member)

    # ------------------------------------------------------------------ summaries
    def me(self, member):
        return {
            "member": self.member_dict(member),
            "channels": self.visible_channel_names(member),
            "unread": self.unread_count(member),
            "last_event": self.last_event_id(),
        }

    def brief(self, member):
        """A tiny summary for ESP32 screens. Always under 1 KB."""
        names = self.visible_channel_names(member)
        if member["kind"] == "boss":
            open_n = self.q1("SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done','declined')")[0]
        else:
            open_n = self.q1("SELECT COUNT(*) FROM tasks WHERE assignee=? AND status NOT IN ('done','declined')",
                             (member["id"],))[0]
        out = {
            "me": member["id"],
            "unread": self.unread_count(member),
            "open": open_n,
            "blocked": self.q1("SELECT COUNT(*) FROM tasks WHERE status='blocked'")[0],
            "paused": self.q1("SELECT COUNT(*) FROM threads WHERE paused=1")[0],
            "ch": [],
        }
        for name in names[:8]:
            r = self.q1("SELECT author, text FROM messages WHERE channel=? AND type!='system' ORDER BY id DESC "
                        "LIMIT 1", (name,))
            if r is not None:
                out["ch"].append({"n": name, "a": r["author"], "t": " ".join(r["text"].split())[:48]})
        width = 48
        while True:
            body = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode()
            if len(body) < 1000 or not out["ch"]:
                return body
            width -= 8
            if width <= 0:
                out["ch"].pop()
                width = 8
            for c in out["ch"]:
                c["t"] = c["t"][:width]

    def stats(self):
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = round(rss / (1024 * 1024 if os.uname().sysname == "Darwin" else 1024), 1)
        except (ImportError, AttributeError):
            rss_mb = None
        size = sum(p.stat().st_size for p in self.home.glob("hub.db*") if p.exists())
        return {
            "uptime": round(time.time() - self.started),
            "memory_mb": rss_mb,
            "db_kb": round(size / 1024),
            "messages": self.q1("SELECT COUNT(*) FROM messages")[0],
            "tasks": self.q1("SELECT COUNT(*) FROM tasks")[0],
            "settings": self.settings,
        }
