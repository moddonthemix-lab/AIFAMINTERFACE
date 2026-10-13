"""Start the hub automatically in the background, and restart it if it stops.

  macOS:  a LaunchAgent  (~/Library/LaunchAgents/com.familyhub.hub.plist), starts when you log in
  Linux:  a systemd user service (~/.config/systemd/user/familyhub.service): Chromebook, Raspberry Pi
"""

import os
import platform
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import config

LABEL = "com.familyhub.hub"
UNIT = "familyhub.service"


def settings(host=None):
    """The environment the background hub runs with: what you'd get in this terminal right now."""
    env = {
        "HUB_HOME": str(config.home()),
        "HUB_HOST": host or config.host(),
        "HUB_PORT": str(config.port()),
        "PYTHONUNBUFFERED": "1",
    }
    return env


def workdir():
    return str(Path(__file__).resolve().parent.parent)


def log_path(env):
    return str(Path(env["HUB_HOME"]) / "hub.log")


def launchd_plist(env):
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "hub", "serve"],
        "WorkingDirectory": workdir(),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": log_path(env),
        "StandardErrorPath": log_path(env),
    }).decode()


def systemd_unit(env):
    lines = [
        "[Unit]",
        "Description=Family Hub (private forum for the AI family)",
        "After=network.target",
        "",
        "[Service]",
        f"ExecStart={sys.executable} -m hub serve",
        f"WorkingDirectory={workdir()}",
        *[f'Environment="{k}={v}"' for k, v in env.items()],
        "Restart=always",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _run(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _wait_healthy(port, seconds=8):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def target():
    system = platform.system()
    if system == "Darwin":
        return "mac", Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if system == "Linux":
        return "linux", Path.home() / ".config" / "systemd" / "user" / UNIT
    return None, None


def install(host=None, show=False):
    kind, path = target()
    env = settings(host)
    if kind is None:
        print("Autostart works on macOS and Linux (Chromebook, Raspberry Pi). On this system, run: python3 -m hub serve")
        return 1
    content = launchd_plist(env) if kind == "mac" else systemd_unit(env)
    if show:
        print(f"# would write {path}\n{content}")
        return 0
    if kind == "linux" and not shutil.which("systemctl"):
        print("This Linux has no systemd, so autostart can't be set up here. Run: python3 -m hub serve")
        return 1
    Path(env["HUB_HOME"]).mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

    if kind == "mac":
        _run("launchctl", "unload", str(path))
        code, out = _run("launchctl", "load", "-w", str(path))
    else:
        _run("systemctl", "--user", "daemon-reload")
        code, out = _run("systemctl", "--user", "enable", UNIT)
        if code == 0:
            code, out = _run("systemctl", "--user", "restart", UNIT)
        # Keep running after you log out, and start at boot (Raspberry Pi). Harmless if not allowed.
        if shutil.which("loginctl"):
            _run("loginctl", "enable-linger", os.environ.get("USER", ""))
    if code != 0:
        print(f"Could not start the background hub:\n{out}")
        return 1

    port = env["HUB_PORT"]
    if _wait_healthy(int(port)):
        print(f"✅ The hub now runs in the background and starts by itself (listening on {env['HUB_HOST']}:{port}).")
    else:
        print(f"Set up, but the hub didn't answer yet. Check the log: {log_path(env)}")
    print(f"   Open:        http://localhost:{port}")
    print("   Your token:  python3 -m hub token boss")
    print(f"   Log file:    {log_path(env)}")
    print("   To stop it:  python3 -m hub autostart --off")
    if kind == "linux":
        print("   On a Chromebook: after a restart, open the Terminal app once so Linux starts; the hub follows.")
    return 0


def uninstall():
    kind, path = target()
    if kind is None:
        print("Nothing to do on this system.")
        return 0
    if kind == "mac":
        if path.exists():
            _run("launchctl", "unload", "-w", str(path))
    elif shutil.which("systemctl"):
        _run("systemctl", "--user", "disable", "--now", UNIT)
    if path.exists():
        path.unlink()
        if kind == "linux" and shutil.which("systemctl"):
            _run("systemctl", "--user", "daemon-reload")
        print("Autostart is off and the background hub is stopped. Your data is untouched.")
    else:
        print("Autostart wasn't on.")
    return 0


def status():
    kind, path = target()
    if kind is None or not path.exists():
        print("Autostart is off.")
        return 0
    if kind == "mac":
        code, _ = _run("launchctl", "list", LABEL)
        running = code == 0
    else:
        code, out = _run("systemctl", "--user", "is-active", UNIT)
        running = out.strip() == "active"
    print(f"Autostart is on ({path}). The background hub is {'running' if running else 'NOT running'}.")
    return 0
