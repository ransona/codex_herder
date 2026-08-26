from __future__ import annotations

import os
import pty
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path

from .models import Analysis, ExperimentGroup, Iteration, Project, SessionLink
from .storage import APP_ROOT, bootstrap_log_path, session_log_path
from .tmux_manager import tmux_socket_path


def codex_command() -> str:
    return os.environ.get("CODEX_HERDER_CODEX_BIN", "codex")


def codex_command_argv() -> list[str]:
    return shlex.split(codex_command())


@dataclass(slots=True)
class SessionLaunchSpec:
    session_id: str
    command: list[str]
    initial_input: str
    log_path: Path
    workdir: Path


@dataclass(slots=True)
class CodexLaunchOption:
    key: str
    label: str
    description: str


@dataclass(slots=True)
class CodexDiagnostics:
    binary_path: str | None
    version: str
    resume_support: bool
    requires_tty: bool
    pty_probe_ok: bool
    pty_probe_summary: str
    launch_command: list[str]


def codex_launch_options() -> list[CodexLaunchOption]:
    return [
        CodexLaunchOption(
            key="ansi",
            label="ANSI screen (recommended)",
            description="PTY-backed terminal with ANSI screen emulation. Lowest latency while staying accurate enough for Codex.",
        ),
        CodexLaunchOption(
            key="raw",
            label="Raw stream",
            description="Shows the exact byte stream decoded as text. Useful when debugging control-sequence issues.",
        ),
    ]


def next_alt_session_id(project: Project, analysis: Analysis) -> str:
    prefix = f"{project.project_id}_{analysis.analysis_id}_alt_"
    highest = 0
    for link in analysis.linked_codex_sessions:
        if link.session_id.startswith(prefix):
            suffix = link.session_id.removeprefix(prefix)
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{prefix}{highest + 1:02d}"


def build_bootstrap_message(
    project: Project,
    analysis: Analysis,
    iteration: Iteration | None,
    task_text: str,
    session_id: str | None = None,
    conda_env: str | None = None,
) -> str:
    iteration_id = iteration.iteration_id if iteration else "none"
    task_body = task_text.strip() or "No task text is available yet."
    project_root = project.path
    included_groups = [group for group in project.experiment_groups if group.name in analysis.included_experiment_groups]
    if included_groups:
        group_lines = []
        for group in included_groups:
            entries = ", ".join(
                f"{exp.exp_id} ({exp.user_id})" if exp.user_id else exp.exp_id
                for exp in group.experiments
            ) or "no experiments added yet"
            group_lines.append(f"- {group.name}: {entries}")
        group_block = "\n".join(group_lines)
    else:
        group_block = "- none selected"
    return (
        "Use the `codex-herder-analysis-context` skill for this session.\n\n"
        f"Project root: {project_root}\n"
        f"Project: {project.project_id} - {project.title}\n"
        f"Analysis: {analysis.analysis_id} - {analysis.title}\n"
        f"Iteration: {iteration_id}\n"
        f"Codex session alias: {session_id or analysis.current_session or 'none'}\n"
        "Available domain skill: Lab Data Access\n\n"
        "Experiment groups included in this analysis:\n"
        f"{group_block}\n\n"
        "Startup behavior:\n"
        "- Do not make suggestions yet.\n"
        "- Do not modify task.md, notes.md, iteration.yaml, code, or outputs unless explicitly instructed.\n"
        "- Do not try to improve or rewrite placeholder files on your own.\n"
        f"- You may read elsewhere as needed, but only write inside this iteration folder: {iteration.path if iteration is not None else analysis.path}\n"
        "- When reporting progress or completed work, do not narrate code modifications line by line and do not dump edit details unless asked.\n"
        "- Default to brief functional summaries of what you changed and why, focusing on behavior and outputs rather than implementation diff detail.\n"
        "- Store figures inside output/figures/ using logical subfolders when related figures belong together.\n"
        "- Keep the figure folder structure understandable and analysis-oriented so related outputs stay grouped.\n"
        "- Store videos inside output/videos/ using the same logical subfolder conventions as figures when related videos belong together.\n"
        "- Save each video as both a .npy file and a matching .mp4 file with the same base name.\n"
        "- Treat the .npy file as the primary GUI preview representation and the .mp4 file as the portable playback/export version.\n"
        "- Default video output format should be mp4.\n"
        "- On startup, check which experiment groups are included in this analysis.\n"
        "- When asked to perform analysis and the target experiment groups are not specified, ask which included groups to analyze.\n"
        "- First inspect the existing iteration state only as needed.\n"
        "- Then wait for instructions for this analysis.\n\n"
        f"Preferred conda environment for this session: {conda_env or 'sci'}\n"
        "- When you need Python/package execution for this analysis, use that conda environment unless explicitly told otherwise.\n\n"
        f"Current task:\n{task_body}\n"
    )


