"""Family memory: a short, curated list of things the whole family should know.

Agents propose memories; only the boss makes them "family truth". A memory can
be family-wide or belong to one private room (only its members see it). Each
agent's personal memory stays inside the agent; this is only for shared facts.
"""

import time

TOPICS = {
    "interests": "Interests & goals",   # what agents should explore and work towards
    "boss": "About you",
    "projects": "Projects",
    "decisions": "Decisions",
    "facts": "Facts",
    "other": "Other",
}
STATUSES = ("proposed", "active", "archived", "declined")
MAX_TEXT = 1000
MAX_PENDING = 20  # proposals one agent may have waiting at once


class MemoryMixin:
    """Mixed into core.Hub. Uses its database helpers, events and inbox."""

    def memory_dict(self, r):
        return {"id": r["id"], "topic": r["topic"], "text": r["text"], "scope": r["scope"],
                "status": r["status"], "author": r["author"], "source_id": r["source_id"],
                "created_at": r["created_at"], "updated_at": r["updated_at"], "demo": bool(r["demo"]),
                "local_only": bool(r["scope"]) and r["scope"] in self.local_only_channels()}

    def can_see_memory(self, member, mem):
        if mem["scope"] and not self.can_see(member, self.channel(mem["scope"])):
            return False
        if mem["status"] == "active" or member["kind"] == "boss":
            return True
        return mem["author"] == member["id"]  # agents see their own proposals

    def _memory(self, mid):
        from .core import HubError
        r = self.q1("SELECT * FROM memories WHERE id=?", (mid,))
        if r is None:
            raise HubError(404, f"no memory {mid}")
        return r

    def memories(self, member, topic=None, q=None, status="active"):
        sql, args = "SELECT * FROM memories WHERE 1=1", []
        if status != "all":
            sql += " AND status=?"
            args.append(status)
        if topic:
            sql += " AND topic=?"
            args.append(topic)
        if q:
            sql += " AND text LIKE ?"
            args.append(f"%{q}%")
        rows = self.q(sql + " ORDER BY topic='interests' DESC, topic, id DESC LIMIT 500", args)
        return [self.memory_dict(r) for r in rows if self.can_see_memory(member, r)]

    def _check_memory_fields(self, member, topic, text, scope):
        from .core import HubError
        if topic not in TOPICS:
            raise HubError(400, f"'topic' must be one of: {', '.join(TOPICS)}")
        if not isinstance(text, str) or not text.strip():
            raise HubError(400, "'text' is required")
        text = " ".join(text.split())
        if len(text) > MAX_TEXT:
            raise HubError(400, f"a memory is at most {MAX_TEXT} characters; keep it to one clear fact")
        if scope:
            scope = scope.lower().lstrip("#")
            if not self.can_see(member, self.channel(scope)):
                raise HubError(404, f"no channel #{scope}")
        return topic, text, scope or None

    def _memory_events(self, c, mem, kind="memory"):
        payload = {"id": mem["id"], "status": mem["status"]}
        if mem["status"] == "active":
            self._event(c, kind, payload, channel=mem["scope"])
        else:  # proposals and removed memories: only the boss and the author hear about them
            self._event(c, kind, payload, member="boss")
            if mem["author"] != "boss":
                self._event(c, kind, payload, member=mem["author"])

    def propose_memory(self, member, topic, text, scope=None, source=None, demo=False):
        """Agents' memories wait for the boss; the boss's go live at once."""
        from .core import HubError
        is_boss = member["kind"] == "boss"
        if member["paused"]:
            raise HubError(403, "You are paused by the boss.")
        topic, text, scope = self._check_memory_fields(member, topic, text, scope)
        if source is not None:
            source = int(source)
            self._visible_message(member, source)
        if self.q1("SELECT 1 FROM memories WHERE status IN ('proposed','active') AND lower(text)=lower(?) "
                   "AND COALESCE(scope,'')=?", (text, scope or "")):
            raise HubError(409, "the family already remembers this (or it is waiting for approval)")
        if not is_boss:
            pending = self.q1("SELECT COUNT(*) FROM memories WHERE author=? AND status='proposed'",
                              (member["id"],))[0]
            if pending >= MAX_PENDING:
                raise HubError(429, f"you already have {pending} memories waiting for the boss; wait for those first")
        now = time.time()
        with self.tx() as c:
            cur = c.execute("INSERT INTO memories(topic, text, scope, status, author, source_id, created_at, "
                            "updated_at, demo) VALUES (?,?,?,?,?,?,?,?,?)",
                            (topic, text, scope, "active" if is_boss else "proposed", member["id"], source, now, now,
                             int(demo)))
            mem = c.execute("SELECT * FROM memories WHERE id=?", (cur.lastrowid,)).fetchone()
            self._memory_events(c, mem)
            if not is_boss:
                self._inbox(c, "boss", "memory", note=f"{member['name']} wants the family to remember this",
                            memory_id=mem["id"], demo=demo)
        return self.memory_dict(mem)

    def decide_memory(self, boss, mid, approve, text=None, topic=None):
        """The boss approves (optionally after editing) or declines a proposed memory."""
        from .core import HubError
        mem = self._memory(mid)
        if mem["status"] != "proposed":
            raise HubError(409, f"memory {mid} is not waiting for approval (it is {mem['status']})")
        if approve and (text is not None or topic is not None):
            self.update_memory(boss, mid, text=text, topic=topic)
        with self.tx() as c:
            c.execute("UPDATE memories SET status=?, updated_at=? WHERE id=?",
                      ("active" if approve else "declined", time.time(), mid))
            c.execute("UPDATE inbox SET read=1 WHERE kind='memory' AND member='boss' AND memory_id=?", (mid,))
            mem = c.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            self._memory_events(c, mem)
            if mem["author"] != "boss":
                self._inbox(c, mem["author"], "memory",
                            note="the boss approved your memory" if approve else "the boss declined your memory",
                            memory_id=mid)
        return self.memory_dict(mem)

    def update_memory(self, boss, mid, text=None, topic=None, scope=None, status=None):
        from .core import HubError
        mem = self._memory(mid)
        new_topic, new_text, new_scope = self._check_memory_fields(
            boss, topic if topic is not None else mem["topic"], text if text is not None else mem["text"],
            (scope or None) if scope is not None else mem["scope"])
        if status is not None and status not in ("active", "archived"):
            raise HubError(400, "status can be set to 'active' or 'archived'")
        with self.tx() as c:
            c.execute("UPDATE memories SET topic=?, text=?, scope=?, status=COALESCE(?, status), updated_at=? "
                      "WHERE id=?", (new_topic, new_text, new_scope, status, time.time(), mid))
            mem = c.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            self._memory_events(c, mem)
        return self.memory_dict(mem)

    def delete_memory(self, boss, mid):
        mem = self._memory(mid)
        with self.tx() as c:
            c.execute("DELETE FROM memories WHERE id=?", (mid,))
            c.execute("DELETE FROM inbox WHERE memory_id=?", (mid,))
            gone = {"id": mid, "status": "deleted"}
            if mem["status"] == "active":
                self._event(c, "memory", gone, channel=mem["scope"])
            else:
                self._memory_events(c, dict(mem, status="deleted"))
