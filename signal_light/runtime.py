"""Runtime process management for persistent signal-light states."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from signal_light.agent_signals import AgentSignal, Frame, SIGNALS
from signal_light.hardware import LightMapping, SignalLight, SignalLightError


STATE_DIR = Path(os.environ.get("SIGNAL_LIGHT_STATE_DIR", "/private/tmp/signal-light"))
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_PID_FILE = STATE_DIR / "server.json"
SERVER_LOG_FILE = STATE_DIR / "server.log"
SERVER_SOCKET_FILE = STATE_DIR / "server.sock"
SERVER_REQUEST_DIR = STATE_DIR / "requests"
SERVER_LOCK_FILE = STATE_DIR / "server.lock"
SERVER_STARTUP_LOCK_FILE = STATE_DIR / "server-startup.lock"
SESSION_FILE = STATE_DIR / "sessions.json"
LOCK_FILE = STATE_DIR / "state.lock"
SESSION_TTL_SECONDS = int(os.environ.get("SIGNAL_LIGHT_SESSION_TTL_SECONDS", "86400"))
WORK_SESSION_STALE_SECONDS = int(os.environ.get("SIGNAL_LIGHT_WORK_SESSION_STALE_SECONDS", "1800"))
IDLE_SLEEP_SECONDS = int(os.environ.get("SIGNAL_LIGHT_IDLE_SLEEP_SECONDS", "600"))
SERVER_POLL_SECONDS = float(os.environ.get("SIGNAL_LIGHT_SERVER_POLL_SECONDS", "1.0"))
SERVER_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("SIGNAL_LIGHT_SERVER_REQUEST_TIMEOUT_SECONDS", "1.0"))
SERVER_REQUEST_POLL_SECONDS = float(os.environ.get("SIGNAL_LIGHT_SERVER_REQUEST_POLL_SECONDS", "0.05"))

RED_SIGNALS = {"blocked"}
YELLOW_SIGNALS = {"permission", "attention", "done"}
WORKING_SIGNALS = {"thinking", "working", "tool_done"}
SESSION_END_SIGNALS = {"session_end"}
# Explicit clears should not look like session-completion cues.
SESSION_CLEAR_SIGNALS = {"off"}
SESSION_END_NOTICE_SIGNAL = "session_done"
TURN_END_SIGNALS = {"turn_end"}
# Sessions still waiting for user action should survive turn_end.
TURN_END_KEEP_SIGNALS = {"permission", "blocked"}
OWNER_PID_SOURCE = "explicit"


class ServerDisplay:
    """Single-process renderer for the physical signal light."""

    def __init__(self, light: SignalLight, *, speed: float = 1.0) -> None:
        self.light = light
        self.speed = speed
        self.aggregate = "idle"
        self.mode = "idle"
        self.frame_index = 0
        self.frame_deadline = 0.0
        self.notice_until = 0.0
        self.idle_since = time.monotonic()

    def set_aggregate(self, aggregate: str, *, show_notice: bool = False, speed: float | None = None) -> None:
        if speed is not None:
            self.speed = speed
        self.aggregate = aggregate
        if show_notice and aggregate not in RED_SIGNALS and aggregate not in YELLOW_SIGNALS:
            self.mode = SESSION_END_NOTICE_SIGNAL
            self.frame_index = 0
            self.frame_deadline = 0.0
            self.notice_until = time.monotonic() + _signal_duration(SIGNALS[SESSION_END_NOTICE_SIGNAL], self.speed)
        else:
            self.mode = aggregate
            self.frame_index = 0
            self.frame_deadline = 0.0
            self.notice_until = 0.0
        if aggregate == "idle" and self.mode == "idle":
            self.idle_since = time.monotonic()

    def tick(self) -> str | None:
        now = time.monotonic()
        if self.mode == SESSION_END_NOTICE_SIGNAL and now >= self.notice_until:
            self.mode = self.aggregate
            self.frame_index = 0
            self.frame_deadline = 0.0
            if self.aggregate == "idle":
                self.idle_since = now

        if self.mode == "idle" and now - self.idle_since >= IDLE_SLEEP_SECONDS:
            self._write_frame(Frame(seconds=0.01))
            self.mode = "off"
            self.frame_deadline = 0.0
            return "idle_sleep"

        signal_to_render = SIGNALS[self.mode]
        if signal_to_render.repeat or self.mode == SESSION_END_NOTICE_SIGNAL:
            self._tick_frames(signal_to_render, now)
            return None

        if self.frame_deadline == 0.0:
            self._render_steady(signal_to_render)
            self.frame_deadline = float("inf")
        return None

    def next_timeout(self) -> float:
        now = time.monotonic()
        deadlines = []
        if self.frame_deadline not in {0.0, float("inf")}:
            deadlines.append(self.frame_deadline)
        if self.notice_until:
            deadlines.append(self.notice_until)
        if self.mode == "idle":
            deadlines.append(self.idle_since + IDLE_SLEEP_SECONDS)
        if not deadlines:
            return SERVER_POLL_SECONDS
        return max(0.0, min(SERVER_POLL_SECONDS, min(deadlines) - now))

    def _tick_frames(self, signal_to_render: AgentSignal, now: float) -> None:
        frames = signal_to_render.frames
        if not frames:
            self._render_steady(signal_to_render)
            return
        if self.frame_deadline and now < self.frame_deadline:
            return

        frame = frames[self.frame_index % len(frames)]
        self._write_frame(frame)
        self.frame_index += 1
        self.frame_deadline = now + max(frame.seconds * max(self.speed, 0.05), 0.0)

    def _render_steady(self, signal_to_render: AgentSignal) -> None:
        if signal_to_render.leave_on is None:
            self.light.off()
            return
        green, yellow, red = signal_to_render.leave_on
        self.light.write(green=green, yellow=yellow, red=red)

    def _write_frame(self, frame: Frame) -> None:
        if not (frame.green or frame.yellow or frame.red):
            self.light.off()
            return

        brightness = max(0.0, min(1.0, frame.brightness))
        write_brightness = getattr(self.light, "write_brightness", None)
        if brightness < 1.0 and callable(write_brightness):
            write_brightness(
                green=brightness if frame.green else 0.0,
                yellow=brightness if frame.yellow else 0.0,
                red=brightness if frame.red else 0.0,
            )
            return
        self.light.write(green=frame.green, yellow=frame.yellow, red=frame.red)


def update_session_signal(
    session_key: str,
    signal_name: str,
    *,
    owner_pid: int | None = None,
) -> dict[str, object]:
    """Update one session state and return the aggregate without rendering it."""
    with _state_lock():
        state = _read_session_state()
        sessions = state.setdefault("sessions", {})
        now = time.time()
        _prune_sessions(sessions, now)
        should_show_session_end_notice = False
        direct_override_should_clear = False

        if signal_name in SESSION_END_SIGNALS:
            should_show_session_end_notice = True
            direct_override_should_clear = True
            sessions.pop(session_key, None)
        elif signal_name in SESSION_CLEAR_SIGNALS:
            direct_override_should_clear = session_key in sessions
            sessions.pop(session_key, None)
        elif signal_name in TURN_END_SIGNALS:
            current = sessions.get(session_key)
            current_signal = current.get("signal") if isinstance(current, dict) else None
            if current_signal not in TURN_END_KEEP_SIGNALS:
                should_show_session_end_notice = True
                direct_override_should_clear = True
                sessions.pop(session_key, None)
        else:
            direct_override_should_clear = True
            current = sessions.get(session_key)
            sessions[session_key] = {
                "signal": signal_name,
                "updated_at": now,
            }
            if isinstance(owner_pid, int) and owner_pid > 0:
                sessions[session_key]["owner_pid"] = owner_pid
                sessions[session_key]["owner_pid_source"] = OWNER_PID_SOURCE
            elif (
                isinstance(current, dict)
                and current.get("owner_pid_source") == OWNER_PID_SOURCE
                and isinstance(current.get("owner_pid"), int)
                and current["owner_pid"] > 0
            ):
                inherited_owner_pid = current["owner_pid"]
                sessions[session_key]["owner_pid"] = inherited_owner_pid
                sessions[session_key]["owner_pid_source"] = OWNER_PID_SOURCE

        if direct_override_should_clear:
            state.pop("direct_signal", None)

        aggregate = aggregate_sessions(sessions)
        _write_session_state(state)
        return {
            "aggregate": aggregate,
            "show_notice": should_show_session_end_notice,
        }


def clear_session_state() -> None:
    """Clear all tracked Codex session states."""
    with _state_lock():
        _write_session_state({"sessions": {}})


def aggregate_sessions(sessions: dict[str, object]) -> str:
    signals = []
    for value in sessions.values():
        if isinstance(value, dict):
            signal_name = value.get("signal")
            if isinstance(signal_name, str):
                signals.append(signal_name)

    if any(signal_name in RED_SIGNALS for signal_name in signals):
        return "blocked"
    if any(signal_name == "permission" for signal_name in signals):
        return "permission"
    if any(signal_name in YELLOW_SIGNALS for signal_name in signals):
        return "attention"
    if any(signal_name in WORKING_SIGNALS for signal_name in signals):
        return "working"
    return "idle"


def read_session_snapshot() -> dict[str, object]:
    with _state_lock():
        return _read_session_snapshot_unlocked()


def update_direct_signal(signal_name: str) -> dict[str, object]:
    with _state_lock():
        if signal_name not in SIGNALS:
            raise SignalLightError(f"Unknown direct signal: {signal_name}")
        state = _read_session_state()
        if signal_name in {"idle", "off"}:
            state["sessions"] = {}
        state["direct_signal"] = signal_name
        _write_session_state(state)
        return _read_display_snapshot_unlocked()


def submit_direct_signal(signal_name: str, *, speed: float = 1.0) -> dict[str, object]:
    return _send_server_request(
        {
            "action": "direct_signal",
            "signal_name": signal_name,
            "speed": speed,
        }
    )


def submit_session_signal(
    session_key: str,
    signal_name: str,
    *,
    owner_pid: int | None = None,
    speed: float = 1.0,
) -> dict[str, object]:
    return _send_server_request(
        {
            "action": "session_signal",
            "session_key": session_key,
            "signal_name": signal_name,
            "owner_pid": owner_pid,
            "speed": speed,
        }
    )


def run_server() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with _server_process_lock():
        try:
            SERVER_PID_FILE.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "started_at": time.time(),
                    },
                    ensure_ascii=False,
                )
            )
            with SignalLight(LightMapping.from_env(os.environ)) as light:
                display = ServerDisplay(light)
                display.set_aggregate(str(read_display_snapshot()["display_signal"]))
                _prepare_server_ipc()
                while True:
                    reconciled = _reconcile_server_sessions()
                    if reconciled["changed"]:
                        display.set_aggregate(str(reconciled["aggregate"]))
                    if display.tick() == "idle_sleep":
                        update_direct_signal("off")
                    _handle_server_requests(display)
                    time.sleep(min(display.next_timeout(), SERVER_REQUEST_POLL_SECONDS))
        finally:
            _clear_pid_file(SERVER_PID_FILE, expected_pid=os.getpid())
            _clear_server_ipc()


def _read_session_snapshot_unlocked() -> dict[str, object]:
    state = _read_session_state()
    return _read_session_snapshot_from_state(state)


def read_display_snapshot() -> dict[str, object]:
    with _state_lock():
        return _read_display_snapshot_unlocked()


def _read_display_snapshot_unlocked() -> dict[str, object]:
    state = _read_session_state()
    return _read_display_snapshot_from_state(state)


def _read_display_snapshot_from_state(state: dict[str, object]) -> dict[str, object]:
    snapshot = _read_session_snapshot_from_state(state)
    direct_signal = state.get("direct_signal")
    display_signal = direct_signal if isinstance(direct_signal, str) and direct_signal in SIGNALS else snapshot["aggregate"]
    return {
        **snapshot,
        "display_signal": display_signal,
    }


def _read_session_snapshot_from_state(state: dict[str, object]) -> dict[str, object]:
    sessions = state.get("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
    now = time.time()
    _prune_sessions(sessions, now)
    _prune_dead_owner_sessions(sessions)
    aggregate = aggregate_sessions(sessions)
    return {
        "aggregate": aggregate,
        "sessions": sessions,
    }


def _signal_duration(signal_to_render: AgentSignal, speed: float) -> float:
    return sum(max(frame.seconds * max(speed, 0.05), 0.0) for frame in signal_to_render.frames) * signal_to_render.loops


@contextmanager
def _state_lock() -> Iterator[None]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except Exception:
                pass


def _read_session_state() -> dict[str, object]:
    try:
        state = json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sessions": {}}

    if not isinstance(state, dict):
        return {"sessions": {}}
    if not isinstance(state.get("sessions"), dict):
        state["sessions"] = {}
    return state


def _write_session_state(state: dict[str, object]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _prune_sessions(sessions: dict[str, object], now: float) -> None:
    expired = []
    for session_key, value in sessions.items():
        if not isinstance(value, dict):
            expired.append(session_key)
            continue
        updated_at = value.get("updated_at")
        if not isinstance(updated_at, (int, float)) or now - updated_at > SESSION_TTL_SECONDS:
            expired.append(session_key)
            continue
        signal_name = value.get("signal")
        if "owner_pid" in value and value.get("owner_pid_source") != OWNER_PID_SOURCE:
            if signal_name in WORKING_SIGNALS:
                expired.append(session_key)
                continue
            value.pop("owner_pid", None)
        if signal_name in WORKING_SIGNALS and now - updated_at > WORK_SESSION_STALE_SECONDS:
            expired.append(session_key)

    for session_key in expired:
        sessions.pop(session_key, None)


def _prune_dead_owner_sessions(sessions: dict[str, object]) -> bool:
    expired = []
    for session_key, value in sessions.items():
        if not isinstance(value, dict):
            continue
        if value.get("owner_pid_source") != OWNER_PID_SOURCE:
            continue
        owner_pid = value.get("owner_pid")
        if isinstance(owner_pid, int) and owner_pid > 0 and not _is_running(owner_pid):
            expired.append(session_key)

    for session_key in expired:
        sessions.pop(session_key, None)
    return bool(expired)


def _server_error_message(log_file: Path) -> str:
    detail = ""
    try:
        detail = log_file.read_text(errors="replace").strip().splitlines()[-1]
    except (FileNotFoundError, IndexError):
        pass

    if detail:
        return f"Signal server exited immediately: {detail}"
    return "Signal server exited immediately."


def _server_running() -> bool:
    pid = _read_pid_file(SERVER_PID_FILE).get("pid")
    return isinstance(pid, int) and pid > 0 and _is_running(pid) and _server_accepts_connections()


def _ensure_server_running() -> None:
    if _server_running():
        return

    with _server_startup_lock():
        if _server_running():
            return
        if _server_process_lock_is_held():
            if _wait_for_running_server(timeout=3.0):
                return
            _stop_unreachable_server()
            if _wait_for_server_lock_release(timeout=2.0) and _server_running():
                return
            if _server_process_lock_is_held():
                raise SignalLightError("Signal server is starting but did not become reachable in time.")

        _clear_pid_file(SERVER_PID_FILE)
        _clear_server_ipc()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "signal_light",
            "server",
        ]
        log = SERVER_LOG_FILE.open("ab")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=PROJECT_ROOT,
                env=os.environ.copy(),
                start_new_session=True,
            )
        finally:
            log.close()

        deadline = time.monotonic() + 3.0
        process_exited = False
        while time.monotonic() < deadline:
            if _server_running():
                return
            if process.poll() is not None:
                process_exited = True
            time.sleep(0.05)

        if process_exited:
            raise SignalLightError(_server_error_message(SERVER_LOG_FILE))
        _stop_process(process.pid)
        _clear_pid_file(SERVER_PID_FILE, expected_pid=process.pid)
        _clear_server_ipc()
        raise SignalLightError("Signal server did not start in time.")


def _send_server_request(payload: dict[str, object], *, start_if_missing: bool = True) -> dict[str, object]:
    if start_if_missing:
        _ensure_server_running()
    elif not _server_running():
        raise SignalLightError("Signal server is not running.")

    try:
        return _send_server_request_once(payload)
    except OSError:
        if not start_if_missing:
            raise SignalLightError("Signal server is not running.")
        if not _stop_unreachable_server():
            raise SignalLightError("Signal server is unreachable and could not be stopped.")
        if not _wait_for_server_lock_release(timeout=2.0):
            raise SignalLightError("Signal server is unreachable and still holding the server lock.")
        _clear_pid_file(SERVER_PID_FILE)
        _clear_server_ipc()
        _ensure_server_running()
        try:
            return _send_server_request_once(payload)
        except OSError as exc:
            raise SignalLightError(f"Cannot reach signal server: {exc}") from exc


def _server_accepts_connections() -> bool:
    try:
        _send_server_request_once({"action": "status"})
    except Exception:
        return False
    return True


def _send_server_request_once(payload: dict[str, object]) -> dict[str, object]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    request_id = f"{os.getpid()}-{time.monotonic_ns()}-{uuid.uuid4().hex}"
    request_path = SERVER_REQUEST_DIR / f"{request_id}.request.json"
    response_path = SERVER_REQUEST_DIR / f"{request_id}.response.json"
    request_tmp_path = request_path.with_suffix(".tmp")
    request_tmp_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n")
    request_tmp_path.replace(request_path)

    deadline = time.monotonic() + SERVER_REQUEST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            data = response_path.read_bytes()
            response_path.unlink()
            break
        except FileNotFoundError:
            time.sleep(min(SERVER_REQUEST_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    else:
        try:
            request_path.unlink()
        except FileNotFoundError:
            pass
        raise TimeoutError("Signal server did not respond in time.")

    try:
        response = json.loads(data.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise SignalLightError("Signal server returned invalid JSON.") from exc

    if not isinstance(response, dict):
        raise SignalLightError("Signal server returned an unexpected response.")
    if response.get("ok") is False:
        raise SignalLightError(str(response.get("error") or "Signal server request failed."))
    return response


def _handle_server_requests(display: ServerDisplay) -> None:
    SERVER_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    for request_path in sorted(SERVER_REQUEST_DIR.glob("*.request.json")):
        response_path = SERVER_REQUEST_DIR / request_path.name.replace(".request.json", ".response.json")
        try:
            request = json.loads(request_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        finally:
            try:
                request_path.unlink()
            except FileNotFoundError:
                pass

        response = _handle_server_request(request, display)
        response_tmp_path = response_path.with_suffix(".tmp")
        response_tmp_path.write_text(json.dumps(response, ensure_ascii=False) + "\n")
        response_tmp_path.replace(response_path)

def _handle_server_request(request: object, display: ServerDisplay) -> dict[str, object]:
    if not isinstance(request, dict):
        return {"ok": False, "error": "Invalid request payload."}

    action = request.get("action")
    try:
        if action == "status":
            snapshot = read_display_snapshot()
            return {"ok": True, **snapshot}

        if action == "direct_signal":
            signal_name = request.get("signal_name")
            speed = float(request.get("speed", 1.0))
            if not isinstance(signal_name, str) or signal_name not in SIGNALS:
                return {"ok": False, "error": "Unknown direct signal."}
            snapshot = update_direct_signal(signal_name)
            display.set_aggregate(str(snapshot["display_signal"]), speed=speed)
            return {"ok": True, "signal": signal_name, **snapshot}

        if action == "session_signal":
            session_key = request.get("session_key")
            signal_name = request.get("signal_name")
            owner_pid = request.get("owner_pid")
            speed = float(request.get("speed", 1.0))
            if not isinstance(session_key, str) or not session_key:
                return {"ok": False, "error": "Missing session key."}
            if not isinstance(signal_name, str):
                return {"ok": False, "error": "Missing signal name."}
            result = update_session_signal(
                session_key,
                signal_name,
                owner_pid=owner_pid if isinstance(owner_pid, int) else None,
            )
            aggregate = str(result["aggregate"])
            display.set_aggregate(aggregate, show_notice=bool(result["show_notice"]), speed=speed)
            snapshot = read_session_snapshot()
            return {"ok": True, "aggregate": aggregate, **snapshot}
    except SignalLightError as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": False, "error": f"Unsupported action: {action}"}


def _reconcile_server_sessions() -> dict[str, object]:
    with _state_lock():
        state = _read_session_state()
        sessions = state.setdefault("sessions", {})
        before = json.dumps(sessions, sort_keys=True, ensure_ascii=False)
        now = time.time()
        _prune_sessions(sessions, now)
        changed = _prune_dead_owner_sessions(sessions)
        changed = changed or before != json.dumps(sessions, sort_keys=True, ensure_ascii=False)
        snapshot = _read_display_snapshot_from_state(state)
        if changed:
            _write_session_state(state)
    return {
        "changed": changed,
        "aggregate": snapshot["display_signal"],
    }


@contextmanager
def _server_process_lock() -> Iterator[object]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = SERVER_LOCK_FILE.open("a+")
    try:
        import fcntl

        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SignalLightError("Signal server is already running.") from exc
        yield lock_file
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_file.close()


@contextmanager
def _server_startup_lock() -> Iterator[object]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with SERVER_STARTUP_LOCK_FILE.open("a+") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_EX)
            yield lock_file
        finally:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except Exception:
                pass


def _server_process_lock_is_held() -> bool:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with SERVER_LOCK_FILE.open("a+") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except Exception:
            pass
    return False


def _wait_for_running_server(*, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _server_running():
            return True
        time.sleep(0.05)
    return _server_running()


def _wait_for_server_lock_release(*, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _server_process_lock_is_held():
            return True
        time.sleep(0.05)
    return not _server_process_lock_is_held()


def _stop_unreachable_server() -> bool:
    pid = _read_pid_file(SERVER_PID_FILE).get("pid")
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    if not _is_running(pid):
        _clear_pid_file(SERVER_PID_FILE)
        _clear_server_ipc()
        return True

    return _stop_process(pid)


def _stop_process(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return _wait_for_process_exit(pid, timeout=1.0)


def _wait_for_process_exit(pid: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_running(pid):
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_running(pid):
            return True
        time.sleep(0.05)
    return not _is_running(pid)


def _prepare_server_ipc() -> None:
    _clear_server_socket()
    SERVER_REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    for path in SERVER_REQUEST_DIR.glob("*.response.json"):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _clear_server_ipc() -> None:
    _clear_server_socket()
    for path in SERVER_REQUEST_DIR.glob("*.request.json"):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _clear_server_socket() -> None:
    try:
        SERVER_SOCKET_FILE.unlink()
    except FileNotFoundError:
        pass


def _clear_pid_file(pid_file: Path, *, expected_pid: int | None = None) -> None:
    if expected_pid is not None:
        state = _read_pid_file(pid_file)
        if state.get("pid") != expected_pid:
            return

    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass


def _read_pid_file(pid_file: Path) -> dict[str, object]:
    try:
        return json.loads(pid_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
