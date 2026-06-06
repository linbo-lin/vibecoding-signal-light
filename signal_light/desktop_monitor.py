"""Desktop monitor that bridges the shared session state to the UI window.

Polls /private/tmp/signal-light/sessions.json and ~/.claude/sessions/*.json,
resolving session display names (including /rename'd names).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from signal_light.runtime import (
    aggregate_sessions,
    _read_session_state,
    _prune_sessions,
)


STATE_DIR = Path(os.environ.get("SIGNAL_LIGHT_STATE_DIR", "/private/tmp/signal-light"))
SESSION_FILE = STATE_DIR / "sessions.json"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
POLL_INTERVAL_MS = 1000


def _read_sessions() -> dict:
    state = _read_session_state()
    sessions = state.get("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
    now = time.time()
    _prune_sessions(sessions, now)
    return sessions


def _read_session_names() -> dict[str, str]:
    """Build a {session_id: name, cwd: name} map from Claude Code session files."""
    names: dict[str, str] = {}
    if not CLAUDE_SESSIONS_DIR.is_dir():
        return names
    for f in CLAUDE_SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        sid = data.get("sessionId")
        name = data.get("name", "")
        cwd = data.get("cwd", "")
        if sid and name:
            names[sid] = name
        if cwd and name:
            names[f"cwd:{cwd}"] = name
    return names


def bootstrap_existing_sessions() -> None:
    """No-op: sessions are registered naturally when hook events fire."""
    pass


class DesktopMonitor:
    """Watches sessions.json and drives a DesktopSignalWindow."""

    def __init__(self, window):
        self._window = window
        self._after_id = None

    def start(self):
        bootstrap_existing_sessions()
        self._schedule_poll()

    def _schedule_poll(self):
        self._after_id = self._window.after(POLL_INTERVAL_MS, self._poll)

    def _poll(self):
        try:
            sessions = _read_sessions()
            names = _read_session_names()
        except Exception:
            sessions = {}
            names = {}

        aggregate = aggregate_sessions(sessions)
        self._window.update_state(aggregate, sessions, names)
        self._schedule_poll()
