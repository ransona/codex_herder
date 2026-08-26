from __future__ import annotations

from pathlib import Path

from codex_herder.sessions import (
    build_bootstrap_message,
    build_new_session_spec,
    build_resume_session_spec,
    build_standalone_bootstrap_message,
    capture_new_codex_session,
    resolve_codex_session_from_db,
    launch_background_codex_session,
    mark_launch_record,
    next_alt_session_id,
    session_index_snapshot,
    session_link,
)
from codex_herder.storage import (
    APP_ROOT,
    copy_analysis,
    create_analysis,
    create_iteration,
    create_project,
    delete_analysis,
    delete_iteration,
    load_analyses,
    load_projects,
    move_analysis,
    upsert_session_link,
)


def test_create_project_analysis_iteration(tmp_path: Path) -> None:
    workspace = tmp_path / "projects"
    project = create_project("project_900", "Night Run", "Test project", workspace)
    analysis = create_analysis(project, "analysis_001", "Testing")
    iteration = create_iteration(project, analysis, "iter_001", task_text="Inspect data")

    assert (workspace / "project_900" / "project.yaml").exists()
    assert (iteration.path / "code").is_dir()
    assert (iteration.path / "output" / "figures").is_dir()
    assert (iteration.path / "output" / "videos").is_dir()
    assert (iteration.path / "output" / "processed_data").is_dir()
    assert (iteration.path / "output" / "stats").is_dir()
    assert (iteration.path / "logs").is_dir()
    assert iteration.task_path.read_text(encoding="utf-8").strip() == "Inspect data"


def test_session_link_persistence(tmp_path: Path) -> None:
    workspace = tmp_path / "projects"
    project = create_project("project_901", "Persistence", workspace_root_path=workspace)
    analysis = create_analysis(project, "analysis_010", "Linking")
    upsert_session_link(analysis, session_link("project_901_analysis_010_alt_01", "Alt"), make_current=True)

    reloaded = load_analyses(project)[0]
    assert reloaded.current_session == "project_901_analysis_010_alt_01"
    assert any(link.session_id == "project_901_analysis_010_alt_01" for link in reloaded.linked_codex_sessions)


def test_project_session_identity_persistence(tmp_path: Path) -> None:
    workspace = tmp_path / "projects"
    project = create_project("project_901b", "Persistence", workspace_root_path=workspace)
    project.main_codex_id = "thread-123"
    project.main_codex_thread_name = "project supervisor"
    project.main_codex_identity_status = "verified"
    from codex_herder.storage import save_project
    save_project(project)

    reloaded = load_projects(workspace)[0]
    assert reloaded.main_codex_id == "thread-123"
    assert reloaded.main_codex_thread_name == "project supervisor"
    assert reloaded.main_codex_identity_status == "verified"