def session_link(
    session_id: str,
    label: str,
    conda_env: str | None = None,
    reused_from_analysis: str | None = None,
    bootstrap_sent: bool = False,
    codex_id: str | None = None,
    codex_thread_name: str | None = None,
    identity_status: str = "unverified",
) -> SessionLink:
    return SessionLink(
        session_id=session_id,
        label=label,
        conda_env=conda_env,
        status="configured",
        session_backend="tmux",
        tmux_session_name=session_id,
        tmux_socket_path=str(tmux_socket_path()),
        reused_from_analysis=reused_from_analysis,
        bootstrap_sent=bootstrap_sent,
        codex_id=codex_id,
        codex_thread_name=codex_thread_name,
        identity_status=identity_status,
    )


def build_new_session_spec(
    project: Project,
    analysis: Analysis,
    iteration: Iteration | None,
    session_id: str,
    task_text: str,
    conda_env: str | None = None,
) -> SessionLaunchSpec:
    log_path = session_log_path(session_id)
    bootstrap = build_bootstrap_message(project, analysis, iteration, task_text, session_id=session_id, conda_env=conda_env)
    bootstrap_log_path(session_id).write_text(bootstrap, encoding="utf-8")
    workdir = iteration.path if iteration is not None else analysis.path
    return SessionLaunchSpec(
        session_id=session_id,
        command=[
            *codex_command_argv(),
            "--no-alt-screen",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(workdir),
            bootstrap,
        ],
        initial_input="",
        log_path=log_path,
        workdir=workdir,
    )


def build_resume_session_spec(link: SessionLink, workdir: Path) -> SessionLaunchSpec:
    target = link.codex_id or link.codex_thread_name
    if not target:
        raise ValueError(f"Session `{link.session_id}` has no verified Codex identity")
    return SessionLaunchSpec(
        session_id=link.session_id,
        command=[
            *codex_command_argv(),
            "--no-alt-screen",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(workdir),
            "resume",
            target,
        ],
        initial_input="",
        log_path=session_log_path(link.session_id),
        workdir=workdir,
    )


@dataclass(slots=True)
class CodexSessionIdentity:
    codex_id: str
    codex_thread_name: str


@dataclass(slots=True)
class BackgroundCodexSessionHandle:
    app_alias: str
    process: subprocess.Popen[bytes]
    master_fd: int
    log_path: Path
    bootstrap_path: Path
    codex_identity: CodexSessionIdentity

    def send_text(self, text: str) -> None:
        payload = text if text.endswith("\n") else text + "\n"
        os.write(self.master_fd, payload.encode("utf-8"))

    def read_available(self, max_bytes: int = 65536) -> str:
        try:
            data = os.read(self.master_fd, max_bytes)
        except BlockingIOError:
            return ""
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace")

    def close(self, terminate: bool = True) -> None:
        if terminate and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass


def session_index_path() -> Path:
    override = os.environ.get("CODEX_HERDER_SESSION_INDEX")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex" / "session_index.jsonl"


