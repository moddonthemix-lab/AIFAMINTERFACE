"""The `hub` command.

  hub serve                 start the hub (web page + API) on port 8770
  hub post "#general" "hi"  post as the boss (also: --type question --to scholar)
  hub tasks                 list tasks
  hub doctor                check that everything is healthy
  hub agent add/token/list/remove/pause/unpause
  hub token boss [--reset]  show (or replace) your boss token
  hub seed --demo | --wipe-demo
"""

import argparse
import json
import socket
import sqlite3
import sys
import urllib.request

from . import __version__, config, seed
from .core import Hub, HubError


def open_hub():
    config.load_dotenv()
    hub = Hub(config.home())
    token = seed.setup(hub)
    first_run = token is not None
    if first_run and not seed.has_demo(hub):
        seed.demo(hub)
    return hub, token


def cmd_serve(args):
    from .server import make_server

    host = args.host or config.host()
    port = args.port or config.port()
    hub, new_token = open_hub()
    if host not in ("127.0.0.1", "localhost", "::1") and not hub.member("boss")["token_hash"]:
        print("Refusing to listen on the network without a boss token. Run: hub token boss --reset")
        return 1
    hub.prune_events()
    from . import scheduler
    scheduler.start(hub)
    try:
        server = make_server(hub, host, port)
    except OSError as e:
        print(f"Could not start on {host}:{port}: {e}. Is the hub already running? Try: hub doctor")
        return 1
    token = new_token or hub.read_boss_token()
    print(f"Family Hub {__version__} is running at http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    if new_token:
        print("\nThis is the first start. Your boss token (keep it private):")
        print(f"  {new_token}")
    if token and host in ("127.0.0.1", "localhost"):
        print(f"\nOne-click login on this Mac:  http://localhost:{port}/#token={token}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Listening on your network. Every request needs a token.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever(poll_interval=1)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def _show(msg):
    who = msg["author"]
    print(f"#{msg['id']} [{msg['type']}] in #{msg['channel']} by {who}")


def cmd_post(args):
    hub, _ = open_hub()
    boss = hub.member("boss")
    data = {}
    if args.type == "question" and args.to:
        data["to"] = args.to
    if args.type == "task_request":
        data = {"title": args.title or args.text[:80], "details": args.text, "assignee": args.to,
                "priority": args.priority}
    try:
        msg = hub.post_message(boss, args.channel, args.type, args.text, data, args.reply_to)
    except HubError as e:
        print(f"Not posted: {e.message}")
        return 1
    _show(msg)
    return 0


def cmd_tasks(args):
    hub, _ = open_hub()
    tasks = hub.tasks(hub.member("boss"), args.assignee, args.status)
    if not tasks:
        print("No tasks.")
    for t in tasks:
        print(f"#{t['id']:<4} {t['status']:<12} {t['priority']:<7} @{t['assignee']:<9} {t['title']}")
    return 0


def cmd_approve(args):
    hub, _ = open_hub()
    try:
        msg = hub.decide_task(hub.member("boss"), args.task_id, not args.decline, args.note or "")
    except HubError as e:
        print(e.message)
        return 1
    print(f"Task {args.task_id} is now {msg['task']['status']}.")
    return 0


def _hub_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            return bool(json.loads(r.read()).get("ok"))
    except OSError:
        return False


def cmd_backup(args):
    hub, _ = open_hub()
    if args.list:
        backups = hub.backups()
        print(f"Backups in {hub.backup_dir()}:" if backups else f"No backups yet in {hub.backup_dir()}.")
        for b in backups:
            print(f"  {b['name']}  {b['size_kb']} KB")
        return 0
    b = hub.backup()
    print(f"Saved {hub.backup_dir() / b['name']} ({b['size_kb']} KB).")
    return 0


def cmd_restore(args):
    from . import backup
    config.load_dotenv()
    if _hub_running(config.port()):
        print("The hub is running. Stop it first, restore, then start it again:\n"
              "  python3 -m hub autostart --off\n"
              f"  python3 -m hub restore {args.file}\n"
              "  python3 -m hub autostart --host 0.0.0.0\n"
              "(If you started it with `hub serve` instead, press Ctrl+C there.)")
        return 1
    path = args.file
    if "/" not in path:
        path = str(Hub(config.home()).backup_dir() / path)
    try:
        safety = backup.restore(config.home(), path)
    except Exception as e:  # noqa: BLE001
        print(f"Could not restore: {e}")
        return 1
    print(f"Restored {path}.\nYour previous database was kept as {safety}.\nNow start the hub again.")
    return 0


def cmd_doctor(args):
    config.load_dotenv()
    ok = True

    def check(label, good, hint="", warn=False):
        nonlocal ok
        if not warn:
            ok &= bool(good)
        mark = "✅" if good else "⚠️ " if warn else "❌"
        print(f"  {mark} {label}" + (f"  → {hint}" if hint and not good else ""))

    print("Family Hub doctor")
    check(f"Python {sys.version.split()[0]} (need 3.9+)", sys.version_info >= (3, 9))
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        fts = True
    except sqlite3.Error:
        fts = False
    check(f"SQLite {sqlite3.sqlite_version} with full-text search", fts,
          "your Python's SQLite lacks FTS5; install Python from python.org or Homebrew")
    try:
        hub, _ = open_hub()
        mode = hub.q1("PRAGMA journal_mode")[0]
        check(f"Database at {hub.db_path}", True)
        check(f"WAL mode ({mode})", mode == "wal")
        check("Boss token exists", bool(hub.read_boss_token()), "run: hub token boss --reset")
        missing = [m["id"] for m in hub.members() if m["kind"] == "agent" and not m["has_token"]]
        check("All agents have tokens", not missing,
              f"no token yet for {', '.join(missing)} (fine until you connect them): hub agent token <id>", warn=True)
        age = hub.last_backup_age()
        check("Backups" + (f": last one {age / 3600:.0f} h ago in {hub.backup_dir()}" if age is not None else ""),
              age is not None and age < 2 * 86400,
              "none recent; the hub makes one daily while running, or run: python3 -m hub backup", warn=True)
        size = len(hub.brief(hub.member("boss")))
        check(f"/api/brief is {size} bytes (must be under 1024)", size < 1024)
    except Exception as e:  # noqa: BLE001
        check("Database opens", False, str(e))
        return 1
    port, host = config.port(), config.host()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            running = json.loads(r.read()).get("ok")
    except OSError:
        running = False
    if running:
        check(f"Hub is running on port {port}", True)
    else:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            free = True
        except OSError:
            free = False
        finally:
            s.close()
        check(f"Hub not running; port {port} is {'free' if free else 'used by something else'}", free,
              "set HUB_PORT to another port")
    check(f"Network: listening on {host}", host in ("127.0.0.1", "localhost") or bool(hub.read_boss_token()))
    print("All good." if ok else "Some checks need attention (see above).")
    return 0 if ok else 1


def cmd_agent(args):
    hub, _ = open_hub()
    try:
        if args.action == "list":
            for m in hub.members():
                flags = (" (paused)" if m["paused"] else "") + ("" if m["has_token"] else " (no token)")
                print(f"{m['emoji']} {m['id']:<10} {m['status']:<8} {m['role']}{flags}")
        elif args.action == "add":
            token = hub.add_member(args.id, args.name, args.role or "", args.emoji or "🤖")
            print(f"Added {args.id}. Their token (shown once, give it to the agent):\n  {token}")
        elif args.action == "token":
            if args.id == "boss":
                print("Use: hub token boss --reset")
                return 1
            print(f"New token for {args.id} (the old one stops working):\n  {hub.reset_token(args.id)}")
        elif args.action == "remove":
            hub.remove_member(args.id)
            print(f"Removed {args.id}. Their messages stay.")
        elif args.action in ("pause", "unpause"):
            hub.set_agent_paused(args.id, args.action == "pause")
            print(f"{args.id} is now {'paused' if args.action == 'pause' else 'active'}.")
    except HubError as e:
        print(e.message)
        return 1
    return 0


def cmd_token(args):
    hub, _ = open_hub()
    token = hub.reset_token("boss") if args.reset else hub.read_boss_token()
    print(token)
    return 0


def cmd_seed(args):
    hub, _ = open_hub()
    if args.wipe_demo:
        n = seed.wipe_demo(hub)
        print(f"Removed the demo conversation ({n} messages).")
    elif args.demo:
        print("Added the demo conversation." if seed.demo(hub) else "The demo conversation is already there.")
    else:
        print("Channels and family are set up.")
    return 0


def cmd_autostart(args):
    from . import autostart
    config.load_dotenv()
    if args.off:
        return autostart.uninstall()
    if args.status:
        return autostart.status()
    return autostart.install(args.host, show=args.show)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m hub", description="The Family Hub")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the hub")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("post", help="post a message as the boss")
    s.add_argument("channel")
    s.add_argument("text")
    s.add_argument("--type", default="post", choices=["post", "question", "task_request"])
    s.add_argument("--to", help="who a question or task is for")
    s.add_argument("--title", help="task title (task_request)")
    s.add_argument("--priority", default="normal", choices=["low", "normal", "high"])
    s.add_argument("--reply-to", type=int)
    s.set_defaults(fn=cmd_post)

    s = sub.add_parser("tasks", help="list tasks")
    s.add_argument("--assignee")
    s.add_argument("--status", help="e.g. open,in_progress")
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("approve", help="approve (or --decline) a proposed task")
    s.add_argument("task_id", type=int)
    s.add_argument("--decline", action="store_true")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("backup", help="back up the hub now (or --list backups)")
    s.add_argument("--list", action="store_true")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("restore", help="restore a backup (stop the hub first)")
    s.add_argument("file", help="a backup name from `backup --list`, or a path")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("doctor", help="check the setup")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("agent", help="manage agents")
    s.add_argument("action", choices=["list", "add", "token", "remove", "pause", "unpause"])
    s.add_argument("id", nargs="?")
    s.add_argument("--name")
    s.add_argument("--role")
    s.add_argument("--emoji")
    s.set_defaults(fn=cmd_agent)

    s = sub.add_parser("token", help="show the boss token")
    s.add_argument("who", choices=["boss"])
    s.add_argument("--reset", action="store_true", help="make a new one (the old one stops working)")
    s.set_defaults(fn=cmd_token)

    s = sub.add_parser("autostart", help="run the hub in the background and start it automatically")
    s.add_argument("--off", action="store_true", help="stop it and turn autostart off")
    s.add_argument("--status", action="store_true", help="is it on and running?")
    s.add_argument("--host", help="0.0.0.0 to let your phone, Mac or Pi reach it over Wi-Fi")
    s.add_argument("--show", action="store_true", help="only print what would be installed")
    s.set_defaults(fn=cmd_autostart)

    s = sub.add_parser("seed", help="demo data")
    s.add_argument("--demo", action="store_true")
    s.add_argument("--wipe-demo", action="store_true")
    s.set_defaults(fn=cmd_seed)

    args = p.parse_args(argv)
    if args.cmd == "agent" and args.action != "list" and not args.id:
        p.error("this needs an agent id, e.g. hub agent token builder")
    if args.cmd == "post" and args.type == "task_request" and not args.to:
        p.error("a task needs --to <agent>")
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
