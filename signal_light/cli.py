"""Command line interface for AI agent signal lights."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from signal_light.agent_signals import SIGNALS, AgentSignal, Frame
from signal_light.hardware import LightMapping, SignalLight, SignalLightError
from signal_light.runtime import (
    IDLE_SLEEP_SIGNAL,
    SESSION_END_NOTICE_SIGNAL,
    apply_session_signal,
    apply_signal,
    clear_session_state,
    read_session_snapshot,
    run_worker,
)


HOOK_CONTROL_SIGNALS = {"turn_end"}


class DryRunLight:
    def write(self, *, green: bool = False, yellow: bool = False, red: bool = False) -> None:
        print(f"green={int(green)} yellow={int(yellow)} red={int(red)}")

    def write_brightness(self, *, green: float = 0.0, yellow: float = 0.0, red: float = 0.0) -> None:
        print(f"green={green:.2f} yellow={yellow:.2f} red={red:.2f}")

    def off(self) -> None:
        self.write()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signal-light",
        description="Play AI agent status patterns on a red/yellow/green traffic signal model.",
    )
    subparsers = parser.add_subparsers(dest="command")

    play = subparsers.add_parser("play", help="play one lamp-language signal")
    play.add_argument("signal", choices=sorted(SIGNALS), help="signal name")
    play.add_argument("--dry-run", action="store_true", help="print GPIO states instead of touching hardware")
    play.add_argument("--speed", type=float, default=1.0, help="delay multiplier; lower is faster")
    play.add_argument("--quiet", action="store_true", help="suppress non-error output")

    subparsers.add_parser("list", help="list available lamp-language signals")
    subparsers.add_parser("status", help="show aggregated Codex session signal state")

    install_hooks = subparsers.add_parser("install-hooks", help="install or repair local agent hooks")
    install_hooks.add_argument(
        "--agent",
        action="append",
        dest="agents",
        help="agent to install: codex or claude-code; can be passed more than once",
    )
    install_hooks.add_argument("--all", action="store_true", help="install or repair all supported agents")
    install_hooks.add_argument("-y", "--yes", action="store_true", help="accept the suggested selection")
    install_hooks.add_argument("--dry-run", action="store_true", help="show planned changes without writing files")

    hook = subparsers.add_parser("codex-hook", help="read a Codex hook event and play the matching signal")
    hook.add_argument("event", nargs="?", help="Codex hook event name, for example Stop or PermissionRequest")
    hook.add_argument("--event", dest="event_option", help="Codex hook event name")
    hook.add_argument("--dry-run", action="store_true", help="print GPIO states instead of touching hardware")

    cc_hook = subparsers.add_parser("claude-code-hook", help="read a Claude Code hook event and play the matching signal")
    cc_hook.add_argument("event", nargs="?", help="Claude Code hook event name, for example Stop or PreToolUse")
    cc_hook.add_argument("--event", dest="event_option", help="Claude Code hook event name")
    cc_hook.add_argument("--dry-run", action="store_true", help="print GPIO states instead of touching hardware")

    worker = subparsers.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument(
        "signal",
        choices=sorted(
            {
                *(name for name, signal in SIGNALS.items() if signal.repeat),
                SESSION_END_NOTICE_SIGNAL,
                IDLE_SLEEP_SIGNAL,
            }
        ),
    )
    worker.add_argument("--owner-token", help=argparse.SUPPRESS)
    worker.add_argument("--speed", type=float, default=1.0)

    menu_bar = subparsers.add_parser("menu-bar", help="start the macOS menu bar status indicator")
    menu_bar.add_argument("--status", action="store_true", help="print current session state and exit")

    desktop = subparsers.add_parser("desktop", help="start the desktop floating traffic-light window")
    desktop.add_argument("--status", action="store_true", help="print current session state and exit")

    test = subparsers.add_parser("test", help="run a quick red/yellow/green hardware test")
    test.add_argument("--dry-run", action="store_true", help="print GPIO states instead of touching hardware")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "list":
        return list_signals()
    if args.command == "play":
        return play_signal(args.signal, dry_run=args.dry_run, speed=args.speed, quiet=args.quiet)
    if args.command == "install-hooks":
        from signal_light.hook_installer import run_install_wizard

        try:
            return run_install_wizard(
                selected_agents=args.agents,
                all_agents=args.all,
                yes=args.yes,
                dry_run=args.dry_run,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "codex-hook":
        event = args.event_option or args.event
        from signal_light.codex_hook import choose_signal, read_codex_hook_input, session_key

        hook_argv = ["signal-light", "--event", event] if event else ["signal-light"]
        hook_input = read_codex_hook_input(hook_argv, sys.stdin.read(), os.environ)
        signal = choose_signal(hook_input)
        key = session_key(hook_input, os.environ)
        return play_hook_signal(signal, session_key=key, dry_run=args.dry_run, quiet=True)
    if args.command == "claude-code-hook":
        event = args.event_option or args.event
        from signal_light.claude_code_hook import choose_signal as cc_choose_signal
        from signal_light.claude_code_hook import read_hook_input, session_key as cc_session_key

        hook_argv = ["signal-light", "--event", event] if event else ["signal-light"]
        hook_input = read_hook_input(hook_argv, sys.stdin.read())
        signal = cc_choose_signal(hook_input)
        key = cc_session_key(hook_input, os.environ)
        return play_hook_signal(signal, session_key=key, dry_run=args.dry_run, quiet=True)
    if args.command == "status":
        print(json.dumps(read_session_snapshot(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "menu-bar":
        return run_menu_bar(status_only=args.status)
    if args.command == "desktop":
        return run_desktop(status_only=args.status)
    if args.command == "test":
        return run_test(dry_run=args.dry_run)
    if args.command == "worker":
        return run_worker(args.signal, speed=args.speed)

    parser.print_help()
    return 2


def list_signals() -> int:
    print("Signal language:")
    for signal in SIGNALS.values():
        print(f"- {signal.name}: {signal.summary} {signal.attention}")
    return 0


def play_signal(signal_name: str, *, dry_run: bool = False, speed: float = 1.0, quiet: bool = False) -> int:
    signal = SIGNALS.get(signal_name)
    if signal is None:
        if not quiet:
            print(f"Unknown signal: {signal_name}", file=sys.stderr)
        return 2

    if not quiet:
        print(f"Playing {signal.name}: {signal.summary}")

    try:
        if dry_run:
            if signal.repeat:
                _preview_repeating_signal(signal, speed=speed)
            else:
                signal.play(DryRunLight(), speed=speed)
        else:
            if signal.name in {"idle", "off"}:
                clear_session_state()
            apply_signal(signal, speed=speed)
    except SignalLightError as exc:
        if not quiet:
            print(str(exc), file=sys.stderr)
        return 1

    return 0


def play_hook_signal(
    signal_name: str,
    *,
    session_key: str,
    dry_run: bool = False,
    speed: float = 1.0,
    quiet: bool = False,
) -> int:
    signal = SIGNALS.get(signal_name)
    if signal is None and signal_name not in HOOK_CONTROL_SIGNALS:
        if not quiet:
            print(f"Unknown signal: {signal_name}", file=sys.stderr)
        return 2

    if dry_run:
        if not quiet:
            print(f"Session {session_key}: {signal_name}")
        if signal is None:
            return 0
        if signal.repeat:
            _preview_repeating_signal(signal, speed=speed)
        else:
            signal.play(DryRunLight(), speed=speed)
        return 0

    try:
        aggregate = apply_session_signal(session_key, signal_name, speed=speed)
    except SignalLightError as exc:
        if not quiet:
            print(str(exc), file=sys.stderr)
        return 1

    if not quiet:
        print(f"Session {session_key}: {signal_name}; aggregate={aggregate}")
    return 0


def _preview_repeating_signal(signal: AgentSignal, *, speed: float) -> None:
    signal.play(DryRunLight(), speed=speed, cycles=2)


def run_test(*, dry_run: bool = False) -> int:
    test_signal = AgentSignal(
        name="test",
        summary="red/yellow/green wiring test",
        attention="",
        frames=(
            Frame(red=True, seconds=0.35),
            Frame(yellow=True, seconds=0.35),
            Frame(green=True, seconds=0.35),
            Frame(red=True, yellow=True, green=True, seconds=0.35),
        ),
        loops=2,
    )

    try:
        if dry_run:
            test_signal.play(DryRunLight())
        else:
            with SignalLight(LightMapping.from_env(os.environ)) as light:
                test_signal.play(light)
    except SignalLightError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


MENU_BAR_PID_FILE = Path("/private/tmp/signal-light/menu-bar.pid")
DESKTOP_PID_FILE = Path("/private/tmp/signal-light/desktop.pid")


def _pid_file_already_running(pid_file: Path) -> bool:
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _desktop_already_running() -> bool:
    return _pid_file_already_running(DESKTOP_PID_FILE)


def run_menu_bar(*, status_only: bool = False) -> int:
    from signal_light.menu_bar import run as run_menu_bar_app
    from signal_light.desktop_monitor import _read_sessions, _read_session_names, aggregate_sessions

    if status_only:
        sessions = _read_sessions()
        aggregate = aggregate_sessions(sessions)
        print(json.dumps({"aggregate": aggregate, "sessions": sessions}, ensure_ascii=False, indent=2))
        return 0

    if _pid_file_already_running(MENU_BAR_PID_FILE):
        print("状态栏信号灯已在运行中", file=sys.stderr)
        return 0

    MENU_BAR_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    MENU_BAR_PID_FILE.write_text(str(os.getpid()))

    try:
        run_menu_bar_app()
    finally:
        try:
            MENU_BAR_PID_FILE.unlink()
        except FileNotFoundError:
            pass

    return 0


def run_desktop(*, status_only: bool = False) -> int:
    from signal_light.desktop_ui import DesktopSignalWindow
    from signal_light.desktop_monitor import (
        DesktopMonitor, _read_sessions, _read_session_names,
        aggregate_sessions, bootstrap_existing_sessions,
    )

    if status_only:
        sessions = _read_sessions()
        aggregate = aggregate_sessions(sessions)
        print(json.dumps({
            "aggregate": aggregate,
            "sessions": sessions,
        }, ensure_ascii=False, indent=2))
        return 0

    if _desktop_already_running():
        print("信号灯已在运行中", file=sys.stderr)
        return 0

    DESKTOP_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_PID_FILE.write_text(str(os.getpid()))

    try:
        bootstrap_existing_sessions()

        window = DesktopSignalWindow()
        monitor = DesktopMonitor(window)

        sessions = _read_sessions()
        names = _read_session_names()
        aggregate = aggregate_sessions(sessions)
        window.update_state(aggregate, sessions, names)
        monitor.start()

        window.mainloop()
    finally:
        try:
            DESKTOP_PID_FILE.unlink()
        except FileNotFoundError:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
