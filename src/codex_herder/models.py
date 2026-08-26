from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


REQUIRED_ITERATION_DIRS = [
    Path("code"),
    Path("output/figures"),
    Path("output/videos"),
    Path("output/processed_data"),
    Path("output/stats"),
    Path("logs"),
]


@dataclass(slots=True)
class SessionLink:
    session_id: str
    label: str
    conda_env: str | None = None
    status: str = "configured"
    session_backend: str = "tmux"
    tmux_session_name: str | None = None
    tmux_socket_path: str | None = None
    reused_from_analysis: str | None = None
    bootstrap_sent: bool = False
    codex_id: str | None = None
    codex_thread_name: str | None = None
    identity_status: str = "unverified"
    last_launched_at: str | None = None
    last_verified_at: str | None = None


@dataclass(slots=True)
class ExperimentRef:
    exp_id: str
    user_id: str | None = None


@dataclass(slots=True)
class ExperimentGroup:
    name: str
    experiments: list[ExperimentRef] = field(default_factory=list)


@dataclass(slots=True)
class Iteration:
    iteration_id: str
    analysis_id: str
    project_id: str
    path: Path
    status: str
    codex_session: str
    task_file: str = "task.md"
    created_from_iteration: str | None = None
    summary: str = ""

    @property
    def task_path(self) -> Path:
        return self.path / self.task_file

    @property
    def notes_path(self) -> Path:
        return self.path / "notes.md"

    @property
    def metadata_path(self) -> Path:
        return self.path / "iteration.yaml"


@dataclass(slots=True)
class Analysis:
    analysis_id: str
    project_id: str
    title: str
    path: Path
    status: str = "active"
    iteration_ids: list[str] = field(default_factory=list)
    linked_codex_sessions: list[SessionLink] = field(default_factory=list)
    current_session: str | None = None
    reused_from_analysis: str | None = None
    included_experiment_groups: list[str] = field(default_factory=list)

    @property
    def metadata_path(self) -> Path:
        return self.path / "analysis.yaml"

    @property
    def notes_path(self) -> Path:
        return self.path / "notes.md"


@dataclass(slots=True)
class Project:
    project_id: str
    title: str
    path: Path
    status: str = "active"
    purpose: str = ""
    active_skills: list[str] = field(default_factory=lambda: ["Lab Data Access"])
    main_codex_session: str | None = None
    main_codex_id: str | None = None
    main_codex_thread_name: str | None = None
    main_codex_identity_status: str = "unverified"
    analysis_ids: list[str] = field(default_factory=list)
    experiment_groups: list[ExperimentGroup] = field(default_factory=list)

    @property
    def metadata_path(self) -> Path:
        return self.path / "project.yaml"

    @property
    def notes_path(self) -> Path:
        return self.path / "notes.md"


@dataclass(slots=True)
class AnalysisBundle:
    project: Project
    analysis: Analysis
    iterations: list[Iteration]
