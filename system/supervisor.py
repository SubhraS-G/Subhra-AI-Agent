"""Keep SURA online — the server, and optionally the wake-word listener.

The app window (the installed PWA) opens at login whether or not the server
is up yet, so *something* has to guarantee the server is there and stays
there. `supervise` runs the normal `serve` command as a child process,
watches it, and relaunches it if it ever exits. With `with_voice` it also
supervises `cli listen`, so "hey SURA" survives a crash, a busy mic that
clears, or a reboot.

It stays out of the way when a server is already listening (one started by
hand, or by an older supervisor), and holds a lock so only one supervisor
ever runs.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from system.logging_utils import log
from system.paths import AGENT_ROOT

RESTART_DELAY_SECONDS = 5.0   # pause before relaunching after a crash
MAX_RESTART_DELAY = 60.0      # backoff cap for a child that keeps dying young
HEALTHY_SECONDS = 60.0        # a child alive this long earns the fast restart
PROBE_SECONDS = 2.0           # how often to check the port / child processes
LOCK_NAME = "supervisor.lock"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """True when something is accepting TCP connections on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def serve_command(
    python: str,
    port: int,
    share: bool = False,
    pairing_key: str = "",
    tunnel: bool = False,
) -> list[str]:
    """Build the `serve` argv the supervisor launches and relaunches.

    `-u` matters: the login launcher redirects output to a log file, and
    without unbuffered mode that file would stay empty (and useless for
    diagnosis) for as long as the server runs.
    """
    command = [python, "-u", "-m", "cli", "serve", "--port", str(port)]
    if share or tunnel:
        command.append("--share")
    if pairing_key:
        command += ["--key", pairing_key]
    if tunnel:
        command.append("--tunnel")
    return command


def listen_command(python: str) -> list[str]:
    """The wake-word listener — supervised exactly like the server."""
    return [python, "-u", "-m", "cli", "listen"]


def open_log(log_path: Path):
    """Open the launcher log for appending, creating its folder if needed."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Line buffered: the log is meant to be readable while SURA runs.
    return open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)


def acquire_lock(path: Path):
    """Take the single-supervisor lock, or return None if another holds it.

    Two watchers would race to relaunch a dead child, and the loser would
    spin forever on a port it cannot bind. Windows drops the lock when the
    process ends, so a crashed supervisor never blocks the next login.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        import msvcrt
    except ImportError:      # not Windows (SURA only targets Windows)
        return handle
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


@dataclass
class _Child:
    """One supervised process, with its own restart clock."""

    name: str
    command: list[str]
    log_handle: object | None = None
    probe_port: int | None = None      # skip spawning while this port serves
    process: subprocess.Popen | None = None
    delay: float = RESTART_DELAY_SECONDS
    retry_at: float = 0.0
    started_at: float = 0.0

    def note(self, text: str) -> None:
        if self.log_handle is not None:
            self.log_handle.write(text)


def _tick(child: _Child, now: float, host: str) -> None:
    """Restart logic for one child: check liveness, then (re)spawn if due."""
    if child.process is not None and child.process.poll() is not None:
        code = child.process.returncode
        uptime = now - child.started_at
        log("supervisor_child_exited", name=child.name, code=code, uptime_s=int(uptime))
        child.process = None
        child.retry_at = now + child.delay
        # Backoff only for a child that dies young (a busy mic, a broken
        # model); a long healthy run gets the fast restart again.
        child.delay = (RESTART_DELAY_SECONDS if uptime >= HEALTHY_SECONDS
                       else min(MAX_RESTART_DELAY, child.delay * 2))
        child.note(f"\n[supervisor] {child.name} exited ({code}) "
                   f"— restarting in {child.retry_at - now:.0f}s\n")
    if child.process is None and now >= child.retry_at:
        if child.probe_port is not None and port_open(child.probe_port, host):
            # Already healthy — a manual `cli serve`, or an older supervisor.
            log("supervisor_server_already_up", port=child.probe_port)
            return
        child.process = subprocess.Popen(
            child.command, cwd=str(AGENT_ROOT), stdin=subprocess.DEVNULL,
            stdout=child.log_handle,
            stderr=subprocess.STDOUT if child.log_handle else None,
        )
        child.started_at = now
        log("supervisor_child_started", name=child.name, pid=child.process.pid)


def supervise(
    port: int,
    share: bool = False,
    pairing_key: str = "",
    tunnel: bool = False,
    host: str = "127.0.0.1",
    python: str | None = None,
    log_path: Path | str | None = None,
    with_voice: bool = False,
    voice_log_path: Path | str | None = None,
) -> int:
    """Block forever, keeping the server (and optionally voice) alive.

    log_path is where the server's own output goes, and it must be handed to
    the child explicitly: a child process does NOT inherit a windowless
    (pythonw) parent's redirected stdout, which is how the autostart log used
    to stay empty. Without log_path the children inherit this process's
    stdout, which is what you want when running `cli supervise` in a console.
    """
    python = python or sys.executable
    log("supervisor_started", port=port, share=share or tunnel, with_voice=with_voice)
    handle = open_log(Path(log_path)) if log_path else None
    if handle:
        handle.write("\n" + "=" * 64
                     + f"\nSURA supervisor started {time.strftime('%Y-%m-%d %H:%M:%S')}"
                     + f"\n  listening on :{port}  (shared: {bool(share or tunnel)}"
                     + f", voice: {with_voice})"
                     + "\n" + "=" * 64 + "\n")
    lock = acquire_lock(AGENT_ROOT / "logs" / LOCK_NAME)
    if lock is None:
        log("supervisor_already_running", port=port)
        if handle:
            handle.write("[supervisor] another supervisor is already keeping SURA "
                         "online — exiting.\n")
            handle.close()
        return 0

    children = [_Child("server", serve_command(python, port, share, pairing_key, tunnel),
                       log_handle=handle, probe_port=port)]
    voice_handle = None
    if with_voice:
        voice_handle = open_log(Path(voice_log_path)) if voice_log_path else handle
        children.append(_Child("voice", listen_command(python), log_handle=voice_handle))
    try:
        while True:
            now = time.monotonic()
            for child in children:
                _tick(child, now, host)
            time.sleep(PROBE_SECONDS)
    finally:
        if handle:
            handle.close()
        if voice_handle is not None and voice_handle is not handle:
            voice_handle.close()
        lock.close()          # releases the single-supervisor lock
