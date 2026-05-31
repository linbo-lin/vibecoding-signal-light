import io
import json
import os
import threading
import time
from pathlib import Path

from signal_light.agent_signals import SIGNALS
from signal_light import cli
from signal_light.codex_hook import CodexHookInput, choose_signal, session_key
from signal_light import hook_installer
from signal_light import runtime
from signal_light.runtime import aggregate_sessions


class RecordingLight:
    def __init__(self) -> None:
        self.states: list[tuple[bool, bool, bool]] = []
        self.brightness_states: list[tuple[float, float, float]] = []

    def write(self, *, green: bool = False, yellow: bool = False, red: bool = False) -> None:
        self.states.append((green, yellow, red))

    def write_brightness(self, *, green: float = 0.0, yellow: float = 0.0, red: float = 0.0) -> None:
        self.brightness_states.append((green, yellow, red))

    def off(self) -> None:
        self.write()


def test_idle_signal_leaves_green_on() -> None:
    light = RecordingLight()

    SIGNALS["idle"].play(light, speed=0.05)

    assert SIGNALS["idle"].repeat is False
    assert light.states[-1] == (True, False, False)


def test_working_signal_uses_soft_green_yellow_red_cycle() -> None:
    light = RecordingLight()

    SIGNALS["working"].play(light, speed=0.05, cycles=1)

    assert SIGNALS["working"].repeat is True
    assert len(light.brightness_states) == 27
    assert all(green > 0 and yellow == 0 and red == 0 for green, yellow, red in light.brightness_states[:9])
    assert all(green == 0 and yellow > 0 and red == 0 for green, yellow, red in light.brightness_states[9:18])
    assert all(green == 0 and yellow == 0 and red > 0 for green, yellow, red in light.brightness_states[18:27])
    assert light.brightness_states[0][0] < light.brightness_states[4][0]
    assert light.brightness_states[4][0] > light.brightness_states[8][0]


def test_attention_signal_flashes_yellow() -> None:
    light = RecordingLight()

    SIGNALS["attention"].play(light, speed=0.05, cycles=1)

    assert SIGNALS["attention"].repeat is True
    assert light.states[:2] == [(False, True, False), (False, False, False)]


def test_thinking_signal_uses_work_cycle() -> None:
    light = RecordingLight()

    SIGNALS["thinking"].play(light, speed=0.05, cycles=1)

    assert SIGNALS["thinking"].frames == SIGNALS["working"].frames
    assert len(light.brightness_states) == 27
    assert light.brightness_states[0] == (0.10, 0.0, 0.0)
    assert light.brightness_states[9] == (0.0, 0.10, 0.0)
    assert light.brightness_states[18] == (0.0, 0.0, 0.10)


def test_permission_signal_flashes_yellow() -> None:
    light = RecordingLight()

    SIGNALS["permission"].play(light, speed=0.05, cycles=1)

    assert SIGNALS["permission"].repeat is True
    assert light.states[:2] == [(False, True, False), (False, False, False)]


def test_session_end_returns_to_idle_green() -> None:
    light = RecordingLight()

    SIGNALS["session_end"].play(light, speed=0.05)

    assert light.states[-1] == (True, False, False)


def test_session_done_signal_briefly_flashes_green() -> None:
    light = RecordingLight()

    SIGNALS["session_done"].play(light, speed=0.05, cycles=1)

    assert SIGNALS["session_done"].repeat is False
    assert light.states[:2] == [(True, False, False), (False, False, False)]
    assert light.states[-1] == (False, False, False)


def test_codex_stop_maps_to_turn_end() -> None:
    signal = choose_signal(CodexHookInput(event_name="Stop", payload={}))

    assert signal == "turn_end"


def test_failed_payload_maps_to_blocked() -> None:
    signal = choose_signal(
        CodexHookInput(
            event_name="PostToolUse",
            payload={"status": "failed"},
        )
    )

    assert signal == "blocked"


def test_structured_error_payload_maps_to_blocked() -> None:
    signal = choose_signal(
        CodexHookInput(
            event_name="PostToolUse",
            payload={"error": {"message": "command failed"}},
        )
    )

    assert signal == "blocked"


def test_prompt_text_containing_error_does_not_map_to_blocked() -> None:
    signal = choose_signal(
        CodexHookInput(
            event_name="UserPromptSubmit",
            payload={"prompt": "please fix this error"},
        )
    )

    assert signal == "thinking"


