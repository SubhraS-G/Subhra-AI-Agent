"""Auto-start at login — free, dependency-free Windows autostart.

Writes a launcher into the user's Startup folder (`shell:startup`): one .vbs
that starts SURA's web UI hidden (no console window) at every login. It runs
`cli supervise`, which keeps the server alive and restarts it if it ever
stops — so the app window that also opens at login always finds a live
server, with nothing to type.

Everything is plain files in the user's own profile — nothing is written to
the registry or anywhere admin rights would be needed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

STARTUP_VBS_NAME = "SURA_autostart.vbs"
# Two logs on purpose: cmd holds its redirect open for the whole session, so
# anything SURA itself wants to write has to go to a file cmd does not hold.
LOG_NAME = "serve-autostart.log"           # SURA's own output (supervisor owns it)
LAUNCHER_LOG_NAME = "serve-launcher.log"   # raw launcher output — pre-start crashes
VOICE_LOG_NAME = "voice-autostart.log"     # the wake-word listener's output
LOG_MAX_BYTES = 1_000_000                  # a fresh install resets oversized logs
LAUNCH_COMMAND = "supervise"               # keeps the server up (see system/supervisor.py)

_VBS_TEMPLATE = """\
' SURA auto-start launcher (standalone, window style 0 = fully hidden).
' Installed by: python -m cli autostart
' Remove with:  python -m cli autostart --uninstall
' serve-autostart.log  = SURA's own output (written by the supervisor)
' serve-launcher.log   = anything that fails before SURA's server starts
' voice-autostart.log  = the wake-word listener's output (voice mode only)
Set shell = CreateObject("WScript.Shell")
pythonw = "{pythonw}"
root = "{root}"
logfile = "{logfile}"
voicelog = "{voice_log}"
launcherlog = "{launcher_log}"
cmd = Chr(34) & pythonw & Chr(34) & " -m cli {command}{serve_flags} --log " & Chr(34) & logfile & Chr(34){voice_flags}
shell.CurrentDirectory = root
shell.Run "cmd /c cd /d " & Chr(34) & root & Chr(34) & " && " & cmd _
    & " >> " & Chr(34) & launcherlog & Chr(34) & " 2>&1", 0, False
"""


class StartupInstallError(Exception):
    pass


def _pythonw() -> Path:
    cand = Path(sys.executable).with_name("pythonw.exe")
    if cand.exists():
        return cand
    raise StartupInstallError(
        f"pythonw.exe not found next to {sys.executable} — cannot launch without a console window"
    )


def startup_dir() -> Path:
    """The user's Startup folder (shell:startup)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise StartupInstallError("APPDATA is not set — not a normal Windows session?")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _launcher_path() -> Path:
    return startup_dir() / STARTUP_VBS_NAME


def install_startup(
    with_voice: bool = False,
    share: bool = False,
    pairing_key: str = "",
    tunnel: bool = False,
) -> Path:
    """Write the hidden launcher .vbs into the Startup folder. Returns its path.

    share=True starts SURA in phone-accessible mode at every login. A stable
    pairing key is generated once and saved to config/agent_config.yaml so
    the phone's stored key keeps working across reboots. tunnel=True adds
    the Tailscale banner (--tunnel) so anywhere-access is advertised too.
    """
    serve_flags = ""
    if share:
        serve_flags = f" --share --key {pairing_key}" if pairing_key else " --share"
    if tunnel:
        serve_flags += " --tunnel"
    pythonw = _pythonw()
    root = Path(__file__).resolve().parent.parent
    logdir = root / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / LOG_NAME
    voice_log = logdir / VOICE_LOG_NAME
    launcher_log = logdir / LAUNCHER_LOG_NAME
    for path in (logfile, voice_log, launcher_log):
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.unlink()       # keep the login logs from growing forever
    # Voice mode adds a second supervised child (the wake-word listener), so
    # "hey SURA" comes back by itself after a crash or a reboot.
    voice_flags = ""
    if with_voice:
        voice_flags = ' & " --with-voice --voice-log " & Chr(34) & voicelog & Chr(34)'
    # --log is what makes the log useful: a windowless child cannot inherit
    # this process's redirected stdout, so the launcher tells the supervisor
    # where to send the server's output (see system/supervisor.py).
    body = _VBS_TEMPLATE.format(
        pythonw=str(pythonw), root=str(root), logfile=str(logfile),
        voice_log=str(voice_log), launcher_log=str(launcher_log),
        command=LAUNCH_COMMAND, serve_flags=serve_flags, voice_flags=voice_flags,
    )
    path = _launcher_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def uninstall_startup() -> bool:
    """Remove the launcher; True when something was actually removed."""
    path = _launcher_path()
    if path.exists():
        path.unlink()
        return True
    return False


def startup_status() -> dict:
    """Report current autostart state without changing anything."""
    try:
        path = _launcher_path()
    except StartupInstallError as exc:
        return {"installed": False, "error": str(exc)}
    body = path.read_text(encoding="utf-8") if path.exists() else ""
    shared = "--share" in body
    return {
        "installed": path.exists(),
        "shared": shared,
        "tunnel": "--tunnel" in body,
        "path": str(path),
        "ui_url": "http://127.0.0.1:8756",
        "voice": "--with-voice" in body,
        "log": str(Path(__file__).resolve().parent.parent / "logs" / LOG_NAME),
        "voice_log": str(Path(__file__).resolve().parent.parent / "logs" / VOICE_LOG_NAME),
        "launcher_log": str(Path(__file__).resolve().parent.parent / "logs" / LAUNCHER_LOG_NAME),
    }


def is_served_by_autostart() -> bool:
    """Best-effort check whether this process was started by the launcher.

    Windows gives console-less processes no parent chain worth walking, so we
    check for a log file created by the autostart redirect within this boot.
    """
    log_file = Path(__file__).resolve().parent.parent / "logs" / LOG_NAME
    if not log_file.exists():
        return False
    try:
        return log_file.stat().st_mtime > _boot_time()
    except OSError:
        return False


def _boot_time() -> float:
    out = subprocess.run(
        ["net", "stats", "srv"], capture_output=True, text=True, check=False
    ).stdout
    # Best-effort only; a missing value means "not detectable".
    for line in out.splitlines():
        if "Statistics since" in line:
            from datetime import datetime

            for fmt in ("%m/%d/%Y %I:%M:%S %p", "%d-%m-%Y %H:%M:%S"):
                try:
                    return datetime.strptime(line.split("since", 1)[1].strip(), fmt).timestamp()
                except ValueError:
                    continue
    return 0.0
