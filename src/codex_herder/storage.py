from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from .models import Analysis, AnalysisBundle, ExperimentGroup, ExperimentRef, Iteration, Project, REQUIRED_ITERATION_DIRS, SessionLink


APP_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKSPACE_ROOT = Path.home() / "data" / "codex_herder"
STATE_ROOT = APP_ROOT / ".codex_herder"
SESSION_LOG_ROOT = STATE_ROOT / "sessions"
BOOTSTRAP_LOG_ROOT = STATE_ROOT / "bootstrap_logs"
TMUX_ROOT = STATE_ROOT / "tmux"


def workspace_root() -> Path:
    override = os.environ.get("CODEX_HERDER_WORKSPACE_ROOT")
    return Path(override).expanduser() if override else DEFAULT_WORKSPACE_ROOT


def ensure_app_roots() -> None:
    workspace_root().mkdir(parents=True, exist_ok=True)
    SESSION_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    BOOTSTRAP_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    TMUX_ROOT.mkdir(parents=True, exist_ok=True)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _session_links_from_payload(items: list[dict[str, Any]] | list[str] | None) -> list[SessionLink]:
    links: list[SessionLink] = []
    for item in items or []:
        if isinstance(item, str):
            links.append(SessionLink(session_id=item, label=item))
            continue
        links.append(
            SessionLink(
                session_id=str(item.get("session_id", "")),
                label=str(item.get("label", item.get("session_id", ""))),
                conda_env=item.get("conda_env"),
                status=str(item.get("status", "configured")),
                session_backend=str(item.get("session_backend", "tmux")),
                tmux_session_name=item.get("tmux_session_name"),
                tmux_socket_path=item.get("tmux_socket_path"),
                reused_from_analysis=item.get("reused_from_analysis"),
                bootstrap_sent=bool(item.get("bootstrap_sent", False)),
                codex_id=item.get("codex_id"),
                codex_thread_name=item.get("codex_thread_name"),
                identity_status=str(item.get("identity_status", "unverified")),
                last_launched_at=item.get("last_launched_at"),
                last_verified_at=item.get("last_verified_at"),
            )
        )
    return [link for link in links if link.session_id]


def _session_links_payload(links: list[SessionLink]) -> list[dict[str, Any]]:
    return [
        {
            "session_id": link.session_id,
            "label": link.label,
            "conda_env": link.conda_env,
            "status": link.status,
            "session_backend": link.session_backend,
            "tmux_session_name": link.tmux_session_name,
            "tmux_socket_path": link.tmux_socket_path,
            "reused_from_analysis": link.reused_from_analysis,
            "bootstrap_sent": link.bootstrap_sent,
            "codex_id": link.codex_id,
            "codex_thread_name": link.codex_thread_name,
            "identity_status": link.identity_status,
            "last_launched_at": link.last_launched_at,
            "last_verified_at": link.last_verified_at,
        }
        for link in links
    ]


def _experiment_groups_from_payload(items: list[dict[str, Any]] | None) -> list[ExperimentGroup]:
    groups: list[ExperimentGroup] = []
    for item in items or []:
        experiments = [
            ExperimentRef(
                exp_id=str(exp.get("exp_id", "")),
                user_id=exp.get("user_id"),
            )
            for exp in item.get("experiments", [])
            if str(exp.get("exp_id", "")).strip()
        ]
        name = str(item.get("name", "")).strip()
        if name:
            groups.append(ExperimentGroup(name=name, experiments=experiments))
    return groups


def _experiment_groups_payload(groups: list[ExperimentGroup]) -> list[dict[str, Any]]:
    return [
        {
            "name": group.name,
            "experiments": [
                {
                    "exp_id": exp.exp_id,
                    "user_id": exp.user_id,
                }
                for exp in group.experiments
            ],
        }
        for group in groups
    ]


def load_projects(workspace_root_path: Path | None = None) -> list[Project]:
    ensure_app_roots()
    workspace_root_path = workspace_root_path or workspace_root()
    if not workspace_root_path.exists():
        return []
    projects: list[Project] = []
    for project_dir in sorted((p for p in workspace_root_path.iterdir() if p.is_dir()), key=lambda item: item.name.lower()):
        payload = _read_yaml(project_dir / "project.yaml")
        if not payload:
            continue
        projects.append(
            Project(
                project_id=str(payload.get("project_id", project_dir.name)),
                title=str(payload.get("title", project_dir.name)),
                path=project_dir,
                status=str(payload.get("status", "active")),
                purpose=str(payload.get("purpose", "")),
                active_skills=list(payload.get("active_skills", ["Lab Data Access"])),
                main_codex_session=payload.get("main_codex_session"),
                main_codex_id=payload.get("main_codex_id"),
                main_codex_thread_name=payload.get("main_codex_thread_name"),
                main_codex_identity_status=str(payload.get("main_codex_identity_status", "unverified")),
                analysis_ids=list(payload.get("analysis_ids", [])),
                experiment_groups=_experiment_groups_from_payload(payload.get("experiment_groups")),
            )
        )
    return projects