def test_success_status_does_not_become_unknown_signal() -> None:
    signal = choose_signal(
        CodexHookInput(
            event_name="PostToolUse",
            payload={"status": "success"},
        )
    )

    assert signal == "tool_done"


def test_aggregate_keeps_attention_over_other_working_session() -> None:
    aggregate = aggregate_sessions(
        {
            "a": {"signal": "attention", "updated_at": 1},
            "b": {"signal": "working", "updated_at": 1},
        }
    )

    assert aggregate == "attention"


def test_aggregate_keeps_permission_over_attention_and_working() -> None:
    aggregate = aggregate_sessions(
        {
            "a": {"signal": "attention", "updated_at": 1},
            "b": {"signal": "working", "updated_at": 1},
            "c": {"signal": "permission", "updated_at": 1},
        }
    )

    assert aggregate == "permission"


def test_aggregate_returns_working_when_any_session_is_working() -> None:
    aggregate = aggregate_sessions(
        {
            "a": {"signal": "idle", "updated_at": 1},
            "b": {"signal": "tool_done", "updated_at": 1},
        }
    )

    assert aggregate == "working"


def test_aggregate_returns_idle_for_empty_sessions() -> None:
    assert aggregate_sessions({}) == "idle"


def test_session_key_uses_session_id_without_turn_id() -> None:
    key = session_key(
        CodexHookInput(event_name="Stop", payload={"session_id": "session-a", "cwd": "/tmp/x"}),
        {},
    )

    assert key == "session-a"


def test_session_key_prefers_turn_id_over_session_id() -> None:
    key = session_key(
        CodexHookInput(event_name="Stop", payload={"session_id": "session-a", "turn_id": "turn-a"}),
        {},
    )

    assert key == "turn:turn-a"


def test_session_key_falls_back_to_cwd() -> None:
    key = session_key(
        CodexHookInput(event_name="Stop", payload={"cwd": "/tmp/project"}),
        {},
    )

    assert key == "cwd:/tmp/project"


def test_session_key_uses_turn_id_before_cwd() -> None:
    key = session_key(
        CodexHookInput(event_name="Stop", payload={"turn_id": "turn-a", "cwd": "/tmp/project"}),
        {"CODEX_TURN_ID": "turn-env"},
    )

    assert key == "turn:turn-a"


def test_session_key_uses_env_turn_id_before_cwd() -> None:
    key = session_key(
        CodexHookInput(event_name="Stop", payload={"cwd": "/tmp/project"}),
        {"CODEX_TURN_ID": "turn-env"},
    )

    assert key == "turn:turn-env"


def test_cli_codex_hook_uses_session_aware_path(monkeypatch) -> None:
    calls: list[tuple[str, str, int | None, bool, bool]] = []
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"session-a","event":"Stop"}'))
    monkeypatch.setattr(
        cli,
        "play_hook_signal",
        lambda signal_name, *, session_key, owner_pid=None, dry_run=False, quiet=False: calls.append(
            (signal_name, session_key, owner_pid, dry_run, quiet)
        )
        or 0,
    )

    assert cli.main(["codex-hook", "--dry-run"]) == 0
    assert calls == [("turn_end", "session-a", None, True, True)]


def test_cli_codex_hook_without_event_uses_stdin_event(monkeypatch) -> None:
    calls: list[tuple[str, str, int | None, bool, bool]] = []
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"session-a","event":"PermissionRequest"}'))
    monkeypatch.setattr(
        cli,
        "play_hook_signal",
        lambda signal_name, *, session_key, owner_pid=None, dry_run=False, quiet=False: calls.append(
            (signal_name, session_key, owner_pid, dry_run, quiet)
        )
        or 0,
    )

    assert cli.main(["codex-hook", "--dry-run"]) == 0
    assert calls == [("permission", "session-a", None, True, True)]


def test_play_signal_submits_to_server(monkeypatch) -> None:
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        cli,
        "submit_direct_signal",
        lambda signal_name, speed=1.0: calls.append((signal_name, speed)) or {"ok": True},
    )

    assert cli.play_signal("working", speed=0.5) == 0
    assert calls == [("working", 0.5)]


def test_play_hook_signal_does_not_guess_owner_pid(monkeypatch) -> None:
    calls: list[tuple[str, str, int | None, float]] = []
    monkeypatch.setattr(
        cli,
        "submit_session_signal",
        lambda session_key, signal_name, owner_pid=None, speed=1.0: calls.append(
            (session_key, signal_name, owner_pid, speed)
        )
        or {"ok": True, "aggregate": "working"},
    )

    assert cli.play_hook_signal("working", session_key="session-a", speed=0.5) == 0
    assert calls == [("session-a", "working", None, 0.5)]


