from __future__ import annotations

from pathlib import Path

from codex_herder.tmux_manager import (
    codex_herder_tmux_wrapper_command,
    tmux_attach_command,
    tmux_command,
    tmux_socket_path,
)


def test_tmux_commands_use_dedicated_socket(monkeypatch, tmp_path: Path) -> None:
    socket_path = tmp_path / "codex-herder.sock"
    monkeypatch.setenv("CODEX_HERDER_TMUX_SOCKET", str(socket_path))

    assert tmux_command("list-sessions") == ["tmux", "-S", str(socket_path), "list-sessions"]
    assert tmux_attach_command("analysis_001") == ["tmux", "-S", str(socket_path), "attach-session", "-t", "analysis_001"]
    assert codex_herder_tmux_wrapper_command() == f"tmux -S {socket_path}"


def test_tmux_wrapper_script_exists() -> None:
    wrapper = Path("/home/adamranson/code/codex_herder/bin/codex-herder-tmux")
    assert wrapper.exists()
    assert wrapper.stat().st_mode & 0o111