def load_analyses(project: Project) -> list[Analysis]:
    analyses_dir = project.path / "analyses"
    analyses: list[Analysis] = []
    if not analyses_dir.exists():
        return analyses
    analysis_by_id: dict[str, Analysis] = {}
    for analysis_dir in (p for p in analyses_dir.iterdir() if p.is_dir()):
        payload = _read_yaml(analysis_dir / "analysis.yaml")
        if not payload:
            continue
        analysis = Analysis(
            analysis_id=str(payload.get("analysis_id", analysis_dir.name)),
            project_id=str(payload.get("project_id", project.project_id)),
            title=str(payload.get("title", analysis_dir.name)),
            path=analysis_dir,
            status=str(payload.get("status", "active")),
            iteration_ids=list(payload.get("iteration_ids", [])),
            linked_codex_sessions=_session_links_from_payload(payload.get("linked_codex_sessions")),
            current_session=payload.get("current_session"),
            reused_from_analysis=payload.get("reused_from_analysis"),
            included_experiment_groups=list(payload.get("included_experiment_groups", [])),
        )
        analysis_by_id[analysis.analysis_id] = analysis
    for analysis_id in project.analysis_ids:
        analysis = analysis_by_id.pop(analysis_id, None)
        if analysis is not None:
            analyses.append(analysis)
    analyses.extend(sorted(analysis_by_id.values(), key=lambda item: item.analysis_id.lower()))
    return analyses


def load_iterations(project: Project, analysis: Analysis) -> list[Iteration]:
    iterations_dir = analysis.path / "iterations"
    iterations: list[Iteration] = []
    if not iterations_dir.exists():
        return iterations
    for iteration_dir in sorted(p for p in iterations_dir.iterdir() if p.is_dir()):
        payload = _read_yaml(iteration_dir / "iteration.yaml")
        if not payload:
            continue
        iterations.append(
            Iteration(
                iteration_id=str(payload.get("iteration_id", iteration_dir.name)),
                analysis_id=str(payload.get("analysis_id", analysis.analysis_id)),
                project_id=str(payload.get("project_id", project.project_id)),
                path=iteration_dir,
                status=str(payload.get("status", "draft")),
                codex_session=str(payload.get("codex_session", analysis.current_session or "")),
                task_file=str(payload.get("task_file", "task.md")),
                created_from_iteration=payload.get("created_from_iteration"),
                summary=str(payload.get("summary", "")),
            )
        )
    return iterations


def load_analysis_bundle(project: Project, analysis_id: str) -> AnalysisBundle | None:
    for analysis in load_analyses(project):
        if analysis.analysis_id == analysis_id:
            return AnalysisBundle(project=project, analysis=analysis, iterations=load_iterations(project, analysis))
    return None


def save_project(project: Project) -> None:
    _write_yaml(
        project.metadata_path,
        {
            "project_id": project.project_id,
            "title": project.title,
            "status": project.status,
            "purpose": project.purpose,
            "active_skills": project.active_skills,
            "main_codex_session": project.main_codex_session,
            "main_codex_id": project.main_codex_id,
            "main_codex_thread_name": project.main_codex_thread_name,
            "main_codex_identity_status": project.main_codex_identity_status,
            "analysis_ids": project.analysis_ids,
            "experiment_groups": _experiment_groups_payload(project.experiment_groups),
        },
    )
    if not project.notes_path.exists():
        _write_text(project.notes_path, f"# {project.title}\n\n")


def save_analysis(analysis: Analysis) -> None:
    _write_yaml(
        analysis.metadata_path,
        {
            "analysis_id": analysis.analysis_id,
            "project_id": analysis.project_id,
            "title": analysis.title,
            "status": analysis.status,
            "iteration_ids": analysis.iteration_ids,
            "linked_codex_sessions": _session_links_payload(analysis.linked_codex_sessions),
            "current_session": analysis.current_session,
            "reused_from_analysis": analysis.reused_from_analysis,
            "included_experiment_groups": analysis.included_experiment_groups,
        },
    )
    if not analysis.notes_path.exists():
        _write_text(analysis.notes_path, f"# {analysis.title}\n\n")


