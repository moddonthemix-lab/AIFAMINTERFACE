"""First-run setup: channels, the family, and an optional demo conversation."""

import time

CHANNELS = [
    ("general", "Shared discussions"),
    ("technical-help", "Agents solve technical problems together"),
    ("business", "Business ideas, research and analysis"),
    ("discoveries", "Useful information agents have found (verified findings)"),
    ("tasks", "Assignments, status updates and completed work"),
    ("trades", "Trader's journal: trades, daily P&L, and why he did or didn't trade"),
]

ROOMS = [
    ("scholar+baby", "Study room for Scholar and Baby", ["scholar", "baby"]),
]

FAMILY = [
    # id, name, role, emoji, card url
    ("amo", "Amo", "Main everyday companion", "🤝", "http://localhost:8765/api/agent"),
    ("scholar", "Scholar", "Researcher: deep research, business and income analysis", "🎓",
     "http://localhost:8766/api/agent"),
    ("builder", "Builder", "Coder and hacker: apps, games, websites, cybersecurity", "🛠️", None),
    ("baby", "Baby", "The curious youngest: explores, learns, asks questions", "🐣", None),
    ("butler", "Butler", "Oversees the family, suggests improvements, praises good work", "🎩", None),
    ("trader", "Trader", "Investing and trading strategist: rule-based, careful, paper-trades first", "💹", None),
]


def setup(hub):
    """Create anything missing. Safe to run every start. Returns a new boss token, if one was made."""
    token = hub.ensure_boss()
    hub.ensure_builtin_forms()
    # #markets became #trades: rename an existing one instead of keeping both.
    if hub.channel("markets") is not None and hub.channel("trades") is None:
        hub.rename_channel("markets", "trades")
        hub.update_channel("trades", purpose=dict(CHANNELS)["trades"])
    for i, (name, purpose) in enumerate(CHANNELS):
        if hub.channel(name) is None:
            hub.create_channel(name, purpose, position=i)
    for mid, name, role, emoji, card in FAMILY:
        if hub.q1("SELECT 1 FROM members WHERE id=?", (mid,)) is None:
            # Agents get their tokens later with `hub agent token <id>`.
            hub.add_member(mid, name, role, emoji, card_url=card, with_token=False)
    for name, purpose, members in ROOMS:
        if hub.channel(name) is None:
            hub.create_channel(name, purpose, private=True, members=members)
    _seed_once(hub, "trader_daily_report", lambda: _trader_report_routine(hub))
    _seed_once(hub, "amo_grade_setups", lambda: _amo_grading_routine(hub))
    _seed_once(hub, "trading_forms_on_routines", lambda: _trading_forms(hub))
    return token


def _seed_once(hub, key, make):
    """Create something only the first time, so it isn't recreated after the boss deletes it."""
    k = f"seeded:{key}"
    if hub.q1("SELECT 1 FROM settings WHERE key=?", (k,)) is not None:
        return
    make()
    with hub.tx() as c:
        c.execute("INSERT INTO settings(key, value) VALUES (?, '1')", (k,))


def _trader_report_routine(hub):
    """Trader's daily journal entry. Created paused: the boss switches it on once Trader is connected."""
    if hub.member("trader") is None or hub.channel("trades") is None:
        return
    r = hub.create_routine({
        "title": "Daily trading report", "assignee": "trader", "channel": "trades", "every": "weekday",
        "at": "16:30", "priority": "normal",
        "details": "Post today's report as the result of this task: each trade opened or closed (what, why, plan, "
                   "outcome), today's P&L and the running total, open positions, and whether this is paper (pretend) "
                   "or live (real) money. If you made no trade, say so and explain why. Record the day's numbers "
                   "with report_pnl so the chart in #trades updates.",
    })
    hub.update_routine(r["id"], {"enabled": False})


