"""Command line interface for AI agent signal lights."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping, Sequence

from signal_light.agent_signals import SIGNALS, AgentSignal, Frame
from signal_light.hardware import LightMapping, SignalLight, SignalLightError
from signal_light.runtime import (
    read_display_snapshot,
    run_server,
    submit_direct_signal,
    submit_session_signal,
)


HOOK_CONTROL_SIGNALS = {"turn_end"}
HOOK_OWNER_PID_PAYLOAD_KEYS = ("owner_pid", "session_pid", "agent_pid", "process_pid")
HOOK_OWNER_PID_ENV_KEYS = (
    "SIGNAL_LIGHT_OWNER_PID",
    "CODEX_OWNER_PID",
    "CLAUDE_CODE_OWNER_PID",
    "CLAUDE_OWNER_PID",
)


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
    subparsers.add_parser("status", help="show current server display and session state")

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

    subparsers.add_parser("server", help=argparse.SUPPRESS)

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
        owner_pid = resolve_hook_owner_pid(hook_input.payload, os.environ)
        return play_hook_signal(signal, session_key=key, owner_pid=owner_pid, dry_run=args.dry_run, quiet=True)
    if args.command == "claude-code-hook":
        event = args.event_option or args.event
        from signal_light.claude_code_hook import choose_signal as cc_choose_signal
        from signal_light.claude_code_hook import read_hook_input, session_key as cc_session_key

        hook_argv = ["signal-light", "--event", event] if event else ["signal-light"]
        hook_input = read_hook_input(hook_argv, sys.stdin.read())
        signal = cc_choose_signal(hook_input)
        key = cc_session_key(hook_input, os.environ)
        owner_pid = resolve_hook_owner_pid(hook_input.payload, os.environ)
        return play_hook_signal(signal, session_key=key, owner_pid=owner_pid, dry_run=args.dry_run, quiet=True)
    if args.command == "status":
        print(json.dumps(read_display_snapshot(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "server":
        return run_server()
    if args.command == "test":
        return run_test(dry_run=args.dry_run)
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
            submit_direct_signal(signal.name, speed=speed)
    except SignalLightError as exc:
        if not quiet:
            print(str(exc), file=sys.stderr)
        return 1

    return 0


def play_hook_signal(
    signal_name: str,
    *,
    session_key: str,
    owner_pid: int | None = None,
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
        response = submit_session_signal(
            session_key,
            signal_name,
            owner_pid=owner_pid,
            speed=speed,
        )
        aggregate = str(response.get("aggregate", "idle"))
    except SignalLightError as exc:
        if not quiet:
            print(str(exc), file=sys.stderr)
        return 1

    if not quiet:
        print(f"Session {session_key}: {signal_name}; aggregate={aggregate}")
    return 0


def resolve_hook_owner_pid(payload: Mapping[str, Any], environ: Mapping[str, str]) -> int | None:
    """Return an explicitly supplied session owner PID, if the hook provides one."""
    for key in HOOK_OWNER_PID_PAYLOAD_KEYS:
        pid = _coerce_owner_pid(payload.get(key))
        if pid is not None:
            return pid

    for key in HOOK_OWNER_PID_ENV_KEYS:
        pid = _coerce_owner_pid(environ.get(key))
        if pid is not None:
            return pid

    return None


def _coerce_owner_pid(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        pid = int(value.strip())
        return pid if pid > 0 else None
    return None


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


if __name__ == "__main__":
    raise SystemExit(main())
