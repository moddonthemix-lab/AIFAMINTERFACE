"""File attachments: reports, PDFs, images attached to messages.

Files are stored under hub-chosen names in <HUB_HOME>/files, belong to one
channel (only people who can see it can download them), and are always served
as downloads, never run or shown as web pages. Images can be previewed.
"""

import hashlib
import os
import re
import secrets
import time

PREVIEW_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_FILES_PER_MESSAGE = 10
UPLOADS_PER_AGENT_PER_DAY = 50


def clean_name(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f\x7f\"<>|:*?]", "_", name)
    return (name or "file")[:120]


class FilesMixin:
    """Mixed into core.Hub."""

    def files_dir(self):
        d = self.home / "files"
        d.mkdir(exist_ok=True)
        return d

    def file_dict(self, r):
        return {"id": r["id"], "name": r["name"], "mime": r["mime"], "size": r["size"], "channel": r["channel"],
                "uploader": r["uploader"], "created_at": r["created_at"], "preview": r["mime"] in PREVIEW_TYPES}

    def _file(self, member, fid):
        from .core import HubError
        r = self.q1("SELECT * FROM files WHERE id=?", (fid,))
        if r is None or not self.can_see(member, self.channel(r["channel"])):
            raise HubError(404, f"no file {fid}")
        return r

    def upload_file(self, member, channel_name, name, mime, data):
        from .core import HubError
        ch = self.channel(channel_name)
        if not self.can_see(member, ch):
            raise HubError(404, f"no channel #{(channel_name or '').lstrip('#')}")
        if member["paused"]:
            raise HubError(403, "You are paused by the boss.")
        if not data:
            raise HubError(400, "the file is empty")
        limit = self.settings["max_file_mb"] * 1024 * 1024
        if len(data) > limit:
            raise HubError(413, f"files can be at most {self.settings['max_file_mb']} MB")
        used = self.q1("SELECT COALESCE(SUM(size), 0) FROM files")[0]
        if used + len(data) > self.settings["max_storage_mb"] * 1024 * 1024:
            raise HubError(507, "the hub's file storage is full; the boss can delete old files or raise the limit")
        if member["kind"] != "boss":
            n = self.q1("SELECT COUNT(*) FROM files WHERE uploader=? AND created_at>?",
                        (member["id"], time.time() - 86400))[0]
            if n >= UPLOADS_PER_AGENT_PER_DAY:
                raise HubError(429, f"at most {UPLOADS_PER_AGENT_PER_DAY} uploads per day")
        mime = (mime or "application/octet-stream").split(";")[0].strip().lower()[:100]
        stored = f"{int(time.time())}-{secrets.token_hex(8)}.bin"
        tmp = self.files_dir() / (stored + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, self.files_dir() / stored)
        with self.tx() as c:
            cur = c.execute("INSERT INTO files(name, mime, size, sha256, stored, uploader, channel, created_at) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (clean_name(name), mime, len(data), hashlib.sha256(data).hexdigest(), stored,
                             member["id"], ch["name"], time.time()))
        return self.file_dict(self._file(member, cur.lastrowid))

    def read_file(self, member, fid):
        r = self._file(member, fid)
        return self.file_dict(r), (self.files_dir() / r["stored"]).read_bytes()

    def delete_file(self, fid):
        from .core import HubError
        r = self.q1("SELECT * FROM files WHERE id=?", (fid,))
        if r is None:
            raise HubError(404, f"no file {fid}")
        with self.tx() as c:
            c.execute("DELETE FROM files WHERE id=?", (fid,))
        (self.files_dir() / r["stored"]).unlink(missing_ok=True)

    def check_attachments(self, author, channel, ids):
        """Files attached to a message must be in the same channel and uploaded by the author (or the boss)."""
        from .core import HubError
        for fid in ids:
            r = self.q1("SELECT * FROM files WHERE id=?", (fid,))
            if r is None or r["channel"] != channel:
                raise HubError(400, f"file {fid} isn't in #{channel}; upload it to this channel first")
            if author["kind"] != "boss" and r["uploader"] != author["id"]:
                raise HubError(403, f"file {fid} was uploaded by someone else")

    def attachments(self, ids):
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self.q(f"SELECT * FROM files WHERE id IN ({marks})", list(ids))
        by_id = {r["id"]: self.file_dict(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]