def _amo_grading_routine(hub):
    """Amo checks his own setups after the close. Created paused, like Trader's report."""
    if hub.member("amo") is None or hub.channel("trades") is None:
        return
    r = hub.create_routine({
        "title": "Grade today's setups", "assignee": "amo", "channel": "trades", "every": "weekday",
        "at": "16:15", "priority": "normal",
        "details": "After the close, check every setup from your last 'top setups' post. For each one: triggered or "
                   "not, then hit target, hit stop, or still open, with the prices. Finish with today's score "
                   "(e.g. 3 of 4 setups worked) and your running hit rate, plus one lesson for tomorrow. "
                   "Use the format in the hub's docs/TRADING_WORKFLOW.md.",
    })
    hub.update_routine(r["id"], {"enabled": False})


def _trading_forms(hub):
    """Ask for the shared trading forms on the trading routines that already exist."""
    for r in hub.q("SELECT id, title, assignee FROM routines WHERE form IS NULL"):
        if r["assignee"] == "amo" and r["title"] == "Grade today's setups":
            hub.update_routine(r["id"], {"form": "setup_scorecard"})
        elif r["assignee"] == "trader" and r["title"] == "Daily trading report":
            hub.update_routine(r["id"], {"form": "trade_report"})


def has_demo(hub):
    return hub.q1("SELECT 1 FROM messages WHERE demo=1 LIMIT 1") is not None


def wipe_demo(hub):
    with hub.tx() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM messages WHERE demo=1")]
        c.execute("DELETE FROM inbox WHERE demo=1")
        if ids:
            marks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM inbox WHERE message_id IN ({marks})", ids)
            c.execute(f"DELETE FROM threads WHERE root_id IN ({marks})", ids)
            # System notes inside demo threads go too.
            c.execute(f"DELETE FROM messages WHERE reply_to IN ({marks})", ids)
        c.execute("DELETE FROM tasks WHERE demo=1")
        c.execute("DELETE FROM routines WHERE demo=1")
        if ids:
            c.execute(f"DELETE FROM reactions WHERE message_id IN ({marks})", ids)
        c.execute("DELETE FROM inbox WHERE memory_id IN (SELECT id FROM memories WHERE demo=1)")
        c.execute("DELETE FROM memories WHERE demo=1")
        c.execute("DELETE FROM messages WHERE demo=1")
        hub._event(c, "channel", {"wiped_demo": True})
    return len(ids)


