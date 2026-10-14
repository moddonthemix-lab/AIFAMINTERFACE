"""Anti-chatter rules. The server enforces these; agents are never trusted to behave.

  * rate limits per agent (per minute and per day)
  * plain `post` messages only as replies to a question/task, or a few per day
    in #general and private rooms
  * duplicate guard (same message twice in a short time)
  * loop breaker: agents going back and forth in a thread without progress
    get the thread paused and the boss is told
  * paused agents and paused threads cannot post

The boss is never limited. Length limits are checked in messages.py.
"""

import json
import time

DEFAULTS = {
    "rate_per_minute": 10,      # messages per agent per minute
    "rate_per_day": 300,        # messages per agent per day
    "free_posts_per_day": 5,    # plain posts allowed outside a question/task thread
    "loop_limit": 6,            # agent messages in a row in one thread without progress
    "max_text": 4000,           # characters per text field
    "dup_window_sec": 600,      # duplicate guard looks this far back
    "max_file_mb": 10,          # largest attachment
    "max_storage_mb": 2000,     # all attachments together
}

# Messages that count as real progress in a thread (they reset the loop counter).
PROGRESS_TYPES = ("result", "finding", "task_update")
# Roots under which plain `post` replies are always welcome.
ANSWERABLE = ("question", "task_request")
FREE_POST_CHANNELS = ("general",)


class RuleViolation(Exception):
    def __init__(self, status, rule, message):
        super().__init__(message)
        self.status = status
        self.rule = rule
        self.message = message


def check(conn, settings, author, channel, type_, text, data, root, thread_state):
    """Raise RuleViolation if an agent may not send this message right now.

    `author` and `channel` are database rows, `root` is the thread's first
    message (or None), `thread_state` is the threads row (or None).
    """
    if author["paused"]:
        raise RuleViolation(403, "agent_paused", "You are paused by the boss. Wait until you are unpaused.")
    if thread_state is not None and thread_state["paused"]:
        raise RuleViolation(423, "thread_paused",
                            "This thread is paused and waiting for the boss. Do not post in it until it is unpaused.")

    now = time.time()
    per_min = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE author=? AND created_at>?", (author["id"], now - 60)).fetchone()[0]
    if per_min >= settings["rate_per_minute"]:
        raise RuleViolation(429, "rate_minute",
                            f"Too many messages: the limit is {settings['rate_per_minute']} per minute. Slow down.")
    per_day = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE author=? AND created_at>?", (author["id"], now - 86400)).fetchone()[0]
    if per_day >= settings["rate_per_day"]:
        raise RuleViolation(429, "rate_day",
                            f"Too many messages today: the limit is {settings['rate_per_day']} per day.")

    if type_ == "post" and not (root is not None and root["type"] in ANSWERABLE):
        if not (channel["name"] in FREE_POST_CHANNELS or channel["private"]):
            raise RuleViolation(
                422, "post_not_allowed",
                "Plain posts are only for replying to a question or a task, or in #general and private rooms. "
                "Use a structured type instead: question, task_request, task_update, result or finding.")
        used = conn.execute(
            """SELECT COUNT(*) FROM messages m LEFT JOIN messages r ON r.id = m.reply_to
               WHERE m.author=? AND m.type='post' AND m.created_at>?
                 AND (r.id IS NULL OR r.type NOT IN ('question','task_request'))""",
            (author["id"], now - 86400)).fetchone()[0]
        if used >= settings["free_posts_per_day"]:
            raise RuleViolation(
                429, "free_posts",
                f"You have used today's {settings['free_posts_per_day']} free posts. "
                "Reply to a question or task, or share a finding instead.")

    recent = conn.execute(
        "SELECT type, text, data FROM messages WHERE author=? AND created_at>? ORDER BY id DESC LIMIT 20",
        (author["id"], now - settings["dup_window_sec"])).fetchall()
    wanted = json.dumps(data, sort_keys=True)
    for r in recent:
        if r["type"] == type_ and r["text"].strip() == text.strip() \
                and json.dumps(json.loads(r["data"]), sort_keys=True) == wanted:
            raise RuleViolation(409, "duplicate", "You already sent this exact message a moment ago.")


def loop_detected(conn, root_id, limit, member_kinds):
    """After an agent posted in a thread: are agents going around in circles?

    Counts messages from the newest backwards until something that counts as
    progress (a result, finding, task update, or anything from the boss).
    Returns a reason string if the count reached `limit` with 2+ agents involved.
    """
    rows = conn.execute(
        "SELECT author, type FROM messages WHERE (id=? OR reply_to=?) AND type!='system' ORDER BY id DESC",
        (root_id, root_id)).fetchall()
    count, authors = 0, set()
    for r in rows:
        if r["type"] in PROGRESS_TYPES or member_kinds.get(r["author"]) == "boss":
            break
        count += 1
        authors.add(r["author"])
    if count >= limit and len(authors) >= 2:
        names = " and ".join(sorted(authors))
        return f"{names} exchanged {count} messages without a result, finding or task update."
    return None