def test_play_hook_signal_submits_explicit_owner_pid(monkeypatch) -> None:
    calls: list[tuple[str, str, int | None, float]] = []
    monkeypatch.setattr(
        cli,
        "submit_session_signal",
        lambda session_key, signal_name, owner_pid=None, speed=1.0: calls.append(
            (session_key, signal_name, owner_pid, speed)
        )
        or {"ok": True, "aggregate": "working"},
    )

    assert cli.play_hook_signal("working", session_key="session-a", owner_pid=4321, speed=0.5) == 0
    assert calls == [("session-a", "working", 4321, 0.5)]


def test_resolve_hook_owner_pid_uses_explicit_payload_pid() -> None:
    assert cli.resolve_hook_owner_pid({"owner_pid": "4321"}, {}) == 4321


def test_resolve_hook_owner_pid_uses_explicit_env_pid() -> None:
    assert cli.resolve_hook_owner_pid({}, {"SIGNAL_LIGHT_OWNER_PID": "4321"}) == 4321


def test_resolve_hook_owner_pid_ignores_invalid_values() -> None:
    assert cli.resolve_hook_owner_pid({"owner_pid": "app-server"}, {"SIGNAL_LIGHT_OWNER_PID": "0"}) is None


def test_update_session_signal_preserves_attention_over_other_work(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("session-a", "attention") == {"aggregate": "attention", "show_notice": False}
    assert runtime.update_session_signal("session-b", "working") == {"aggregate": "attention", "show_notice": False}


def test_update_session_signal_escalates_permission_over_attention(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("session-a", "attention") == {"aggregate": "attention", "show_notice": False}
    assert runtime.update_session_signal("session-b", "permission") == {"aggregate": "permission", "show_notice": False}


def test_update_session_signal_removes_session_on_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("session-a", "working") == {"aggregate": "working", "show_notice": False}
    assert runtime.update_session_signal("session-a", "session_end") == {"aggregate": "idle", "show_notice": True}

    assert runtime.read_session_snapshot() == {"aggregate": "idle", "sessions": {}}


def test_update_session_signal_turn_end_restores_idle_after_notice(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("session-a", "working") == {"aggregate": "working", "show_notice": False}
    assert runtime.update_session_signal("session-a", "turn_end") == {"aggregate": "idle", "show_notice": True}

    assert runtime.read_session_snapshot() == {"aggregate": "idle", "sessions": {}}


def test_update_session_signal_notices_one_session_end_while_another_works(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("session-a", "working") == {"aggregate": "working", "show_notice": False}
    assert runtime.update_session_signal("session-b", "working") == {"aggregate": "working", "show_notice": False}
    assert runtime.update_session_signal("session-a", "session_end") == {"aggregate": "working", "show_notice": True}


def test_update_session_signal_turn_end_notices_one_session_end_while_another_works(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("session-a", "working") == {"aggregate": "working", "show_notice": False}
    assert runtime.update_session_signal("session-b", "working") == {"aggregate": "working", "show_notice": False}
    assert runtime.update_session_signal("session-a", "turn_end") == {"aggregate": "working", "show_notice": True}



def test_prune_dead_owner_sessions_removes_only_dead_processes(monkeypatch) -> None:
    sessions = {
        "session-a": {
            "signal": "working",
            "updated_at": 1,
            "owner_pid": 111,
            "owner_pid_source": runtime.OWNER_PID_SOURCE,
        },
        "session-b": {
            "signal": "working",
            "updated_at": 1,
            "owner_pid": 222,
            "owner_pid_source": runtime.OWNER_PID_SOURCE,
        },
        "session-c": {"signal": "attention", "updated_at": 1},
    }
    monkeypatch.setattr(runtime, "_is_running", lambda pid: pid == 222)

    changed = runtime._prune_dead_owner_sessions(sessions)

    assert changed is True
    assert sessions == {
        "session-b": {
            "signal": "working",
            "updated_at": 1,
            "owner_pid": 222,
            "owner_pid_source": runtime.OWNER_PID_SOURCE,
        },
        "session-c": {"signal": "attention", "updated_at": 1},
    }


def test_prune_sessions_removes_stale_working_state(monkeypatch) -> None:
    sessions = {
        "session-a": {"signal": "working", "updated_at": 100.0},
        "session-b": {"signal": "permission", "updated_at": 100.0},
    }
    monkeypatch.setattr(runtime, "WORK_SESSION_STALE_SECONDS", 60)
    monkeypatch.setattr(runtime, "SESSION_TTL_SECONDS", 86400)

    runtime._prune_sessions(sessions, 200.0)

    assert sessions == {
        "session-b": {"signal": "permission", "updated_at": 100.0},
    }


def test_prune_sessions_removes_legacy_owner_working_state() -> None:
    sessions = {
        "session-a": {"signal": "tool_done", "updated_at": 100.0, "owner_pid": 30569},
        "session-b": {"signal": "permission", "updated_at": 100.0, "owner_pid": 30569},
    }

    runtime._prune_sessions(sessions, 101.0)

    assert sessions == {
        "session-b": {"signal": "permission", "updated_at": 100.0},
    }


def test_update_session_signal_marks_explicit_owner_pid(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "_is_running", lambda _pid: True)

    runtime.update_session_signal("session-a", "working", owner_pid=4321)

    session = runtime.read_session_snapshot()["sessions"]["session-a"]
    assert session["owner_pid"] == 4321
    assert session["owner_pid_source"] == runtime.OWNER_PID_SOURCE


def test_reconcile_server_sessions_applies_new_aggregate_after_owner_exit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "sessions.json").write_text(
        json.dumps(
            {
                "sessions": {
                    "session-a": {
                        "signal": "working",
                        "updated_at": time.time(),
                        "owner_pid": 111,
                        "owner_pid_source": runtime.OWNER_PID_SOURCE,
                    },
                    "session-b": {
                        "signal": "working",
                        "updated_at": time.time(),
                        "owner_pid": 222,
                        "owner_pid_source": runtime.OWNER_PID_SOURCE,
                    },
                }
            }
        )
    )
    monkeypatch.setattr(runtime, "_is_running", lambda pid: pid == 222)

    result = runtime._reconcile_server_sessions()

    snapshot = runtime.read_session_snapshot()
    assert snapshot["aggregate"] == "working"
    assert set(snapshot["sessions"]) == {"session-b"}
    assert snapshot["sessions"]["session-b"]["owner_pid"] == 222
    assert snapshot["sessions"]["session-b"]["owner_pid_source"] == runtime.OWNER_PID_SOURCE
    assert result == {"changed": True, "aggregate": "working"}


def test_server_process_lock_rejects_second_display_owner(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SERVER_LOCK_FILE", tmp_path / "server.lock")

    with runtime._server_process_lock():
        try:
            with runtime._server_process_lock():
                raised = False
        except runtime.SignalLightError:
            raised = True

    assert raised is True


def test_ensure_server_running_waits_for_existing_startup(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SERVER_PID_FILE", tmp_path / "server.json")
    monkeypatch.setattr(runtime, "SERVER_LOG_FILE", tmp_path / "server.log")
    monkeypatch.setattr(runtime, "SERVER_SOCKET_FILE", tmp_path / "server.sock")
    monkeypatch.setattr(runtime, "SERVER_LOCK_FILE", tmp_path / "server.lock")
    monkeypatch.setattr(runtime, "SERVER_STARTUP_LOCK_FILE", tmp_path / "server-startup.lock")

    attempts = {"count": 0}

    def fake_server_running() -> bool:
        attempts["count"] += 1
        return attempts["count"] >= 3

    def fail_popen(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("client should wait for the existing display server")

    monkeypatch.setattr(runtime.subprocess, "Popen", fail_popen)
    monkeypatch.setattr(runtime, "_server_running", fake_server_running)
    monkeypatch.setattr(runtime, "_server_process_lock_is_held", lambda: True)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    runtime._ensure_server_running()

    assert attempts["count"] == 3


def test_server_running_requires_connectable_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "SERVER_PID_FILE", tmp_path / "server.json")
    (tmp_path / "server.json").write_text(json.dumps({"pid": 12345}))
    monkeypatch.setattr(runtime, "_is_running", lambda _pid: True)
    monkeypatch.setattr(runtime, "_send_server_request_once", lambda _payload: (_ for _ in ()).throw(OSError("stale")))

    assert runtime._server_running() is False


def test_send_server_request_restarts_after_stale_ipc(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "SERVER_PID_FILE", tmp_path / "server.json")
    monkeypatch.setattr(runtime, "SERVER_SOCKET_FILE", tmp_path / "server.sock")
    monkeypatch.setattr(runtime, "SERVER_REQUEST_DIR", tmp_path / "requests")
    (tmp_path / "server.json").write_text(json.dumps({"pid": 12345}))
    (tmp_path / "server.sock").write_text("")
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests" / "old.request.json").write_text("{}")
    calls: list[str] = []
    send_count = {"value": 0}

    def fake_send_once(_payload: dict[str, object]) -> dict[str, object]:
        send_count["value"] += 1
        calls.append("send")
        if send_count["value"] == 1:
            raise OSError("stale")
        return {"ok": True}

    monkeypatch.setattr(runtime, "_ensure_server_running", lambda: calls.append("ensure"))
    monkeypatch.setattr(runtime, "_send_server_request_once", fake_send_once)
    monkeypatch.setattr(runtime, "_stop_unreachable_server", lambda: calls.append("stop") or True)
    monkeypatch.setattr(runtime, "_wait_for_server_lock_release", lambda timeout: calls.append("wait") or True)

    assert runtime._send_server_request({"action": "status"}) == {"ok": True}
    assert calls == ["ensure", "send", "stop", "wait", "ensure", "send"]
    assert not (tmp_path / "server.json").exists()
    assert not (tmp_path / "server.sock").exists()
    assert not (tmp_path / "requests" / "old.request.json").exists()


def test_send_server_request_preserves_pid_when_unreachable_server_will_not_stop(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "SERVER_PID_FILE", tmp_path / "server.json")
    monkeypatch.setattr(runtime, "SERVER_SOCKET_FILE", tmp_path / "server.sock")
    monkeypatch.setattr(runtime, "SERVER_REQUEST_DIR", tmp_path / "requests")
    (tmp_path / "server.json").write_text(json.dumps({"pid": 12345}))
    (tmp_path / "server.sock").write_text("")
    (tmp_path / "requests").mkdir()
    (tmp_path / "requests" / "old.request.json").write_text("{}")
    monkeypatch.setattr(runtime, "_ensure_server_running", lambda: None)
    monkeypatch.setattr(runtime, "_send_server_request_once", lambda _payload: (_ for _ in ()).throw(OSError("stale")))
    monkeypatch.setattr(runtime, "_stop_unreachable_server", lambda: False)

    try:
        runtime._send_server_request({"action": "status"})
        raised = False
    except runtime.SignalLightError:
        raised = True

    assert raised is True
    assert (tmp_path / "server.json").exists()
    assert (tmp_path / "server.sock").exists()
    assert (tmp_path / "requests" / "old.request.json").exists()


def test_send_server_request_once_uses_file_ipc(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SERVER_REQUEST_DIR", tmp_path / "requests")
    monkeypatch.setattr(runtime, "SERVER_REQUEST_TIMEOUT_SECONDS", 0.5)
    writes: list[dict[str, object]] = []

    def fake_server_response() -> None:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            requests = list((tmp_path / "requests").glob("*.request.json"))
            if requests:
                request_path = requests[0]
                request = json.loads(request_path.read_text())
                writes.append(request)
                response_path = tmp_path / "requests" / request_path.name.replace(".request.json", ".response.json")
                request_path.unlink()
                response_path.write_text('{"ok": true}\n')
                return
            time.sleep(0.01)

    thread = threading.Thread(target=fake_server_response)
    thread.start()

    try:
        assert runtime._send_server_request_once({"action": "status"}) == {"ok": True}
        assert writes == [{"action": "status"}]
        assert not list((tmp_path / "requests").glob("*.request.json"))
        assert not list((tmp_path / "requests").glob("*.response.json"))
    finally:
        thread.join(timeout=1.0)


def test_ensure_server_running_stops_unreachable_lock_owner(monkeypatch) -> None:
    calls: list[str] = []
    lock_held = {"value": True}

    def fake_lock_held() -> bool:
        return lock_held["value"]

    def fake_stop_unreachable_server() -> bool:
        calls.append("stop")
        lock_held["value"] = False
        return True

    class StartedProcess:
        pid = 222

        def poll(self) -> None:
            return None

    monkeypatch.setattr(runtime, "_server_running", lambda: False)
    monkeypatch.setattr(runtime, "_server_process_lock_is_held", fake_lock_held)
    monkeypatch.setattr(runtime, "_stop_unreachable_server", fake_stop_unreachable_server)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: calls.append("start") or StartedProcess())

    try:
        runtime._ensure_server_running()
    except runtime.SignalLightError:
        pass

    assert calls[:2] == ["stop", "start"]


def test_update_direct_signal_persists_display_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_direct_signal("off") == {
        "aggregate": "idle",
        "display_signal": "off",
        "sessions": {},
    }
    assert runtime.read_display_snapshot() == {
        "aggregate": "idle",
        "display_signal": "off",
        "sessions": {},
    }
    assert runtime.read_session_snapshot() == {"aggregate": "idle", "sessions": {}}


def test_session_signal_clears_manual_display_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    runtime.update_direct_signal("off")
    assert runtime.update_session_signal("session-a", "working") == {
        "aggregate": "working",
        "show_notice": False,
    }

    assert runtime.read_display_snapshot()["display_signal"] == "working"


def test_unknown_session_end_clears_manual_display_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    runtime.update_direct_signal("off")
    assert runtime.update_session_signal("missing-session", "session_end") == {
        "aggregate": "idle",
        "show_notice": True,
    }

    assert runtime.read_display_snapshot()["display_signal"] == "idle"


def test_server_status_returns_display_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    runtime.update_direct_signal("off")

    response = runtime._handle_server_request({"action": "status"}, runtime.ServerDisplay(RecordingLight()))

    assert response["display_signal"] == "off"


def test_handle_server_requests_writes_response_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SERVER_REQUEST_DIR", tmp_path / "requests")
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    (tmp_path / "requests").mkdir()
    request_path = tmp_path / "requests" / "abc.request.json"
    request_path.write_text('{"action":"status"}')

    runtime._handle_server_requests(runtime.ServerDisplay(RecordingLight()))

    response_path = tmp_path / "requests" / "abc.response.json"
    assert not request_path.exists()
    assert json.loads(response_path.read_text())["display_signal"] == "idle"


def test_server_display_cycles_working_frames(monkeypatch) -> None:
    light = RecordingLight()
    current_time = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: current_time[0])

    display = runtime.ServerDisplay(light, speed=0.05)
    display.set_aggregate("working")
    display.tick()
    current_time[0] += 1.0
    display.tick()

    assert len(light.brightness_states) == 2
    assert light.brightness_states[0][0] > 0
    assert light.brightness_states[1][0] > 0


def test_server_display_notice_then_restores_idle(monkeypatch) -> None:
    light = RecordingLight()
    current_time = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: current_time[0])

    display = runtime.ServerDisplay(light, speed=0.05)
    display.set_aggregate("idle", show_notice=True)
    display.tick()
    current_time[0] += runtime._signal_duration(SIGNALS["session_done"], 0.05) + 0.01
    display.tick()

    assert light.states[0] == (True, False, False)
    assert light.states[-1] == (True, False, False)


def test_server_display_turns_idle_off_after_timeout(monkeypatch) -> None:
    light = RecordingLight()
    current_time = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: current_time[0])
    monkeypatch.setattr(runtime, "IDLE_SLEEP_SECONDS", 10)

    display = runtime.ServerDisplay(light)
    display.set_aggregate("idle")
    display.tick()
    current_time[0] += 10.1
    assert display.tick() == "idle_sleep"

    assert light.states[0] == (True, False, False)
    assert light.states[-1] == (False, False, False)


def test_two_codex_turns_in_same_cwd_get_distinct_session_keys() -> None:
    first = session_key(
        CodexHookInput(event_name="PreToolUse", payload={"turn_id": "turn-a", "cwd": "/tmp/project"}),
        {},
    )
    second = session_key(
        CodexHookInput(event_name="PreToolUse", payload={"turn_id": "turn-b", "cwd": "/tmp/project"}),
        {},
    )

    assert first == "turn:turn-a"
    assert second == "turn:turn-b"
    assert first != second


def test_update_session_signal_notices_unknown_session_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("missing-session", "session_end") == {
        "aggregate": "idle",
        "show_notice": True,
    }


def test_update_session_signal_notices_unknown_turn_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("missing-session", "turn_end") == {
        "aggregate": "idle",
        "show_notice": True,
    }


def test_update_session_signal_keeps_red_alert_notice_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("session-a", "working") == {"aggregate": "working", "show_notice": False}
    assert runtime.update_session_signal("session-b", "permission") == {"aggregate": "permission", "show_notice": False}
    assert runtime.update_session_signal("session-a", "session_end") == {"aggregate": "permission", "show_notice": True}


def test_supported_agents_exposes_codex_and_claude_code(tmp_path) -> None:
    agents = hook_installer.supported_agents(home=tmp_path)

    assert set(agents) == {"codex", "claude-code"}
    assert agents["codex"].config_path == tmp_path / ".codex" / "hooks.json"
    assert agents["claude-code"].config_path == tmp_path / ".claude" / "settings.json"


def test_inspect_agent_marks_missing_config_as_needing_install(tmp_path) -> None:
    spec = hook_installer.supported_agents(home=tmp_path)["codex"]

    status = hook_installer.inspect_agent(spec)

    assert not status.installed
    assert status.message == "config missing"


def test_install_agent_writes_codex_hooks_and_backups_existing_file(tmp_path) -> None:
    spec = hook_installer.supported_agents(home=tmp_path)["codex"]
    spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    existing_hook = {"hooks": [{"type": "command", "command": "echo keep-me", "timeout": 1}]}
    spec.config_path.write_text(json.dumps({"hooks": {"Stop": [existing_hook]}}, indent=2))

    result = hook_installer.install_agent(spec)

    assert result.status.installed
    assert result.backup_path is not None
    data = json.loads(spec.config_path.read_text())
    assert set(data["hooks"]) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Stop", "SessionEnd"}
    assert existing_hook in data["hooks"]["Stop"]


def test_install_agent_replaces_existing_signal_light_hooks_but_keeps_other_hooks(tmp_path) -> None:
    spec = hook_installer.supported_agents(home=tmp_path)["claude-code"]
    spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": str(hook_installer.CLAUDE_CODE_HOOK_SCRIPT),
                            "timeout": 1,
                        }
                    ],
                    "matcher": "",
                },
                {
                    "hooks": [{"type": "command", "command": "echo keep-me", "timeout": 1}],
                    "matcher": "",
                },
            ]
        }
    }
    spec.config_path.write_text(json.dumps(existing, indent=2))

    hook_installer.install_agent(spec)

    data = json.loads(spec.config_path.read_text())
    stop_groups = data["hooks"]["Stop"]
    assert len(stop_groups) == 2
    assert stop_groups[0]["hooks"][0]["command"] == str(hook_installer.CLAUDE_CODE_HOOK_SCRIPT)
    assert stop_groups[0]["hooks"][0]["timeout"] == 5
    assert stop_groups[1]["hooks"][0]["command"] == "echo keep-me"


