"""Backups: a daily copy of the hub's database, plus attached files.

Copies go to <HUB_HOME>/backups (or HUB_BACKUP_DIR, e.g. a Google Drive folder
shared with Linux). The newest HUB_BACKUP_KEEP copies (default 14) are kept.
"""

import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

NAME_RE = re.compile(r"^hub-\d{8}-\d{6}\.db$")
EVERY = 24 * 3600


class BackupMixin:
    """Mixed into core.Hub."""

    def backup_dir(self):
        d = Path(os.environ.get("HUB_BACKUP_DIR") or (self.home / "backups")).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def backups(self):
        out = []
        for p in sorted(self.backup_dir().glob("hub-*.db"), reverse=True):
            if NAME_RE.match(p.name):
                st = p.stat()
                out.append({"name": p.name, "size_kb": round(st.st_size / 1024), "created_at": st.st_mtime})
        return out

    def backup(self):
        """Make a consistent copy now (safe while the hub is running). Returns its info."""
        d = self.backup_dir()
        name = time.strftime("hub-%Y%m%d-%H%M%S.db")
        tmp = d / (name + ".part")
        dst = sqlite3.connect(str(tmp))
        try:
            with self.lock:
                self.conn.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, d / name)
        self._backup_files(d)
        keep = max(1, int(os.environ.get("HUB_BACKUP_KEEP", "14")))
        for old in self.backups()[keep:]:
            (d / old["name"]).unlink(missing_ok=True)
        return next(b for b in self.backups() if b["name"] == name)

    def _backup_files(self, d):
        """Attached files never change once stored, so copying the new ones is enough."""
        src = self.home / "files"
        if not src.is_dir():
            return
        dst = d / "files"
        dst.mkdir(exist_ok=True)
        for f in src.iterdir():
            if f.is_file() and not (dst / f.name).exists():
                shutil.copy2(f, dst / f.name)

    def last_backup_age(self):
        b = self.backups()
        return time.time() - b[0]["created_at"] if b else None

    def backup_if_due(self):
        age = self.last_backup_age()
        if age is None or age >= EVERY:
            return self.backup()
        return None

    def backup_path(self, name):
        """The full path of one backup, checking the name so nothing outside the folder can be read."""
        from .core import HubError
        if not NAME_RE.match(name or ""):
            raise HubError(400, "not a backup name")
        p = self.backup_dir() / name
        if not p.is_file():
            raise HubError(404, f"no backup {name}")
        return p


def restore(home, backup_file):
    """Replace the hub's database with a backup. The hub must be stopped first.
    The current database is kept as a safety copy. Returns the safety copy's path."""
    home = Path(home).expanduser()
    src = Path(backup_file).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"no file {src}")
    check = sqlite3.connect(str(src))
    try:
        check.execute("SELECT COUNT(*) FROM messages").fetchone()
    finally:
        check.close()
    db = home / "hub.db"
    safety = home / time.strftime("hub-before-restore-%Y%m%d-%H%M%S.db")
    if db.exists():
        cur = sqlite3.connect(str(db))
        out = sqlite3.connect(str(safety))
        try:
            cur.backup(out)
        finally:
            out.close()
            cur.close()
    for extra in ("hub.db-wal", "hub.db-shm"):
        (home / extra).unlink(missing_ok=True)
    shutil.copy2(src, db)
    return safety
