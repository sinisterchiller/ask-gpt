"""Startup port-cleanup for the ChatGPT Bridge WebSocket server.

On startup, before binding the WebSocket server port (8765), discovers any
existing LISTEN socket on that port, verifies it belongs to a stale
chatgpt-bridge process (same user, same project cwd, ``python -m server``
command), and terminates it. Unverified listeners cause startup to fail
safely rather than blindly killing an unrelated service.

Also manages a PID file so that a subsequent bridge instance can identify
its predecessor across process restarts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PORT = 8765

# Assuming this file is:
# ask-gpt/server/port_cleanup.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]

RUNTIME_DIR = PROJECT_ROOT / ".runtime"
PID_FILE = RUNTIME_DIR / f"websocket-{PORT}.pid.json"

# The bridge is launched as: python -m server
EXPECTED_MODULE = "server"


try:
    import psutil
except ImportError:
    psutil = None


class PortCleanupError(RuntimeError):
    """Raised when port cleanup cannot safely proceed."""

    pass


# ---------------------------------------------------------------------------
# Listener discovery
# ---------------------------------------------------------------------------


def _listener_pids_lsof(port: int) -> set[int]:
    """Return PIDs with a TCP socket in LISTEN state on *port* (macOS/Linux)."""
    if shutil.which("lsof") is None:
        raise FileNotFoundError("lsof not installed")

    result = subprocess.run(
        [
            "lsof",
            "-nP",
            "-t",
            f"-iTCP:{port}",
            "-sTCP:LISTEN",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    # lsof returns 1 when there are simply no matches.
    if result.returncode not in (0, 1):
        raise PortCleanupError(
            f"lsof failed with exit code {result.returncode}: "
            f"{result.stderr.strip()}"
        )

    pids: set[int] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return pids


def _listener_pids_fuser(port: int) -> set[int]:
    """Linux fallback using ``fuser``."""
    if shutil.which("fuser") is None:
        raise FileNotFoundError("fuser not installed")

    result = subprocess.run(
        ["fuser", "-n", "tcp", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if result.returncode not in (0, 1):
        raise PortCleanupError(
            f"fuser failed with exit code {result.returncode}: "
            f"{result.stderr.strip()}"
        )

    return {int(value) for value in re.findall(r"\b\d+\b", result.stdout)}


def _listener_pids_psutil(port: int) -> set[int]:
    """psutil-based discovery (may need elevated privileges on macOS)."""
    if psutil is None:
        raise RuntimeError("psutil is not installed")

    pids: set[int] = set()
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, PermissionError) as exc:
        raise PortCleanupError(
            "psutil cannot enumerate system TCP connections"
        ) from exc

    for conn in connections:
        if conn.status != psutil.CONN_LISTEN:
            continue
        if not conn.laddr:
            continue
        if conn.laddr.port != port:
            continue
        if conn.pid is not None:
            pids.add(conn.pid)
    return pids


def find_listener_pids(port: int) -> set[int]:
    """Discover PIDs listening on *port*.

    Unix: ``lsof`` → ``fuser`` → ``psutil``
    Windows: ``psutil``
    """
    if os.name == "nt":
        if psutil is None:
            raise PortCleanupError(
                "Port cleanup on Windows requires psutil. "
                "Install it with: pip install psutil"
            )
        return _listener_pids_psutil(port)

    try:
        return _listener_pids_lsof(port)
    except FileNotFoundError:
        pass

    try:
        return _listener_pids_fuser(port)
    except FileNotFoundError:
        pass

    if psutil is not None:
        return _listener_pids_psutil(port)

    raise PortCleanupError(
        "Cannot determine which process owns the WebSocket port. "
        "Install lsof or psutil."
    )


# ---------------------------------------------------------------------------
# Process identity checks
# ---------------------------------------------------------------------------


def _read_pid_file() -> dict | None:
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _pid_file_matches(pid: int) -> bool:
    """Check whether the previous bridge PID file identifies *pid*."""
    info = _read_pid_file()
    if not info:
        return False
    if info.get("pid") != pid:
        return False
    if info.get("project_root") != str(PROJECT_ROOT):
        return False
    if psutil is not None and "create_time" in info:
        try:
            process = psutil.Process(pid)
            actual = process.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        if abs(actual - float(info["create_time"])) > 0.01:
            return False
    return True


def _unix_process_uid(pid: int) -> int | None:
    if not hasattr(os, "getuid"):
        return None
    result = subprocess.run(
        ["ps", "-o", "uid=", "-p", str(pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    if not value.isdigit():
        return None
    return int(value)


def _unix_process_command(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _unix_process_cwd(pid: int) -> Path | None:
    # Linux has /proc/[pid]/cwd.
    proc_cwd = Path(f"/proc/{pid}/cwd")
    if proc_cwd.exists():
        try:
            return proc_cwd.resolve()
        except OSError:
            pass
    # macOS / general Unix fallback via lsof.
    if shutil.which("lsof"):
        result = subprocess.run(
            [
                "lsof",
                "-a",
                "-p",
                str(pid),
                "-d",
                "cwd",
                "-Fn",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                value = line[1:]
                if value:
                    try:
                        return Path(value).resolve()
                    except OSError:
                        return None
    return None


def _looks_like_our_bridge_psutil(pid: int) -> bool:
    """Check whether *pid* looks like our bridge using psutil."""
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False

    if pid == os.getpid():
        return False

    if hasattr(os, "getuid"):
        try:
            if process.uids().real != os.getuid():
                return False
        except (psutil.AccessDenied, AttributeError):
            return False

    try:
        cwd = Path(process.cwd()).resolve()
    except (psutil.AccessDenied, FileNotFoundError, OSError):
        return False

    try:
        argv = process.cmdline()
    except psutil.AccessDenied:
        return False

    if cwd != PROJECT_ROOT:
        return False

    # Require the exact ``-m server`` pattern.
    module_match = any(
        argv[i] == "-m" and argv[i + 1] == EXPECTED_MODULE
        for i in range(len(argv) - 1)
    )
    return module_match


def _looks_like_our_bridge_unix(pid: int) -> bool:
    """Check whether *pid* looks like our bridge using pure-subprocess."""
    if pid == os.getpid():
        return False

    if hasattr(os, "getuid"):
        uid = _unix_process_uid(pid)
        if uid is None or uid != os.getuid():
            return False

    cwd = _unix_process_cwd(pid)
    if cwd is None or cwd != PROJECT_ROOT:
        return False

    command = _unix_process_command(pid)
    if not command:
        return False

    module_pattern = re.compile(
        rf"(?:^|\s)-m\s+{re.escape(EXPECTED_MODULE)}(?:\s|$)"
    )
    return bool(module_pattern.search(command))


def looks_like_our_bridge(pid: int) -> bool:
    """Conservative identity check for a stale bridge process.

    PID-file match is strongest. Otherwise require same user, same project
    cwd, and ``python -m server`` command pattern.
    """
    if pid == os.getpid():
        return False
    if _pid_file_matches(pid):
        return True
    if psutil is not None:
        return _looks_like_our_bridge_psutil(pid)
    if os.name != "nt":
        return _looks_like_our_bridge_unix(pid)
    return False


# ---------------------------------------------------------------------------
# Termination helpers
# ---------------------------------------------------------------------------


def _pid_exists(pid: int) -> bool:
    if psutil is not None:
        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return False


def _terminate_with_psutil(pid: int, timeout: float) -> None:
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    print(
        f"[bridge] sending SIGTERM to stale bridge process PID {pid}",
        file=sys.stderr,
    )
    try:
        process.terminate()
    except psutil.NoSuchProcess:
        return

    try:
        process.wait(timeout=timeout)
    except psutil.TimeoutExpired:
        pass

    if not _pid_exists(pid):
        return

    print(
        f"[bridge] PID {pid} did not exit after {timeout:.1f}s; "
        "sending SIGKILL",
        file=sys.stderr,
    )
    try:
        process.kill()
        process.wait(timeout=1.0)
    except psutil.NoSuchProcess:
        pass


def _terminate_with_unix_kill(pid: int, timeout: float) -> None:
    if shutil.which("kill") is None:
        raise PortCleanupError("Unix kill command isn't available")

    print(
        f"[bridge] sending SIGTERM to stale bridge process PID {pid}",
        file=sys.stderr,
    )
    subprocess.run(
        ["kill", "-TERM", str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.05)

    if not _pid_exists(pid):
        return

    print(
        f"[bridge] PID {pid} did not exit after {timeout:.1f}s; "
        "sending SIGKILL",
        file=sys.stderr,
    )
    subprocess.run(
        ["kill", "-KILL", str(pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def terminate_bridge_process(pid: int, timeout: float = 2.0) -> None:
    """Terminate a verified stale bridge process."""
    if pid == os.getpid():
        raise PortCleanupError("Refusing to terminate the current process")

    if not looks_like_our_bridge(pid):
        raise PortCleanupError(
            f"Refusing to terminate PID {pid}: "
            "it cannot be verified as another chatgpt-bridge process."
        )

    if psutil is not None:
        _terminate_with_psutil(pid, timeout)
    elif os.name != "nt":
        _terminate_with_unix_kill(pid, timeout)
    else:
        raise PortCleanupError("Process termination on Windows requires psutil.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def cleanup_stale_listener(port: int = PORT, timeout: float = 2.0) -> None:
    """Call once at startup, before binding the WebSocket port.

    Discovers stale listeners, verifies identity, terminates them, and
    confirms the port is free.
    """
    current_pid = os.getpid()
    listeners = find_listener_pids(port)
    listeners.discard(current_pid)

    if not listeners:
        return

    print(
        f"[bridge] port {port} already has listener(s): "
        f"{sorted(listeners)}",
        file=sys.stderr,
    )

    # Validate ALL listeners before killing ANY — prevents partial damage.
    unrecognized = [
        pid for pid in listeners if not looks_like_our_bridge(pid)
    ]
    if unrecognized:
        raise PortCleanupError(
            f"Port {port} is occupied by process(es) "
            f"{unrecognized}, which cannot be verified as stale "
            "chatgpt-bridge processes. Refusing to kill them."
        )

    for pid in listeners:
        terminate_bridge_process(pid, timeout=timeout)

    # Verify the port actually became free.
    # The kernel may take a moment to release the socket after a process dies,
    # so retry a few times with small delays before giving up.
    for _retry in range(5):
        remaining = find_listener_pids(port)
        remaining.discard(current_pid)
        if not remaining:
            break
        time.sleep(0.2)

    remaining = find_listener_pids(port)
    remaining.discard(current_pid)
    if remaining:
        raise PortCleanupError(
            f"Could not free port {port}; still listening: "
            f"{sorted(remaining)}"
        )


def write_pid_file(port: int = PORT) -> None:
    """Call only after the WebSocket bind succeeds."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    info = {
        "pid": os.getpid(),
        "port": port,
        "project_root": str(PROJECT_ROOT),
        "written_at": time.time(),
    }
    if psutil is not None:
        try:
            info["create_time"] = psutil.Process().create_time()
        except psutil.Error:
            pass
    temp_file = PID_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(info, indent=2), encoding="utf-8")
    temp_file.replace(PID_FILE)


def remove_pid_file() -> None:
    """Remove the PID file only if it still belongs to this process."""
    info = _read_pid_file()
    if not info:
        return
    if info.get("pid") != os.getpid():
        return
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
