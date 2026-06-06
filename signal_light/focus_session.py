"""Helpers to focus the terminal tab/window of a running Claude Code session.

Identifies the specific Terminal.app window by matching the TTY device of
the Claude process with the tty of Terminal windows via AppleScript.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"


def _find_pid_for_session(session_key: str) -> int | None:
    """Find the PID of a Claude Code process matching the session key."""
    if not CLAUDE_SESSIONS_DIR.is_dir():
        return None
    for f in CLAUDE_SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        sid = data.get("sessionId", "")
        cwd = data.get("cwd", "")
        pid = data.get("pid")
        if session_key == sid or session_key == f"cwd:{cwd}":
            if isinstance(pid, int) and pid > 0:
                return pid
    return None


def _process_tty(pid: int) -> str:
    """Return the controlling terminal device name, e.g. 'ttys000'."""
    try:
        result = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True, text=True,
        )
        return result.stdout.strip()
    except OSError:
        return ""


def focus_session_window(session_key: str) -> None:
    """Bring the terminal tab/window hosting the given session to the front."""
    pid = _find_pid_for_session(session_key)
    if pid is None:
        return

    tty = _process_tty(pid)
    if not tty:
        return

    # Normalize tty: ps returns "ttys000", AppleScript returns "/dev/ttys000"
    tty_short = tty.replace("/dev/", "")

    # Step 1: Activate Terminal.app to trigger macOS space switch
    subprocess.run(["open", "-a", "Terminal"], capture_output=True)
    time.sleep(0.05)

    # Step 2: select the matching tab inside Terminal
    select_script = f'''
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                set tty_name to tty of t
                if tty_name ends with "{tty_short}" then
                    set selected of t to true
                    set index of w to 1
                end if
            end repeat
        end repeat
    end tell
    '''
    subprocess.run(["osascript", "-e", select_script], capture_output=True)

    # Step 3: use AXRaise to force focus (needs Accessibility permission)
    raise_script = '''
    tell application "System Events"
        tell process "Terminal"
            set frontmost to true
            perform action "AXRaise" of window 1
        end tell
    end tell
    '''
    subprocess.run(["osascript", "-e", raise_script], capture_output=True)