def save_iteration(iteration: Iteration) -> None:
    ensure_iteration_layout(iteration.path)
    _write_yaml(
        iteration.metadata_path,
        {
            "iteration_id": iteration.iteration_id,
            "analysis_id": iteration.analysis_id,
            "project_id": iteration.project_id,
            "status": iteration.status,
            "codex_session": iteration.codex_session,
            "task_file": iteration.task_file,
            "created_from_iteration": iteration.created_from_iteration,
            "summary": iteration.summary,
        },
    )
    if not iteration.notes_path.exists():
        _write_text(iteration.notes_path, f"# {iteration.iteration_id}\n\n")
    if not iteration.task_path.exists():
        _write_text(iteration.task_path, "Describe the analysis task here.\n")


def ensure_iteration_layout(iteration_path: Path) -> None:
    iteration_path.mkdir(parents=True, exist_ok=True)
    for rel in REQUIRED_ITERATION_DIRS:
        (iteration_path / rel).mkdir(parents=True, exist_ok=True)


def create_project(project_id: str, title: str, purpose: str = "", workspace_root_path: Path | None = None) -> Project:
    workspace_root_path = workspace_root_path or workspace_root()
    project_path = workspace_root_path / project_id
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "analyses").mkdir(exist_ok=True)
    project = Project(project_id=project_id, title=title, path=project_path, purpose=purpose)
    project.main_codex_session = f"{project_id}_supervisor"
    save_project(project)
    return project


def create_analysis(
    project: Project,
    analysis_id: str,
    title: str,
    included_experiment_groups: list[str] | None = None,
    reuse_session_id: str | None = None,
    reused_from_analysis: str | None = None,
    link_session: bool = True,
) -> Analysis:
    analysis_path = project.path / "analyses" / analysis_id
    analysis_path.mkdir(parents=True, exist_ok=True)
    (analysis_path / "iterations").mkdir(exist_ok=True)
    default_session_id = reuse_session_id or f"{project.project_id}_{analysis_id}_main"
    linked_sessions = []
    current_session = None
    if link_session:
        linked_sessions = [
            SessionLink(
                session_id=default_session_id,
                label="Main session",
                status="configured",
                session_backend="tmux",
                tmux_session_name=default_session_id,
                reused_from_analysis=reused_from_analysis,
            )
        ]
        current_session = default_session_id
    analysis = Analysis(
        analysis_id=analysis_id,
        project_id=project.project_id,
        title=title,
        path=analysis_path,
        iteration_ids=[],
        linked_codex_sessions=linked_sessions,
        current_session=current_session,
        reused_from_analysis=reused_from_analysis,
        included_experiment_groups=list(included_experiment_groups or []),
    )
    if analysis_id not in project.analysis_ids:
        project.analysis_ids.append(analysis_id)
        save_project(project)
    save_analysis(analysis)
    return analysis


def create_iteration(
    project: Project,
    analysis: Analysis,
    iteration_id: str,
    from_iteration: Iteration | None = None,
    task_text: str | None = None,
) -> Iteration:
    iteration_path = analysis.path / "iterations" / iteration_id
    ensure_iteration_layout(iteration_path)
    iteration = Iteration(
        iteration_id=iteration_id,
        analysis_id=analysis.analysis_id,
        project_id=project.project_id,
        path=iteration_path,
        status="draft",
        codex_session=analysis.current_session or "",
        created_from_iteration=from_iteration.iteration_id if from_iteration else None,
    )
    if from_iteration:
        _copy_tree(from_iteration.path / "code", iteration.path / "code")
        _copy_tree(from_iteration.path / "output" / "processed_data", iteration.path / "output" / "processed_data")
        _copy_tree(from_iteration.path / "output" / "stats", iteration.path / "output" / "stats")
        if from_iteration.task_path.exists():
            shutil.copy2(from_iteration.task_path, iteration.task_path)
        if from_iteration.notes_path.exists():
            shutil.copy2(from_iteration.notes_path, iteration.notes_path)
    if task_text is not None:
        _write_text(iteration.task_path, task_text.rstrip() + "\n")
    save_iteration(iteration)
    if iteration_id not in analysis.iteration_ids:
        analysis.iteration_ids.append(iteration_id)
        save_analysis(analysis)
    return iteration


def delete_iteration(analysis: Analysis, iteration: Iteration) -> None:
    if iteration.path.exists():
        shutil.rmtree(iteration.path)
    analysis.iteration_ids = [item for item in analysis.iteration_ids if item != iteration.iteration_id]
    save_analysis(analysis)