def test_install_agent_preserves_existing_hook_order_when_repairing(tmp_path) -> None:
    spec = hook_installer.supported_agents(home=tmp_path)["claude-code"]
    spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "echo before", "timeout": 1},
                        {
                            "type": "command",
                            "command": str(hook_installer.CLAUDE_CODE_HOOK_SCRIPT),
                            "timeout": 1,
                        },
                        {"type": "command", "command": "echo after", "timeout": 1},
                    ],
                    "matcher": "",
                }
            ]
        }
    }
    spec.config_path.write_text(json.dumps(existing, indent=2))

    hook_installer.install_agent(spec)

    data = json.loads(spec.config_path.read_text())
    hooks = data["hooks"]["Stop"][0]["hooks"]
    assert [hook["command"] for hook in hooks] == [
        "echo before",
        str(hook_installer.CLAUDE_CODE_HOOK_SCRIPT),
        "echo after",
    ]
    assert hooks[1]["timeout"] == 5


def test_inspect_agent_marks_wrong_timeout_as_broken(tmp_path) -> None:
    spec = hook_installer.supported_agents(home=tmp_path)["codex"]
    spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    hook_command = f"{hook_installer.CODEX_HOOK_SCRIPT} PermissionRequest"
    spec.config_path.write_text(
        json.dumps(
            {
                "hooks": {
                    event: [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{hook_installer.CODEX_HOOK_SCRIPT} {event}",
                                    "timeout": 5,
                                }
                            ]
                        }
                    ]
                    for event in hook_installer.CODEX_EVENTS
                }
            },
            indent=2,
        )
    )
    data = json.loads(spec.config_path.read_text())
    data["hooks"]["PermissionRequest"][0]["hooks"][0]["command"] = hook_command
    data["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] = 5
    spec.config_path.write_text(json.dumps(data, indent=2))

    status = hook_installer.inspect_agent(spec)

    assert not status.installed
    assert status.broken_events == ("PermissionRequest",)


def test_hook_command_quotes_paths_with_spaces() -> None:
    spec = hook_installer.AgentSpec(
        key="codex",
        name="Codex",
        config_path=Path("/tmp/unused.json"),
        hook_script=Path("/tmp/signal light/scripts/codex-signal-hook"),
        events={},
        passes_event_arg=True,
    )

    command = hook_installer._hook_command(spec, "Stop")

    assert command == "'/tmp/signal light/scripts/codex-signal-hook' Stop"


def test_install_wizard_selects_missing_agents_by_default(tmp_path, monkeypatch) -> None:
    codex_spec = hook_installer.supported_agents(home=tmp_path)["codex"]
    claude_spec = hook_installer.supported_agents(home=tmp_path)["claude-code"]
    codex_spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    codex_spec.config_path.write_text(json.dumps({"hooks": {}}, indent=2))

    written: list[str] = []

    def fake_install(spec, backup=True):
        written.append(spec.key)
        return hook_installer.InstallResult(
            status=hook_installer.inspect_agent(spec), changed=True, backup_path=None
        )

    monkeypatch.setattr(hook_installer, "install_agent", fake_install)

    stdin = io.StringIO("\n")
    stdout = io.StringIO()
    assert hook_installer.run_install_wizard(stdin=stdin, stdout=stdout, home=tmp_path, yes=True) == 0

    assert written == ["codex", "claude-code"]
    assert "Signal Light hook installer" in stdout.getvalue()


def test_install_wizard_supports_explicit_agent_selection(tmp_path, monkeypatch) -> None:
    selected: list[str] = []

    def fake_install(spec, backup=True):
        selected.append(spec.key)
        return hook_installer.InstallResult(
            status=hook_installer.inspect_agent(spec), changed=True, backup_path=None
        )

    monkeypatch.setattr(hook_installer, "install_agent", fake_install)

    assert hook_installer.run_install_wizard(selected_agents=["codex"], home=tmp_path, yes=True) == 0

    assert selected == ["codex"]


def test_install_hooks_cli_invokes_wizard(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(hook_installer, "run_install_wizard", lambda **kwargs: calls.append(kwargs) or 0)

    assert cli.main(["install-hooks", "--agent", "codex", "--dry-run"]) == 0

    assert calls == [{"selected_agents": ["codex"], "all_agents": False, "yes": False, "dry_run": True}]


def test_update_session_signal_clears_non_urgent_session_on_turn_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("session-a", "working") == {"aggregate": "working", "show_notice": False}
    assert runtime.update_session_signal("session-a", "turn_end") == {"aggregate": "idle", "show_notice": True}

    assert runtime.read_session_snapshot() == {"aggregate": "idle", "sessions": {}}


def test_session_done_notice_fires_when_other_sessions_still_working(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("session-a", "working") == {"aggregate": "working", "show_notice": False}
    assert runtime.update_session_signal("session-b", "working") == {"aggregate": "working", "show_notice": False}
    assert runtime.update_session_signal("session-b", "turn_end") == {"aggregate": "working", "show_notice": True}

    snapshot = runtime.read_session_snapshot()
    assert "session-a" in snapshot["sessions"]
    assert "session-b" not in snapshot["sessions"]


def test_update_session_signal_keeps_permission_on_turn_end(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")

    assert runtime.update_session_signal("session-a", "permission") == {"aggregate": "permission", "show_notice": False}
    assert runtime.update_session_signal("session-a", "turn_end") == {"aggregate": "permission", "show_notice": False}

    assert runtime.read_session_snapshot()["aggregate"] == "permission"


def test_manual_idle_clears_all_session_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(
        cli,
        "submit_direct_signal",
        lambda signal_name, speed=1.0: runtime.clear_session_state()
        or {"ok": True, "signal": signal_name},
    )

    assert runtime.update_session_signal("session-a", "attention") == {"aggregate": "attention", "show_notice": False}
    assert cli.play_signal("idle") == 0
    assert runtime.read_session_snapshot() == {"aggregate": "idle", "sessions": {}}


def test_manual_off_clears_all_session_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(
        cli,
        "submit_direct_signal",
        lambda signal_name, speed=1.0: runtime.clear_session_state()
        or {"ok": True, "signal": signal_name},
    )

    assert runtime.update_session_signal("session-a", "permission") == {"aggregate": "permission", "show_notice": False}
    assert cli.play_signal("off") == 0
    assert runtime.read_session_snapshot() == {"aggregate": "idle", "sessions": {}}