def thread_db_path() -> Path:
    override = os.environ.get("CODEX_HERDER_THREAD_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex" / "state_5.sqlite"


def session_index_snapshot() -> set[str]:
    path = session_index_path()
    entries = _read_session_index_entries(path)
    return {entry["id"] for entry in entries if entry.get("id")}


def thread_db_snapshot() -> set[str]:
    return {row["id"] for row in _read_thread_entries(thread_db_path()) if row.get("id")}


def capture_new_codex_session(previous_ids: set[str], timeout_seconds: float = 6.0, poll_seconds: float = 0.2) -> CodexSessionIdentity | None:
    path = session_index_path()
    deadline = time.time() + timeout_seconds
    while True:
        entries = _read_session_index_entries(path)
        new_entries = [entry for entry in entries if entry.get("id") and entry["id"] not in previous_ids]
        if new_entries:
            newest = new_entries[-1]
            return CodexSessionIdentity(
                codex_id=str(newest["id"]),
                codex_thread_name=str(newest.get("thread_name", newest["id"])),
            )
        if time.time() >= deadline:
            break
        time.sleep(poll_seconds)
    return None


def capture_new_codex_session_from_db(
    previous_ids: set[str],
    *,
    cwd: Path | None = None,
    first_user_message: str | None = None,
    min_created_at: int | None = None,
    timeout_seconds: float = 12.0,
    poll_seconds: float = 0.2,
) -> CodexSessionIdentity | None:
    expected_cwd = str(cwd.resolve()) if cwd else None
    deadline = time.time() + timeout_seconds
    while True:
        entries = [entry for entry in _read_thread_entries(thread_db_path()) if entry.get("id") and entry["id"] not in previous_ids]
        if expected_cwd:
            entries = [entry for entry in entries if entry.get("cwd") == expected_cwd]
        if min_created_at is not None:
            entries = [entry for entry in entries if int(entry.get("created_at") or 0) >= min_created_at]
        if first_user_message:
            exact = [entry for entry in entries if entry.get("first_user_message") == first_user_message]
            if exact:
                newest = exact[-1]
                return CodexSessionIdentity(
                    codex_id=str(newest["id"]),
                    codex_thread_name=str(newest.get("title") or newest["id"]),
                )
        if entries:
            newest = entries[-1]
            return CodexSessionIdentity(
                codex_id=str(newest["id"]),
                codex_thread_name=str(newest.get("title") or newest["id"]),
            )
        if time.time() >= deadline:
            break
        time.sleep(poll_seconds)
    return None


def resolve_codex_session_from_db(
    *,
    cwd: Path | None = None,
    first_user_message: str | None = None,
    min_created_at: int | None = None,
) -> CodexSessionIdentity | None:
    expected_cwd = str(cwd.resolve()) if cwd else None
    entries = [entry for entry in _read_thread_entries(thread_db_path()) if entry.get("id")]
    if expected_cwd:
        entries = [entry for entry in entries if entry.get("cwd") == expected_cwd]
    if min_created_at is not None:
        entries = [entry for entry in entries if int(entry.get("created_at") or 0) >= min_created_at]
    if first_user_message:
        exact = [entry for entry in entries if entry.get("first_user_message") == first_user_message]
        if exact:
            newest = exact[-1]
            return CodexSessionIdentity(
                codex_id=str(newest["id"]),
                codex_thread_name=str(newest.get("title") or newest["id"]),
            )
    if entries:
        newest = entries[-1]
        return CodexSessionIdentity(
            codex_id=str(newest["id"]),
            codex_thread_name=str(newest.get("title") or newest["id"]),
        )
    return None


def mark_launch_record(link: SessionLink, *, launched: bool = False, verified: CodexSessionIdentity | None = None, capture_failed: bool = False) -> SessionLink:
    timestamp = datetime.now(timezone.utc).isoformat()
    if launched:
        link.last_launched_at = timestamp
    if verified is not None:
        link.codex_id = verified.codex_id
        link.codex_thread_name = verified.codex_thread_name
        link.identity_status = "verified"
        link.last_verified_at = timestamp
    elif capture_failed:
        link.identity_status = "capture_failed"
    return link

def build_standalone_bootstrap_message(
    *,
    project_root: Path,
    project_id: str,
    project_title: str,
    project_purpose: str,
    analysis_id: str,
    analysis_title: str,
    iteration_id: str | None,
    active_skills: list[str],
    task_text: str,
) -> str:
    iteration_segment = iteration_id or "none"
    return (
        "Use the `codex-herder-analysis-context` skill for this session.\n\n"
        f"Project root: {project_root}\n"
        f"Project: {project_id} - {project_title}\n"
        f"Analysis: {analysis_id} - {analysis_title}\n"
        f"Iteration: {iteration_segment}\n"
        f"Available domain skills: {', '.join(active_skills or ['Lab Data Access'])}\n\n"
        f"Current task:\n{task_text.strip() or 'No task text is available yet.'}\n"
    )


def launch_background_codex_session(
    *,
    app_alias: str,
    project_id: str,
    project_title: str,
    project_purpose: str,
    analysis_id: str,
    analysis_title: str,
    iteration_id: str | None,
    active_skills: list[str],
    task_text: str,
    repo_root: Path,
    rename_after_capture: bool = False,
    capture_timeout_seconds: float = 6.0,
) -> BackgroundCodexSessionHandle:
    bootstrap = build_standalone_bootstrap_message(
        project_root=repo_root / "projects" / project_id if (repo_root / "projects" / project_id).exists() else repo_root,
        project_id=project_id,
        project_title=project_title,
        project_purpose=project_purpose,
        analysis_id=analysis_id,
        analysis_title=analysis_title,
        iteration_id=iteration_id,
        active_skills=active_skills,
        task_text=task_text,
    )
    log_path = session_log_path(app_alias)
    bootstrap_path = bootstrap_log_path(app_alias)
    bootstrap_path.write_text(bootstrap, encoding="utf-8")
    index_snapshot = session_index_snapshot()
    db_snapshot = thread_db_snapshot()
    command = [*codex_command_argv(), "--no-alt-screen", "-C", str(repo_root), bootstrap]
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=repo_root,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave_fd)
    identity = capture_new_codex_session(index_snapshot, timeout_seconds=2.0)
    if identity is None:
        identity = capture_new_codex_session_from_db(
            db_snapshot,
            cwd=repo_root,
            first_user_message=bootstrap,
            timeout_seconds=max(capture_timeout_seconds, 12.0),
        )
    if identity is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        raise RuntimeError("Codex started, but no new session identity was captured from the session index")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + "\n")
        handle.write(f"CAPTURED_ID {identity.codex_id}\n")
        handle.write(f"CAPTURED_THREAD {identity.codex_thread_name}\n")
    session = BackgroundCodexSessionHandle(
        app_alias=app_alias,
        process=process,
        master_fd=master_fd,
        log_path=log_path,
        bootstrap_path=bootstrap_path,
        codex_identity=identity,
    )
    return session