def delete_analysis(project: Project, analysis: Analysis) -> None:
    if analysis.path.exists():
        shutil.rmtree(analysis.path)
    project.analysis_ids = [item for item in project.analysis_ids if item != analysis.analysis_id]
    save_project(project)


def move_analysis(project: Project, analysis_id: str, direction: int) -> None:
    if analysis_id not in project.analysis_ids or direction == 0:
        return
    index = project.analysis_ids.index(analysis_id)
    target_index = index + direction
    if target_index < 0 or target_index >= len(project.analysis_ids):
        return
    project.analysis_ids[index], project.analysis_ids[target_index] = project.analysis_ids[target_index], project.analysis_ids[index]
    save_project(project)


def upsert_experiment_group(project: Project, group: ExperimentGroup) -> None:
    existing = next((idx for idx, item in enumerate(project.experiment_groups) if item.name == group.name), None)
    if existing is None:
        project.experiment_groups.append(group)
    else:
        project.experiment_groups[existing] = group
    save_project(project)


def delete_experiment_group(project: Project, group_name: str) -> None:
    project.experiment_groups = [group for group in project.experiment_groups if group.name != group_name]
    save_project(project)


def copy_analysis(project: Project, analysis: Analysis, new_analysis_id: str, new_title: str | None = None) -> Analysis:
    new_path = project.path / "analyses" / new_analysis_id
    if new_path.exists():
        raise FileExistsError(f"Analysis `{new_analysis_id}` already exists")
    shutil.copytree(analysis.path, new_path)
    default_session_id = f"{project.project_id}_{new_analysis_id}_main"
    new_analysis = Analysis(
        analysis_id=new_analysis_id,
        project_id=project.project_id,
        title=new_title or new_analysis_id,
        path=new_path,
        status=analysis.status,
        iteration_ids=[],
        linked_codex_sessions=[
            SessionLink(
                session_id=default_session_id,
                label="Main session",
                status="configured",
                session_backend="tmux",
                tmux_session_name=default_session_id,
            )
        ],
        current_session=default_session_id,
        reused_from_analysis=analysis.analysis_id,
    )
    iterations: list[str] = []
    iterations_dir = new_path / "iterations"
    if iterations_dir.exists():
        for iteration_dir in sorted((p for p in iterations_dir.iterdir() if p.is_dir()), key=lambda item: item.name.lower()):
            payload = _read_yaml(iteration_dir / "iteration.yaml")
            payload["iteration_id"] = str(payload.get("iteration_id", iteration_dir.name))
            payload["analysis_id"] = new_analysis_id
            payload["project_id"] = project.project_id
            payload["codex_session"] = default_session_id
            _write_yaml(iteration_dir / "iteration.yaml", payload)
            iterations.append(str(payload["iteration_id"]))
    new_analysis.iteration_ids = iterations
    save_analysis(new_analysis)
    if analysis.analysis_id in project.analysis_ids:
        index = project.analysis_ids.index(analysis.analysis_id) + 1
        project.analysis_ids.insert(index, new_analysis_id)
    else:
        project.analysis_ids.append(new_analysis_id)
    save_project(project)
    return new_analysis


def _copy_tree(source: Path, target: Path) -> None:
    if not source.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)


def upsert_session_link(analysis: Analysis, session: SessionLink, make_current: bool = False) -> None:
    for idx, existing in enumerate(analysis.linked_codex_sessions):
        if existing.session_id == session.session_id:
            analysis.linked_codex_sessions[idx] = session
            break
    else:
        analysis.linked_codex_sessions.append(session)
    if make_current:
        analysis.current_session = session.session_id
    save_analysis(analysis)


def set_notes(path: Path, text: str) -> None:
    _write_text(path, text)


def read_notes(path: Path) -> str:
    return _read_text(path)


def metadata_text(path: Path) -> str:
    return _read_text(path)


def write_metadata_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml.safe_load(text or "{}")
    _write_text(path, text if text.endswith("\n") else text + "\n")


def bootstrap_log_path(session_id: str) -> Path:
    return BOOTSTRAP_LOG_ROOT / f"{session_id}.md"


def session_log_path(session_id: str) -> Path:
    return SESSION_LOG_ROOT / f"{session_id}.log"


def list_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted((p for p in path.iterdir() if p.is_file()), key=lambda item: item.name.lower())


def list_tree_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted((p for p in path.rglob("*") if p.is_file()), key=lambda item: str(item.relative_to(path)).lower())
