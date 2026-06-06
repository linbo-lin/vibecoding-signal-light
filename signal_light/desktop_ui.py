"""Desktop floating traffic-light window for Claude Code session monitoring.

Renders a frameless, always-on-top window showing the aggregate status
and per-session indicators, driven by the shared sessions.json state.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

# ── colour constants ──────────────────────────────────────────────
GREEN_ON = "#00E676"
YELLOW_ON = "#FFD600"
RED_ON = "#FF1744"

GREY_OFF = "#2a2a2a"
BG = "#1a1a1c"
SURFACE = "#242426"
TEXT_PRIMARY = "#e0e0e0"

RADIUS_SMALL = 8
ROW_HEIGHT = 24
PADDING = 12
DOTS_WIDTH = 72   # 3 × (radius*2+4) + 3×2 padding + 6 gap to name
MIN_WIDTH = 80
MAX_WIDTH = 400

# ── signal → colour helpers ───────────────────────────────────────

RED_SIGNALS = {"blocked", "permission"}
YELLOW_SIGNALS = {"thinking", "working", "tool_done", "attention", "done"}


def signal_colour(signal_name: str) -> str:
    if signal_name in RED_SIGNALS:
        return RED_ON
    if signal_name in YELLOW_SIGNALS:
        return YELLOW_ON
    return GREEN_ON


# ── helpers ───────────────────────────────────────────────────────


def _session_display_name(session_key: str, names: dict[str, str]) -> str:
    if session_key in names:
        return names[session_key]
    if session_key.startswith("cwd:"):
        return Path(session_key[4:]).name or session_key[4:]
    if len(session_key) > 16:
        return session_key[:16]
    return session_key


def _read_position(path: Path) -> tuple[int, int]:
    try:
        data = json.loads(path.read_text())
        return data.get("x", 100), data.get("y", 100)
    except (FileNotFoundError, json.JSONDecodeError):
        return 100, 100


def _write_position(path: Path, x: int, y: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"x": x, "y": y}, ensure_ascii=False))


# ── traffic light canvas widget ───────────────────────────────────


class TrafficDot(tk.Canvas):
    """A single coloured dot that can be on or off."""

    def __init__(self, parent, radius: int = RADIUS_SMALL, **kw):
        size = radius * 2 + 4
        super().__init__(parent, width=size, height=size,
                         bg=BG, highlightthickness=0, **kw)
        self.radius = radius
        self._dot = self.create_oval(
            2, 2, size - 2, size - 2, fill=GREY_OFF, outline=""
        )

    def set(self, colour: str):
        self.itemconfig(self._dot, fill=colour)


class TrafficLightRow(tk.Frame):
    """A single row: three small dots + clickable session name."""

    def __init__(self, parent, on_click=None):
        super().__init__(parent, bg=BG, height=ROW_HEIGHT)
        self.pack_propagate(False)
        self.session_key = ""
        self._on_click = on_click

        self.dots = (
            TrafficDot(self, RADIUS_SMALL),
            TrafficDot(self, RADIUS_SMALL),
            TrafficDot(self, RADIUS_SMALL),
        )
        for d in self.dots:
            d.pack(side=tk.LEFT, padx=1)

        self.name_label = tk.Label(
            self, text="", fg=TEXT_PRIMARY, bg=BG,
            font=tkfont.Font(size=11, underline=True), anchor="w",
            cursor="pointinghand",
        )
        self.name_label.pack(side=tk.LEFT, padx=(6, 0))
        self.name_label.bind("<ButtonRelease-1>", self._on_name_click)
        for d in self.dots:
            d.bind("<ButtonRelease-1>", self._on_name_click)

    def _on_name_click(self, event):
        if self._on_click and self.session_key:
            self._on_click(self.session_key)

    def update(self, name: str, signal: str, key: str = ""):
        colour = signal_colour(signal)
        self.session_key = key
        self.name_label.config(text=name)
        self.dots[0].set(GREEN_ON if colour == GREEN_ON else GREY_OFF)
        self.dots[1].set(YELLOW_ON if colour == YELLOW_ON else GREY_OFF)
        self.dots[2].set(RED_ON if colour == RED_ON else GREY_OFF)


# ── main desktop window ───────────────────────────────────────────


class DesktopSignalWindow(tk.Tk):
    """Frameless floating window displaying Claude Code session status."""

    def __init__(self):
        super().__init__()
        self.title("信号灯")
        self.configure(bg=BG)
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        self.after(100, self._join_all_spaces)

        # position
        self._pos_file = Path.home() / ".claude" / "signal-light-ui-position.json"
        x, y = _read_position(self._pos_file)
        self.geometry(f"{MIN_WIDTH}x20+{x}+{y}")
        self.minsize(MIN_WIDTH, 20)
        self._offset_x = 0
        self._offset_y = 0

        # ── session list ──
        self.session_frame = tk.Frame(self, bg=BG)
        self.session_frame.pack(fill=tk.BOTH, expand=True, padx=PADDING, pady=(6, 6))

        # drag anywhere on the window
        self.bind("<Button-1>", self._drag_start)
        self.bind("<B1-Motion>", self._drag_move)
        self.session_frame.bind("<Button-1>", self._drag_start)
        self.session_frame.bind("<B1-Motion>", self._drag_move)

        # ── right-click menu ──
        self._menu = tk.Menu(self, tearoff=0, bg=SURFACE, fg=TEXT_PRIMARY)
        self._menu.add_command(label="退出", command=self._quit)
        self.bind("<Button-2>", self._popup)
        self.bind("<Control-Button-1>", self._popup)
        self.bind("<Button-3>", self._popup)

        # ── close handling ──
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.bind("<Command-w>", lambda _e: self._quit())
        self.bind("<Escape>", lambda _e: self._quit())

        # ── state ──
        self._session_widgets: dict[str, TrafficLightRow] = {}
        self._visible = False
        self._last_geometry = ""

        self.attributes('-alpha', 0.0)

    # ── dragging ────────────────────────────────────────────────

    def _drag_start(self, event):
        self._offset_x = event.x
        self._offset_y = event.y

    def _drag_move(self, event):
        self.geometry(f"+{event.x_root - self._offset_x}+{event.y_root - self._offset_y}")

    def _save_position(self):
        geo = self.geometry()
        parts = geo.split("+")
        if len(parts) == 3:
            _write_position(self._pos_file, int(parts[1]), int(parts[2]))

    # ── menu / quit ─────────────────────────────────────────────

    def _popup(self, event):
        self._menu.post(event.x_root, event.y_root)

    def _quit(self):
        self._save_position()
        self.destroy()

    # ── public update API (called by monitor) ───────────────────

    def update_state(self, aggregate: str, sessions: dict, names: dict[str, str] | None = None):
        self._render_sessions(sessions, names or {})

    # ── internal render helpers ─────────────────────────────────

    def _join_all_spaces(self):
        """Set the NSWindow to appear on every macOS Space."""
        try:
            libobjc = ctypes.cdll.LoadLibrary(
                ctypes.util.find_library("objc") or "/usr/lib/libobjc.A.dylib"
            )
        except Exception:
            return

        libobjc.objc_getClass.restype = ctypes.c_void_p
        libobjc.objc_getClass.argtypes = [ctypes.c_char_p]
        libobjc.sel_registerName.restype = ctypes.c_void_p
        libobjc.sel_registerName.argtypes = [ctypes.c_char_p]

        msg_send = libobjc.objc_msgSend
        msg_send.restype = ctypes.c_void_p
        msg_send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        try:
            NSApp = msg_send(
                libobjc.objc_getClass(b"NSApplication"),
                libobjc.sel_registerName(b"sharedApplication"),
            )
            if not NSApp:
                return

            # Try mainWindow, then keyWindow, then first window
            win = msg_send(NSApp, libobjc.sel_registerName(b"mainWindow"))
            if not win:
                win = msg_send(NSApp, libobjc.sel_registerName(b"keyWindow"))
            if not win:
                windows = msg_send(NSApp, libobjc.sel_registerName(b"windows"))
                if windows:
                    win = msg_send(windows, libobjc.sel_registerName(b"firstObject"))

            if not win:
                return

            msg_send_int = libobjc.objc_msgSend
            msg_send_int.restype = None
            msg_send_int.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_longlong]
            # NSWindowCollectionBehaviorCanJoinAllSpaces = 1
            msg_send_int(win, libobjc.sel_registerName(b"setCollectionBehavior:"), 1)
        except Exception:
            pass

    def _focus_session(self, session_key: str):
        from signal_light.focus_session import focus_session_window
        focus_session_window(session_key)

    def _render_sessions(self, sessions: dict, names: dict[str, str]):
        seen = set()
        count = 0
        font = tkfont.Font(size=11)

        for i, (key, value) in enumerate(sessions.items()):
            if not isinstance(value, dict):
                continue
            signal_name = value.get("signal", "idle")
            seen.add(key)
            count += 1
            name = _session_display_name(key, names)

            if key not in self._session_widgets:
                row = TrafficLightRow(self.session_frame, on_click=self._focus_session)
                row.pack(fill=tk.X, pady=1)
                self._session_widgets[key] = row
            else:
                row = self._session_widgets[key]
                row.pack_forget()
                row.pack(fill=tk.X, pady=1)

            row.update(name, signal_name, key=key)

        # remove stale rows
        for key in list(self._session_widgets):
            if key not in seen:
                self._session_widgets[key].destroy()
                del self._session_widgets[key]

        if count == 0:
            if self._visible:
                self.attributes('-alpha', 0.0)
                self._visible = False
            return

        if not self._visible:
            self.attributes('-alpha', 1.0)
            self._visible = True

        # auto-size window (only update when size changes)
        max_name = max(
            (_session_display_name(k, names) for k in seen),
            key=lambda n: font.measure(n),
            default="",
        )
        w = max(MIN_WIDTH, min(MAX_WIDTH, DOTS_WIDTH + font.measure(max_name) + PADDING * 2 + 12))
        h = count * (ROW_HEIGHT + 2) + PADDING * 2
        geo = self.geometry()
        parts = geo.split("+")
        new_geo = f"{w}x{h}+{parts[1]}+{parts[2]}"
        if new_geo != self._last_geometry:
            self._last_geometry = new_geo
            self.geometry(new_geo)