def codex_diagnostics(repo_root: Path) -> CodexDiagnostics:
    binary_path = shutil.which(codex_command_argv()[0]) if codex_command_argv() else None
    version = _run_capture([*codex_command_argv(), "--help"])
    resume_help = _run_capture([*codex_command_argv(), "resume", "--help"])
    non_tty = _run_shell(f"{shlex.join([*codex_command_argv(), '--no-alt-screen', '-C', str(repo_root), 'ping'])} | sed -n '1,20p'")
    pty_ok, pty_summary = _pty_probe(repo_root)
    return CodexDiagnostics(
        binary_path=binary_path,
        version=version.splitlines()[0] if version else "unknown",
        resume_support="Resume a previous interactive session" in resume_help,
        requires_tty="stdin is not a terminal" in non_tty,
        pty_probe_ok=pty_ok,
        pty_probe_summary=pty_summary,
        launch_command=[*codex_command_argv(), "--no-alt-screen", "-C", str(repo_root)],
    )


def diagnostics_lines(diag: CodexDiagnostics) -> list[str]:
    return [
        f"Binary: {diag.binary_path or 'not found'}",
        f"CLI: {diag.version}",
        f"Resume support: {'yes' if diag.resume_support else 'no'}",
        f"Requires TTY: {'yes' if diag.requires_tty else 'no'}",
        f"PTY launch probe: {'ok' if diag.pty_probe_ok else 'failed'}",
        f"PTY probe detail: {diag.pty_probe_summary}",
        f"Launch command: {' '.join(diag.launch_command)}",
    ]


def _run_capture(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=4, check=False)
    except Exception as exc:
        return str(exc)
    return result.stdout or result.stderr


def _run_shell(command: str) -> str:
    try:
        result = subprocess.run(["bash", "-lc", command], capture_output=True, text=True, timeout=6, check=False)
    except Exception as exc:
        return str(exc)
    return result.stdout or result.stderr


def _pty_probe(repo_root: Path) -> tuple[bool, str]:
    import os
    import pty
    import select

    command = [*codex_command_argv(), "--no-alt-screen", "-C", str(repo_root), "Reply with exactly OK and then stop."]
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=repo_root,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        os.close(master_fd)
        os.close(slave_fd)
        return False, f"spawn failed: {exc}"
    os.close(slave_fd)
    chunks: list[bytes] = []
    deadline = time.time() + 3.0
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if master_fd in ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
                lowered = data.lower()
                if b"error" in lowered or b"login" in lowered or b"ok" in lowered:
                    break
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
    output = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not output:
        return False, "no PTY output observed within probe timeout"
    summary = output.replace("\n", " ")[:200]
    return True, summary


def _read_session_index_entries(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    entries: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def _read_thread_entries(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            """
            SELECT id, cwd, title, first_user_message, created_at, updated_at
            FROM threads
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [dict(row) for row in rows]