def test_copy_iteration_from_previous(tmp_path: Path) -> None:
    workspace = tmp_path / "projects"
    project = create_project("project_902", "Forking", workspace_root_path=workspace)
    analysis = create_analysis(project, "analysis_020", "Fork")
    first = create_iteration(project, analysis, "iter_001", task_text="v1")
    (first.path / "code" / "script.py").write_text("print('hi')\n", encoding="utf-8")
    (first.path / "output" / "processed_data" / "cache.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    second = create_iteration(project, analysis, "iter_002", from_iteration=first)

    assert (second.path / "code" / "script.py").exists()
    assert (second.path / "output" / "processed_data" / "cache.csv").exists()


def test_analysis_order_and_copy(tmp_path: Path) -> None:
    workspace = tmp_path / "projects"
    project = create_project("project_order", "Ordering", workspace_root_path=workspace)
    first = create_analysis(project, "A", "A")
    create_iteration(project, first, "iter_001", task_text="a")
    second = create_analysis(project, "B", "B")
    create_iteration(project, second, "iter_001", task_text="b")
    move_analysis(project, "B", -1)
    copied = copy_analysis(project, second, "B copy", "B copy")

    analyses = load_analyses(project)
    assert [analysis.analysis_id for analysis in analyses] == ["B", "B copy", "A"]
    assert copied.current_session == "project_order_B copy_main"
    assert [link.session_id for link in copied.linked_codex_sessions] == ["project_order_B copy_main"]
    copied_iteration = analyses[1]
    assert (copied_iteration.path / "iterations" / "iter_001" / "task.md").exists()


def test_create_analysis_without_session_link(tmp_path: Path) -> None:
    workspace = tmp_path / "projects"
    project = create_project("project_902b", "Forking", workspace_root_path=workspace)
    analysis = create_analysis(project, "analysis_020", "Fork", link_session=False)

    assert analysis.current_session is None
    assert analysis.linked_codex_sessions == []


def test_bootstrap_message_includes_required_context(tmp_path: Path) -> None:
    workspace = tmp_path / "projects"
    project = create_project("project_903", "Bootstrap", "Purpose text", workspace)
    analysis = create_analysis(project, "analysis_001", "Goal")
    iteration = create_iteration(project, analysis, "iter_001", task_text="Task body")
    message = build_bootstrap_message(project, analysis, iteration, "Task body")

    assert "codex-herder-analysis-context" in message
    assert str(project.path) in message
    assert "Lab Data Access" in message
    assert "Iteration: iter_001" in message


def test_next_alt_session_id(tmp_path: Path) -> None:
    workspace = tmp_path / "projects"
    project = create_project("project_904", "Sessions", workspace_root_path=workspace)
    analysis = create_analysis(project, "analysis_001", "Goal")
    upsert_session_link(analysis, session_link("project_904_analysis_001_alt_01", "Alt1"))
    upsert_session_link(analysis, session_link("project_904_analysis_001_alt_02", "Alt2"))

    assert next_alt_session_id(project, analysis) == "project_904_analysis_001_alt_03"


def test_delete_iteration_and_analysis(tmp_path: Path) -> None:
    workspace = tmp_path / "projects"
    project = create_project("project_905", "Deletes", workspace_root_path=workspace)
    analysis = create_analysis(project, "analysis_001", "Goal")
    iteration = create_iteration(project, analysis, "iter_001")

    delete_iteration(analysis, iteration)
    assert not iteration.path.exists()
    assert "iter_001" not in load_analyses(project)[0].iteration_ids

    delete_analysis(project, analysis)
    assert not analysis.path.exists()
    assert "analysis_001" not in project.metadata_path.read_text(encoding="utf-8")


def test_session_specs_use_explicit_terminal_safe_codex_command(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "projects"
    monkeypatch.setenv("CODEX_HERDER_CODEX_BIN", "codex")
    project = create_project("project_906", "Launch", workspace_root_path=workspace)
    analysis = create_analysis(project, "analysis_001", "Goal")
    iteration = create_iteration(project, analysis, "iter_001", task_text="Task")
    link = session_link("project_906_analysis_001_main", "Main", codex_id="abc-123", identity_status="verified")

    new_spec = build_new_session_spec(project, analysis, iteration, "project_906_analysis_001_main", "Task", conda_env="sci")
    resume_spec = build_resume_session_spec(link, iteration.path)

    assert new_spec.command[:5] == [
        "codex",
        "--no-alt-screen",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(iteration.path),
    ]
    assert new_spec.initial_input == ""
    assert "codex-herder-analysis-context" in new_spec.command[-1]
    assert f"Project root: {project.path}" in new_spec.command[-1]
    assert "Project: project_906 - Launch" in new_spec.command[-1]
    assert "Do not make suggestions yet." in new_spec.command[-1]
    assert f"only write inside this iteration folder: {iteration.path}" in new_spec.command[-1]
    assert "Store videos inside output/videos/" in new_spec.command[-1]
    assert "Default video output format should be mp4." in new_spec.command[-1]
    assert "Preferred conda environment for this session: sci" in new_spec.command[-1]
    assert "Then wait for instructions for this analysis." in new_spec.command[-1]
    assert resume_spec.command[:5] == [
        "codex",
        "--no-alt-screen",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(iteration.path),
    ]
    assert resume_spec.command[-2:] == ["resume", "abc-123"]


def test_session_index_capture_and_launch_record(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "session_index.jsonl"
    monkeypatch.setenv("CODEX_HERDER_SESSION_INDEX", str(index_path))
    index_path.write_text('{"id":"old","thread_name":"old-thread","updated_at":"2026-04-04T00:00:00Z"}\n', encoding="utf-8")
    snapshot = session_index_snapshot()
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write('{"id":"new-id","thread_name":"new-thread","updated_at":"2026-04-04T00:00:01Z"}\n')

    identity = capture_new_codex_session(snapshot, timeout_seconds=0)
    link = session_link("project_907_analysis_001_main", "Main")
    mark_launch_record(link, launched=True, verified=identity)

    assert identity is not None
    assert identity.codex_id == "new-id"
    assert link.codex_id == "new-id"
    assert link.codex_thread_name == "new-thread"
    assert link.identity_status == "verified"


def test_resolve_existing_codex_session_from_db(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state_5.sqlite"
    monkeypatch.setenv("CODEX_HERDER_THREAD_DB", str(db_path))
    import sqlite3

    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                created_at INTEGER,
                updated_at INTEGER,
                cwd TEXT,
                title TEXT,
                first_user_message TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO threads (id, created_at, updated_at, cwd, title, first_user_message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("thread-001", 100, 100, str(tmp_path / "iter_001"), "Recovered thread", "bootstrap body"),
        )
        connection.commit()
    finally:
        connection.close()

    identity = resolve_codex_session_from_db(cwd=tmp_path / "iter_001", first_user_message="bootstrap body")
    assert identity is not None
    assert identity.codex_id == "thread-001"
    assert identity.codex_thread_name == "Recovered thread"


def test_standalone_background_launcher_captures_identity_and_bootstrap(tmp_path: Path, monkeypatch) -> None:
    index_path = tmp_path / "session_index.jsonl"
    fake_log = tmp_path / "fake_codex.log"
    monkeypatch.setenv("CODEX_HERDER_SESSION_INDEX", str(index_path))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(fake_log))
    monkeypatch.setenv("FAKE_CODEX_ID", "captured-id")
    monkeypatch.setenv("FAKE_CODEX_THREAD", "captured-thread")
    monkeypatch.setenv("CODEX_HERDER_CODEX_BIN", f"python3 {Path(__file__).resolve().parent / 'fake_codex.py'}")
    session = launch_background_codex_session(
        app_alias="project_908_analysis_001_main",
        project_id="project_908",
        project_title="Standalone",
        project_purpose="Test background launch",
        analysis_id="analysis_001",
        analysis_title="Check launcher",
        iteration_id="iter_001",
        active_skills=["Lab Data Access", "Custom Skill"],
        task_text="Inspect dataset and initialize the analysis.",
        repo_root=tmp_path,
        capture_timeout_seconds=2.0,
    )
    try:
        assert session.codex_identity.codex_id == "captured-id"
        assert session.codex_identity.codex_thread_name == "captured-thread"
        bootstrap = session.bootstrap_path.read_text(encoding="utf-8")
        assert "codex-herder-analysis-context" in bootstrap
        assert str(tmp_path) in bootstrap
        assert "Lab Data Access" in bootstrap
        assert "Custom Skill" in bootstrap
        assert "Inspect dataset and initialize the analysis." in bootstrap
        body = fake_log.read_text(encoding="utf-8")
        assert "ARGV --no-alt-screen -C" in body
    finally:
        session.close()


def test_codex_herder_skill_exists() -> None:
    skill_path = Path("/home/adamranson/.codex/skills/codex-herder-analysis-context/SKILL.md")
    assert skill_path.exists()
    body = skill_path.read_text(encoding="utf-8")
    assert "Required iteration layout" in body
    assert "Lab Data Access" in body
