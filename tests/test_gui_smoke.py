from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

from codex_herder.app import CodexHerderApp
from codex_herder.storage import create_analysis, create_iteration, create_project
from codex_herder.terminal import TerminalPane


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_gui_loads_sample_workspace(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    workspace = Path.cwd() / ".tmp_gui_workspace"
    monkeypatch.setenv("CODEX_HERDER_WORKSPACE_ROOT", str(workspace))
    project = create_project("project_a", "project_a", workspace_root_path=workspace)
    analysis = create_analysis(project, "analysis_a", "analysis_a")
    create_iteration(project, analysis, "iter_001")
    _app()
    window = CodexHerderApp()
    try:
        assert window.project_list.count() == 1
        assert window.project_list.item(0).text() == "project_a"
        assert window.tree.topLevelItemCount() == 1
        assert window.tree.topLevelItem(0).text(0) == "analysis_a"
        assert window.content_tabs.tabText(window.content_tabs.count() - 1) == "CLI"
        labels = [window.content_tabs.tabText(i) for i in range(window.content_tabs.count())]
        assert "Overview" in labels
        assert "Notes" in labels
        assert "Metadata" in labels
    finally:
        window.close()


def test_terminal_launch_and_resume(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    fake_log = tmp_path / "fake_codex.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(fake_log))
    monkeypatch.setenv("CODEX_HERDER_CODEX_BIN", sys.executable + " " + str((Path(__file__).resolve().parent / "fake_codex.py")))
    _app()
    window = CodexHerderApp()
    terminal = None
    try:
        from codex_herder.sessions import SessionLaunchSpec

        spec = SessionLaunchSpec(
            session_id="project_001_analysis_001_main",
            command=[sys.executable, str(Path(__file__).resolve().parent / "fake_codex.py")],
            initial_input="hello\n/exit\n",
            log_path=tmp_path / "terminal.log",
            workdir=Path(__file__).resolve().parents[1],
        )
        terminal = TerminalPane()
        terminal.launch(spec)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.05)
            if fake_log.exists() and "/exit" in fake_log.read_text(encoding="utf-8"):
                break
        assert fake_log.exists()
        body = fake_log.read_text(encoding="utf-8")
        assert "STDIN hello" in body
        assert "STDIN /exit" in body
    finally:
        if terminal is not None:
            terminal.stop()
        window.close()