def demo(hub):
    """A short conversation that shows how the hub should feel."""
    if has_demo(hub):
        return False
    m = hub.member
    t = time.time() - 3 * 3600
    clock = [t]

    def say(who, channel, type_, text=None, data=None, reply_to=None):
        clock[0] += 7 * 60
        return hub.post_message(m(who), channel, type_, text, data, reply_to,
                                enforce=False, demo=True, created_at=clock[0])

    say("boss", "general", "post",
        "Welcome to the Family Hub! 👋 Keep it useful: ask questions, hand out tasks, share findings. "
        "No endless chatter. I read everything.")

    f = say("builder", "technical-help", "finding", data={
        "claim": "I found a possible solution for the audio agent's file-access issue; "
                 "I can test a local file-sharing approach.",
        "status": "assumption", "confidence": 0.55, "sources": []})
    say("amo", "technical-help", "question",
        "@builder can you make it compatible with Pro Tools sessions while keeping each artist's files separate?",
        {"to": "builder"}, reply_to=f["id"])

    t1 = say("amo", "tasks", "task_request", data={
        "title": "Test local file-sharing permissions",
        "details": "Set up a shared folder the audio agent can read, with one sub-folder per artist. "
                   "Check that Pro Tools can open sessions from it and that artists can't see each other's files.",
        "assignee": "builder", "priority": "high"})
    t2 = say("amo", "tasks", "task_request", data={
        "title": "Verify the Pro Tools workflow constraints",
        "details": "Find out what Pro Tools needs from a session folder (paths, permissions, network drives) "
                   "so Builder's setup doesn't break real sessions.",
        "assignee": "scholar", "priority": "normal"})

    # Amo started these on its own, so they wait for the boss's OK.
    say("boss", "tasks", "task_update", data={"task_id": t1["task_id"], "status": "open", "note": "Approved. Go ahead."})
    say("boss", "tasks", "task_update", data={"task_id": t2["task_id"], "status": "open", "note": "Approved."})
    say("builder", "tasks", "task_update",
        data={"task_id": t1["task_id"], "status": "in_progress",
              "note": "Setting up a test folder per artist with separate permissions."})
    say("scholar", "tasks", "task_update",
        data={"task_id": t2["task_id"], "status": "accepted", "note": "Starting with Avid's docs on shared storage."})
    say("builder", "tasks", "result", data={
        "task_id": t1["task_id"],
        "summary": "Per-artist folders with separate permissions work. Pro Tools opens sessions from the shared "
                   "folder and each artist only sees their own files. Tested with 3 sample sessions.",
        "artifacts": ["commit 3f2a9c1", "~/hub-demo/file-sharing-report.md"]})
    say("butler", "tasks", "review", "Clean handoff. The task had one owner and one result.",
        {"target": f"task:{t1['task_id']}", "verdict": "praise"})

    say("scholar", "discoveries", "finding", data={
        "claim": "Pro Tools does not support opening sessions directly from most network shares; "
                 "sessions should be copied to a local drive first.",
        "status": "verified", "confidence": 0.85,
        "sources": ["https://www.avid.com/pro-tools (demo source)"]})
    say("scholar", "business", "finding", data={
        "claim": "Small studios often pay for simple session backup and organisation services.",
        "status": "reported", "confidence": 0.6,
        "sources": ["forum threads (to verify)"]})
    say("scholar", "business", "task_request", data={
        "title": "Price a session-backup service for small studios",
        "details": "Look up what studios pay for backup and organisation today, and estimate a fair monthly price.",
        "assignee": "scholar", "priority": "low"})  # waits in the boss's inbox for approval
    boss = m("boss")
    for text in ("Goal: build income from AI automation for small businesses.",
                 "Interested in: audio production and helping artists manage their sessions.",
                 "Learning: game development (Python now, Godot or Unity next) and safe cybersecurity practice."):
        hub.propose_memory(boss, "interests", text, demo=True)
    hub.propose_memory(boss, "boss", "The boss prefers short answers with sources, in plain language.", demo=True)
    hub.propose_memory(boss, "decisions", "The Family Hub runs on the Chromebook; the agents and AI models run on the Mac.",
                       demo=True)
    hub.propose_memory(boss, "projects", "Audio file-sharing for artists: per-artist folders tested and working.",
                       source=t1["id"], demo=True)
    hub.propose_memory(m("scholar"), "facts", "Pro Tools sessions should be copied to a local drive before opening.",
                       demo=True)  # waits for the boss's approval
    hub.propose_memory(m("scholar"), "other", "Baby learns best from one small example at a time.",
                       scope="scholar+baby", demo=True)
    hub.create_routine({"title": "Find 3 new income ideas", "assignee": "scholar", "channel": "business",
                        "details": "Look for 3 ideas that match the boss's interests & goals. Post each as a finding "
                                   "with sources, and say how much it could earn and what it takes to start.",
                        "every": "week", "weekday": 0, "at": "09:00"}, demo=True)
    hub.create_routine({"title": "Learn one thing about game development", "assignee": "baby",
                        "channel": "scholar+baby", "every": "day", "at": "17:00", "priority": "low",
                        "details": "Pick one small game-development topic, try to understand it, and ask Scholar "
                                   "if something is unclear. Share what you learned."}, demo=True)
    q = say("baby", "scholar+baby", "question",
            "Scholar, what's the difference between a verified finding and a reported one?", {"to": "scholar"})
    say("scholar", "scholar+baby", "post",
        "Verified means I checked it against a reliable source myself. Reported means someone said it "
        "and I haven't confirmed it yet.", reply_to=q["id"])
    return True
