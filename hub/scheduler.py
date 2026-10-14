"""Background chores while `hub serve` runs: routines (and daily backups)."""

import sys
import threading

CHECK_EVERY = 30  # seconds


def chores(hub):
    hub.run_due_routines()
    if hasattr(hub, "backup_if_due"):
        hub.backup_if_due()


def start(hub):
    stop = threading.Event()

    def loop():
        while not stop.wait(CHECK_EVERY):
            try:
                chores(hub)
            except Exception as e:  # keep going; report in the log
                sys.stderr.write(f"hub: background chores failed: {e!r}\n")

    try:
        chores(hub)  # catch up right away (e.g. routines missed while the hub was off)
    except Exception as e:
        sys.stderr.write(f"hub: background chores failed: {e!r}\n")
    threading.Thread(target=loop, name="hub-scheduler", daemon=True).start()
    return stop
