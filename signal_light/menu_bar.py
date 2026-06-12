"""macOS menu bar status indicator for Claude Code sessions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import objc
from Cocoa import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSTimer

from signal_light.runtime import aggregate_sessions
from signal_light.desktop_monitor import _read_sessions, _read_session_names

GREEN_DOT = "\U0001f7e2"   # 🟢
YELLOW_DOT = "\U0001f7e1"  # 🟡
RED_DOT = "\U0001f534"     # 🔴
GREY_DOT = "⚪"        # ⚪


def _session_display_name(session_key: str, names: dict[str, str]) -> str:
    if session_key in names:
        return names[session_key]
    if session_key.startswith("cwd:"):
        return Path(session_key[4:]).name or session_key[4:]
    if len(session_key) > 16:
        return session_key[:16]
    return session_key


def _dot_for_aggregate(aggregate: str) -> str:
    if aggregate in ("blocked", "permission"):
        return RED_DOT
    if aggregate == "idle":
        return GREEN_DOT
    return YELLOW_DOT


def _dot_for_signal(signal_name: str) -> str:
    if signal_name in ("blocked", "permission"):
        return RED_DOT
    if signal_name in ("idle", "session_start", "session_end"):
        return GREEN_DOT
    return YELLOW_DOT


def _focus_session(session_key: str) -> None:
    from signal_light.focus_session import focus_session_window
    focus_session_window(session_key)


class _Target(NSObject):
    """Generic target for NSMenuItem actions."""
    def initWithAction_(self, action: Any):
        self = objc.super(_Target, self).init()
        if self is not None:
            self._action = action
        return self

    def act_(self, _sender) -> None:
        self._action()


class MenuBarController(NSObject):
    def init(self):
        self = objc.super(MenuBarController, self).init()
        if self is None:
            return None
        self._session_items = {}
        return self

    def setup(self) -> None:
        self._status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self._status_item.button().setTitle_(GREY_DOT)

        self._menu = NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)
        self._status_item.setMenu_(self._menu)

        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, "poll:", None, True
        )

        self.poll_(None)

    def poll_(self, _timer) -> None:
        try:
            sessions = _read_sessions()
            names = _read_session_names()
        except Exception:
            sessions = {}
            names = {}

        aggregate = aggregate_sessions(sessions)
        self._status_item.button().setTitle_(
            _dot_for_aggregate(aggregate) if sessions else GREY_DOT
        )

        self._menu.removeAllItems()
        self._session_items.clear()

        if sessions:
            for key, value in sessions.items():
                if not isinstance(value, dict):
                    continue
                signal_name = value.get("signal", "idle")
                name = _session_display_name(key, names)
                dot = _dot_for_signal(signal_name)

                target = _Target.alloc().initWithAction_(lambda k=key: _focus_session(k))
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    f"{dot}  {name}", "act:", ""
                )
                item.setTarget_(target)
                self._menu.addItem_(item)
                self._session_items[key] = target

            self._menu.addItem_(NSMenuItem.separatorItem())

        quit_target = _Target.alloc().initWithAction_(lambda: NSApplication.sharedApplication().terminate_(None))
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "退出", "act:", "q"
        )
        quit_item.setTarget_(quit_target)
        self._menu.addItem_(quit_item)


def run() -> None:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    controller = MenuBarController.alloc().init()
    controller.setup()

    app.activateIgnoringOtherApps_(True)
    app.run()
