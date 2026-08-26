from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from .storage import APP_ROOT, TMUX_ROOT, ensure_app_roots


def tmux_bin() -> str:
    return os.environ.get("CODEX_HERDER_TMUX_BIN", "tmux")


def tmux_socket_path() -> Path:
    override = os.environ.get("CODEX_HERDER_TMUX_SOCKET")
    if override:
        return Path(override).expanduser()
    ensure_app_roots()
    return TMUX_ROOT / "codex-herder.sock"


def tmux_command(*args: str) -> list[str]:
    return [tmux_bin(), "-S", str(tmux_socket_path()), *args]


def tmux_has_session(session_name: str) -> bool:
    result = subprocess.run(
        tmux_command("has-session", "-t", session_name),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def tmux_create_session(session_name: str, command: list[str], cwd: Path) -> None:
    shell_command = shlex.join(command)
    result = subprocess.run(
        tmux_command("new-session", "-d", "-s", session_name, "-c", str(cwd), shell_command),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"failed to create tmux session {session_name}")


def tmux_kill_session(session_name: str) -> None:
    subprocess.run(
        tmux_command("kill-session", "-t", session_name),
        capture_output=True,
        text=True,
        check=False,
    )


def tmux_list_sessions() -> list[str]:
    result = subprocess.run(
        tmux_command("list-sessions", "-F", "#{session_name}"),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def tmux_attach_command(session_name: str) -> list[str]:
    return tmux_command("attach-session", "-t", session_name)


def tmux_send_text(session_name: str, text: str) -> None:
    for raw_line in text.splitlines():
        subprocess.run(tmux_command("send-keys", "-t", session_name, "-l", raw_line), check=False)
        time.sleep(0.06)
        subprocess.run(tmux_command("send-keys", "-t", session_name, "Enter"), check=False)
    if text.endswith("\n\n"):
        time.sleep(0.06)
        subprocess.run(tmux_command("send-keys", "-t", session_name, "Enter"), check=False)


def tmux_capture_pane(session_name: str, lines: int = 400, include_escape: bool = False) -> str:
    command = ["capture-pane", "-p", "-S", f"-{max(lines, 1)}", "-t", session_name]
    if include_escape:
        command.insert(2, "-e")
    result = subprocess.run(
        tmux_command(*command),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def codex_herder_tmux_wrapper_command() -> str:
    return f"tmux -S {shlex.quote(str(tmux_socket_path()))}"


def tmux_attach_spec(session_id: str, session_name: str, log_path: Path):
    from .sessions import SessionLaunchSpec

    return SessionLaunchSpec(
        session_id=session_id,
        command=tmux_attach_command(session_name),
        initial_input="",
        log_path=log_path,
        workdir=APP_ROOT,
    )
