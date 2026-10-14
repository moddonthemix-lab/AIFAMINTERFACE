"""SQLite schema and connection helpers. One file: <HUB_HOME>/hub.db (WAL mode)."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT '',
    emoji       TEXT NOT NULL DEFAULT '🤖',
    kind        TEXT NOT NULL DEFAULT 'agent',      -- agent | boss | removed
    token_hash  TEXT,
    card_url    TEXT,
    last_seen   REAL,
    paused      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_members_token ON members(token_hash);

CREATE TABLE IF NOT EXISTS channels (
    name        TEXT PRIMARY KEY,
    purpose     TEXT NOT NULL DEFAULT '',
    private     INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL DEFAULT 100,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_members (
    channel     TEXT NOT NULL,
    member      TEXT NOT NULL,
    PRIMARY KEY (channel, member)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,
    author      TEXT NOT NULL,
    type        TEXT NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL DEFAULT '{}',
    reply_to    INTEGER,
    created_at  REAL NOT NULL,
    demo        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_messages_channel ON messages(channel, id);
CREATE INDEX IF NOT EXISTS ix_messages_reply ON messages(reply_to, id);
CREATE INDEX IF NOT EXISTS ix_messages_author ON messages(author, created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT NOT NULL,
    details             TEXT NOT NULL DEFAULT '',
    requester           TEXT NOT NULL,
    assignee            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open',
    priority            TEXT NOT NULL DEFAULT 'normal',
    due                 TEXT,
    channel             TEXT NOT NULL,
    message_id          INTEGER NOT NULL,
    result_message_id   INTEGER,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    demo                INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tasks_assignee ON tasks(assignee, status);

CREATE TABLE IF NOT EXISTS threads (
    root_id     INTEGER PRIMARY KEY,
    paused      INTEGER NOT NULL DEFAULT 0,
    needs_boss  INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    updated_at  REAL
);

CREATE TABLE IF NOT EXISTS inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member      TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- mention | assignment | reply | task | review | needs_boss
    message_id  INTEGER,
    task_id     INTEGER,
    note        TEXT,
    created_at  REAL NOT NULL,
    read        INTEGER NOT NULL DEFAULT 0,
    demo        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_inbox_member ON inbox(member, read, id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,          -- message | task | thread | inbox | member | channel
    channel     TEXT,                   -- if set: only members who can see this channel get it
    member      TEXT,                   -- if set: only this member gets it
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member      TEXT NOT NULL,
    rule        TEXT NOT NULL,
    detail      TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,              -- boss | projects | decisions | facts | other
    text        TEXT NOT NULL,
    scope       TEXT,                       -- NULL = whole family, else a channel/room name
    status      TEXT NOT NULL,              -- proposed | active | archived | declined
    author      TEXT NOT NULL,
    source_id   INTEGER,                    -- message it came from, if any
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    demo        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_memories_status ON memories(status, topic);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


def connect(path):
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# Columns added after the first release. init() adds them to older databases.
MIGRATIONS = [
    ("channels", "local_only", "INTEGER NOT NULL DEFAULT 0"),   # 1 = agents must not send it to cloud AI
    ("messages", "client_id", "TEXT"),                          # sender's id for safe re-sending
    ("members", "task_autonomy", "TEXT NOT NULL DEFAULT 'ask'"),  # ask | free | strict (see core.AUTONOMY)
    ("inbox", "memory_id", "INTEGER"),                          # a family-memory item this is about
]


def init(conn):
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            if (table, column) == ("channels", "local_only"):
                conn.execute("UPDATE channels SET local_only=private")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_client ON messages(author, client_id) "
                 "WHERE client_id IS NOT NULL")
