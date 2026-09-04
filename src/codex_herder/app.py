from __future__ import annotations

import os
import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
import subprocess
import pwd
import numpy as np
try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QPainter, QPixmap, QDesktopServices, QImage
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QComboBox,
    QPlainTextEdit,
    QProgressDialog,
    QScrollArea,
    QSlider,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
try:
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    QT_VIDEO_AVAILABLE = True
except Exception:  # pragma: no cover
    QUrl = None  # type: ignore[assignment]
    QAudioOutput = None  # type: ignore[assignment]
    QMediaPlayer = None  # type: ignore[assignment]
    QVideoWidget = None  # type: ignore[assignment]
    QT_VIDEO_AVAILABLE = False

from .models import Analysis, ExperimentGroup, ExperimentRef, Iteration, Project, SessionLink
from .sessions import (
    build_new_session_spec,
    build_resume_session_spec,
    capture_new_codex_session,
    capture_new_codex_session_from_db,
    mark_launch_record,
    next_alt_session_id,
    resolve_codex_session_from_db,
    session_index_snapshot,
    session_link,
    thread_db_snapshot,
)
from .storage import (
    APP_ROOT,
    workspace_root,
    create_analysis,
    copy_analysis,
    create_iteration,
    create_project,
    delete_experiment_group,
    delete_analysis,
    delete_iteration,
    ensure_app_roots,
    list_files,
    list_tree_files,
    load_analyses,
    load_iterations,
    load_projects,
    metadata_text,
    move_experiment_group,
    move_analysis,
    read_notes,
    bootstrap_log_path,
    save_iteration,
    save_project,
    set_notes,
    upsert_experiment_group,
    upsert_session_link,
    write_metadata_text,
)
from .tmux_manager import (
    codex_herder_tmux_wrapper_command,
    tmux_create_session,
    tmux_has_session,
    tmux_kill_session,
    tmux_list_sessions,
    tmux_send_text,
)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif"}
SVG_EXTENSIONS = {".svg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".npy"}
PREVIEW_EXTENSIONS = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".py"}


@dataclass(slots=True)
class Selection:
    kind: str
    project: Project | None = None
    analysis: Analysis | None = None
    iteration: Iteration | None = None
    session: SessionLink | None = None
    experiment_group: ExperimentGroup | None = None
    experiment_entry: ExperimentRef | None = None


def _expanded_tree_paths(tree: QTreeWidget, item_targets: dict[int, Path], root: Path) -> set[str]:
    expanded: set[str] = set()

    def visit(item: QTreeWidgetItem) -> None:
        target = item_targets.get(id(item))
        if item.isExpanded() and target is not None and target.is_dir():
            expanded.add(str(target.relative_to(root)))
        for child_index in range(item.childCount()):
            visit(item.child(child_index))

    for index in range(tree.topLevelItemCount()):
        visit(tree.topLevelItem(index))
    return expanded


class FigurePreviewLabel(QLabel):
    def __init__(self, empty_text: str) -> None:
        super().__init__(empty_text)
        self._figure_path: Path | None = None
        self._open_callback: callable | None = None
        self._source_pixmap = QPixmap()
        self._zoom = 1.0
        self._pan = QPoint(0, 0)
        self._drag_start: QPoint | None = None
        self._last_escape_at = 0.0
        self.setFocusPolicy(Qt.StrongFocus)

    def set_open_callback(self, callback: callable) -> None:
        self._open_callback = callback

    def set_figure_path(self, path: Path | None) -> None:
        if path != self._figure_path:
            self._zoom = 1.0
            self._pan = QPoint(0, 0)
        self._figure_path = path

    def set_source_pixmap(self, pixmap: QPixmap) -> None:
        self._source_pixmap = pixmap
        self.setText("")
        self.update()

    def zoom_in(self) -> None:
        if not self._source_pixmap.isNull():
            self.zoom_in_at(QPoint(self.width() // 2, self.height() // 2))

    def zoom_in_at(self, point: QPoint) -> None:
        if self._source_pixmap.isNull():
            return
        old_scaled, old_center = self._scaled_geometry(self._zoom)
        old_position = old_center + self._pan
        if old_scaled.width() > 0 and old_scaled.height() > 0:
            relative_x = (point.x() - old_position.x()) / old_scaled.width()
            relative_y = (point.y() - old_position.y()) / old_scaled.height()
        else:
            relative_x = relative_y = 0.5
        self._zoom *= 1.2
        new_scaled, new_center = self._scaled_geometry(self._zoom)
        self._pan = QPoint(
            int(point.x() - new_center.x() - relative_x * new_scaled.width()),
            int(point.y() - new_center.y() - relative_y * new_scaled.height()),
        )
        self.update()

    def _scaled_geometry(self, zoom: float) -> tuple[QSize, QPoint]:
        base_size = self._source_pixmap.size()
        base_size.scale(max(1, self.width() - 20), max(1, self.height() - 20), Qt.KeepAspectRatio)
        scaled_size = QSize(max(1, int(base_size.width() * zoom)), max(1, int(base_size.height() * zoom)))
        center = QPoint((self.width() - scaled_size.width()) // 2, (self.height() - scaled_size.height()) // 2)
        return scaled_size, center

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._pan = QPoint(0, 0)
        self.update()

    def clear_source_pixmap(self, text: str = "") -> None:
        self._source_pixmap = QPixmap()
        self._zoom = 1.0
        self._pan = QPoint(0, 0)
        self.setPixmap(QPixmap())
        self.setText(text)
        self.update()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        if self._source_pixmap.isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        target_size, centered_position = self._scaled_geometry(self._zoom)
        scaled = self._source_pixmap.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        position = centered_position + self._pan
        painter.drawPixmap(position, scaled)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and not self._source_pixmap.isNull():
            self.setFocus(Qt.MouseFocusReason)
            self._drag_start = event.position().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._drag_start is not None:
            current = event.position().toPoint()
            self._pan += current - self._drag_start
            self._drag_start = current
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key_Escape and not self._source_pixmap.isNull():
            now = time.monotonic()
            if now - self._last_escape_at < 0.45:
                self.reset_view()
            else:
                self._zoom = max(1.0, self._zoom / 1.2)
                if self._zoom <= 1.0:
                    self._pan = QPoint(0, 0)
                else:
                    self._pan = QPoint(int(self._pan.x() / 1.2), int(self._pan.y() / 1.2))
                self.update()
            self._last_escape_at = now
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.LeftButton and not self._source_pixmap.isNull():
            self.zoom_in_at(event.position().toPoint())
            event.accept()
            return
        if event.button() == Qt.RightButton and not self._source_pixmap.isNull():
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class FigureWindow(QMainWindow):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._pixmap = self._load_pixmap(path)
        self.setWindowTitle(path.name)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._label)
        self.setCentralWidget(scroll)
        self._render()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._render()

    def _render(self) -> None:
        if self._pixmap.isNull():
            self._label.setText(self._path.name)
            return
        target = self.centralWidget().size() if self.centralWidget() is not None else self.size()
        scaled = self._pixmap.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._label.setPixmap(scaled)

    @staticmethod
    def _load_pixmap(path: Path) -> QPixmap:
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            return QPixmap(str(path))
        if path.suffix.lower() in SVG_EXTENSIONS:
            pixmap = QPixmap(2400, 1600)
            pixmap.fill(Qt.white)
            painter = QPainter(pixmap)
            renderer = QSvgRenderer(str(path))
            renderer.render(painter)
            painter.end()
            return pixmap
        return QPixmap()


class VideoWindow(QMainWindow):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self.setWindowTitle(path.name)
        self._temp_dir: Path | None = None
        self._frames: list[Path] = []
        self._frame_index = 0
        self._playing = False
        self._frame_interval_ms = 100

        container = QWidget()
        layout = QVBoxLayout(container)
        self._label = QLabel("Loading video...")
        self._label.setAlignment(Qt.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._label)
        controls = QHBoxLayout()
        self._play_button = QPushButton("Play")
        self._prev_button = QPushButton("Prev")
        self._next_button = QPushButton("Next")
        self._slider = QSlider(Qt.Horizontal)
        controls.addWidget(self._play_button)
        controls.addWidget(self._prev_button)
        controls.addWidget(self._next_button)
        controls.addWidget(self._slider, 1)
        layout.addWidget(scroll, 1)
        layout.addLayout(controls)
        self.setCentralWidget(container)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance_frame)
        self._play_button.clicked.connect(self._toggle_playback)
        self._prev_button.clicked.connect(lambda: self._set_frame(max(0, self._frame_index - 1)))
        self._next_button.clicked.connect(lambda: self._set_frame(min(len(self._frames) - 1, self._frame_index + 1)))
        self._slider.valueChanged.connect(self._slider_changed)

        self._load_video_frames()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._timer.stop()
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._render_current_frame()

    def _load_video_frames(self) -> None:
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="codex_herder_video_"))
            self._extract_video_frames(self._path, temp_dir)
            metadata_path = temp_dir / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
            self._frame_interval_ms = int(metadata.get("frame_interval_ms", 100))
            self._frames = sorted(temp_dir.glob("frame_*.jpg"))
            self._temp_dir = temp_dir
        except Exception as exc:
            self._label.setText(f"Unable to load video in GUI.\n\n{self._path}\n\n{exc}")
            return
        if not self._frames:
            self._label.setText(f"No preview frames could be extracted.\n\n{self._path}")
            return
        self._slider.setRange(0, len(self._frames) - 1)
        self._set_frame(0)

    @staticmethod
    def _extract_video_frames(path: Path, out_dir: Path) -> None:
        if cv2 is not None:
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                raise RuntimeError(f"Could not open video: {path}")
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            if fps <= 0:
                fps = 10.0
            target_fps = min(max(fps, 1.0), 12.0)
            step = max(1, int(round(fps / target_fps)))
            index = 0
            saved = 0
            out_dir.mkdir(parents=True, exist_ok=True)
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if index % step == 0:
                    out_path = out_dir / f"frame_{saved:06d}.jpg"
                    cv2.imwrite(str(out_path), frame)
                    saved += 1
                index += 1
            cap.release()
            metadata = {"frame_interval_ms": int(round(1000.0 / target_fps)), "saved_frames": saved}
            (out_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            return
        script = """
import cv2, json, math, pathlib, sys
video_path = pathlib.Path(sys.argv[1])
out_dir = pathlib.Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)
cap = cv2.VideoCapture(str(video_path))
if not cap.isOpened():
    raise RuntimeError(f"Could not open video: {video_path}")
fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
if fps <= 0:
    fps = 10.0
target_fps = min(max(fps, 1.0), 12.0)
step = max(1, int(round(fps / target_fps)))
index = 0
saved = 0
while True:
    ok, frame = cap.read()
    if not ok:
        break
    if index % step == 0:
        out_path = out_dir / f"frame_{saved:06d}.jpg"
        cv2.imwrite(str(out_path), frame)
        saved += 1
    index += 1
cap.release()
metadata = {"frame_interval_ms": int(round(1000.0 / target_fps)), "saved_frames": saved}
(out_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
"""
        result = subprocess.run(
            ["python3", "-c", script, str(path), str(out_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "frame extraction failed")

    def _toggle_playback(self) -> None:
        if not self._frames:
            return
        self._playing = not self._playing
        self._play_button.setText("Pause" if self._playing else "Play")
        if self._playing:
            self._timer.start(self._frame_interval_ms)
        else:
            self._timer.stop()

    def _advance_frame(self) -> None:
        if not self._frames:
            return
        next_index = 0 if self._frame_index >= len(self._frames) - 1 else self._frame_index + 1
        self._set_frame(next_index)

    def _slider_changed(self, value: int) -> None:
        if self._frames and value != self._frame_index:
            self._set_frame(value)

    def _set_frame(self, index: int) -> None:
        if not self._frames:
            return
        self._frame_index = max(0, min(index, len(self._frames) - 1))
        self._slider.blockSignals(True)
        self._slider.setValue(self._frame_index)
        self._slider.blockSignals(False)
        self._render_current_frame()

    def _render_current_frame(self) -> None:
        if not self._frames:
            return
        pixmap = QPixmap(str(self._frames[self._frame_index]))
        if pixmap.isNull():
            self._label.setText(self._frames[self._frame_index].name)
            return
        target = self.centralWidget().size() if self.centralWidget() is not None else self.size()
        scaled = pixmap.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._label.setPixmap(scaled)


class TmuxSessionsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("App Tmux Sessions")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        self.listing = QListWidget()
        layout.addWidget(self.listing)
        button_row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.kill_button = QPushButton("Kill Selected")
        self.kill_all_button = QPushButton("Kill All")
        button_row.addWidget(self.refresh_button)
        button_row.addWidget(self.kill_button)
        button_row.addWidget(self.kill_all_button)
        layout.addLayout(button_row)
        self.refresh_button.clicked.connect(self.reload)
        self.kill_button.clicked.connect(self.kill_selected)
        self.kill_all_button.clicked.connect(self.kill_all)
        self.reload()

    def reload(self) -> None:
        self.listing.clear()
        for name in tmux_list_sessions():
            self.listing.addItem(name)

    def kill_selected(self) -> None:
        item = self.listing.currentItem()
        if item is None:
            return
        tmux_kill_session(item.text())
        self.reload()

    def kill_all(self) -> None:
        for name in tmux_list_sessions():
            tmux_kill_session(name)
        self.reload()


class ExperimentGroupDialog(QDialog):
    def __init__(self, user_ids: list[str], name: str, group: ExperimentGroup | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Experiment Group: {name}")
        self.resize(640, 520)
        self.group_name = name
        layout = QVBoxLayout(self)
        self.exp_ids_edit = QPlainTextEdit()
        self.exp_ids_edit.setPlaceholderText("Space separated expIDs")
        self.user_combo = QComboBox()
        self.user_combo.addItems(user_ids or [""])
        self.add_button = QPushButton("Add")
        self.entries_list = QListWidget()
        self.entries_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.remove_button = QPushButton("Remove Selected")
        self.move_up_button = QPushButton("↑ Move Up")
        self.move_down_button = QPushButton("↓ Move Down")
        self.sort_button = QPushButton("Sort")
        self.save_button = QPushButton("Save Group")
        layout.addWidget(QLabel("expIDs"))
        layout.addWidget(self.exp_ids_edit)
        layout.addWidget(QLabel("userID"))
        layout.addWidget(self.user_combo)
        layout.addWidget(self.add_button)
        layout.addWidget(QLabel("Current group entries"))
        layout.addWidget(self.entries_list, 1)
        entry_controls = QHBoxLayout()
        entry_controls.addWidget(self.move_up_button)
        entry_controls.addWidget(self.move_down_button)
        entry_controls.addWidget(self.remove_button)
        layout.addLayout(entry_controls)
        save_controls = QHBoxLayout()
        save_controls.addWidget(self.sort_button)
        save_controls.addWidget(self.save_button)
        layout.addLayout(save_controls)
        self._entries: list[ExperimentRef] = list(group.experiments) if group is not None else []
        self.add_button.clicked.connect(self._add_entries)
        self.remove_button.clicked.connect(self._remove_selected)
        self.move_up_button.clicked.connect(lambda: self._move_selected(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected(1))
        self.sort_button.clicked.connect(self._sort_entries)
        self.entries_list.currentRowChanged.connect(lambda _row: self._refresh_entry_controls())
        self.entries_list.itemSelectionChanged.connect(self._refresh_entry_controls)
        self.save_button.clicked.connect(self.accept)
        self._refresh_entries()

    def _add_entries(self) -> None:
        raw = self.exp_ids_edit.toPlainText().split()
        user_id = self.user_combo.currentText().strip() or None
        for exp_id in raw:
            exp_id = exp_id.strip()
            if not exp_id:
                continue
            entry = ExperimentRef(exp_id=exp_id, user_id=user_id)
            if not any(item.exp_id == entry.exp_id and item.user_id == entry.user_id for item in self._entries):
                self._entries.append(entry)
        self.exp_ids_edit.clear()
        self._refresh_entries()

    def _remove_selected(self) -> None:
        rows = sorted((self.entries_list.row(item) for item in self.entries_list.selectedItems()), reverse=True)
        if not rows:
            return
        for row in rows:
            if 0 <= row < len(self._entries):
                del self._entries[row]
        self._refresh_entries()

    def _move_selected(self, direction: int) -> None:
        row = self.entries_list.currentRow()
        target = row + direction
        if row < 0 or target < 0 or target >= len(self._entries):
            return
        self._entries[row], self._entries[target] = self._entries[target], self._entries[row]
        self._refresh_entries()
        self.entries_list.setCurrentRow(target)

    def _sort_entries(self) -> None:
        selected = self.entries_list.currentItem()
        selected_entry = self._entries[self.entries_list.currentRow()] if selected is not None else None
        self._entries.sort(key=lambda entry: entry.exp_id.casefold())
        self._refresh_entries()
        if selected_entry is not None:
            try:
                self.entries_list.setCurrentRow(self._entries.index(selected_entry))
            except ValueError:
                pass

    def _refresh_entries(self) -> None:
        self.entries_list.clear()
        for entry in self._entries:
            self.entries_list.addItem(f"{entry.exp_id} ({entry.user_id})" if entry.user_id else entry.exp_id)
        self._refresh_entry_controls()

    def _refresh_entry_controls(self) -> None:
        row = self.entries_list.currentRow()
        self.move_up_button.setEnabled(row > 0)
        self.move_down_button.setEnabled(0 <= row < len(self._entries) - 1)
        self.remove_button.setEnabled(bool(self.entries_list.selectedItems()))

    def result_group(self) -> ExperimentGroup:
        return ExperimentGroup(name=self.group_name, experiments=list(self._entries))


class ExperimentGroupSelectionDialog(QDialog):
    def __init__(self, groups: list[ExperimentGroup], selected: list[str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Experiment Groups")
        self.resize(480, 420)
        layout = QVBoxLayout(self)
        self.listing = QListWidget()
        self.listing.setSelectionMode(QListWidget.MultiSelection)
        selected_set = set(selected or [])
        for group in groups:
            item = QListWidgetItem(group.name)
            self.listing.addItem(item)
            item.setSelected(group.name in selected_set)
        self.ok_button = QPushButton("Use Selected Groups")
        layout.addWidget(self.listing)
        layout.addWidget(self.ok_button)
        self.ok_button.clicked.connect(self.accept)

    def selected_group_names(self) -> list[str]:
        return [item.text() for item in self.listing.selectedItems()]


class CodexHerderApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_app_roots()
        self.setWindowTitle("Codex Herder")
        self.resize(1720, 980)
        self.projects: list[Project] = []
        self.selected_project_id: str | None = None
        self.current_selection = Selection(kind="none")
        self._tree_lookup: dict[int, Selection] = {}
        self._project_lookup: dict[int, Project] = {}
        self._experiment_group_lookup: dict[int, ExperimentGroup] = {}
        self._figure_paths: list[Path] = []
        self._figure_windows: list[FigureWindow] = []
        self._video_windows: list[VideoWindow] = []
        self._tmux_dialog: TmuxSessionsDialog | None = None
        self._trust_auto_sent_at: dict[str, float] = {}
        self._pending_session_verifications: dict[str, dict[str, object]] = {}

        self._build_ui()
        self._asset_refresh_timer = QTimer(self)
        self._asset_refresh_timer.setInterval(1000)
        self._asset_refresh_timer.timeout.connect(self._refresh_current_asset_tab)
        self._verification_timer = QTimer(self)
        self._verification_timer.setInterval(1000)
        self._verification_timer.timeout.connect(self._poll_pending_session_verifications)
        self.reload_workspace()

    def _build_ui(self) -> None:
        main_splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(main_splitter)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)

        project_row = QHBoxLayout()
        self.new_project_button = QPushButton("New Project")
        project_row.addWidget(self.new_project_button)
        left_layout.addLayout(project_row)

        left_layout.addWidget(QLabel("Projects"))
        self.project_list = QListWidget()
        left_layout.addWidget(self.project_list, 1)

        analysis_row = QGridLayout()
        self.new_analysis_button = QPushButton("New Analysis")
        self.copy_analysis_button = QPushButton("Copy Analysis")
        self.move_up_button = QPushButton("↑")
        self.move_down_button = QPushButton("↓")
        self.delete_button = QPushButton("Delete")
        analysis_row.addWidget(self.new_analysis_button, 0, 0)
        analysis_row.addWidget(self.copy_analysis_button, 0, 1)
        analysis_row.addWidget(self.move_up_button, 1, 0)
        analysis_row.addWidget(self.move_down_button, 1, 1)
        analysis_row.addWidget(self.delete_button, 1, 2)
        analysis_row.setColumnStretch(0, 1)
        analysis_row.setColumnStretch(1, 1)
        analysis_row.setColumnStretch(2, 1)
        left_layout.addLayout(analysis_row)

        left_layout.addWidget(QLabel("Analyses"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Analysis / Iteration"])
        left_layout.addWidget(self.tree, 3)

        left_layout.addWidget(QLabel("Experiment Groupings"))
        self.experiment_group_list = QListWidget()
        left_layout.addWidget(self.experiment_group_list, 1)
        exp_group_row = QGridLayout()
        self.new_exp_group_button = QPushButton("New Exp Group")
        self.edit_exp_group_button = QPushButton("Edit Exp Group")
        self.rename_exp_group_button = QPushButton("Rename Exp Group")
        self.delete_exp_group_button = QPushButton("Delete Exp Group")
        self.move_exp_group_up_button = QPushButton("↑")
        self.move_exp_group_down_button = QPushButton("↓")
        exp_group_row.addWidget(self.new_exp_group_button, 0, 0)
        exp_group_row.addWidget(self.edit_exp_group_button, 0, 1)
        exp_group_row.addWidget(self.rename_exp_group_button, 1, 0)
        exp_group_row.addWidget(self.delete_exp_group_button, 1, 1)
        exp_group_row.addWidget(self.move_exp_group_up_button, 2, 0)
        exp_group_row.addWidget(self.move_exp_group_down_button, 2, 1)
        exp_group_row.setColumnStretch(0, 1)
        exp_group_row.setColumnStretch(1, 1)
        left_layout.addLayout(exp_group_row)
        main_splitter.addWidget(left_panel)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(8, 8, 8, 8)
        self.selection_label = QLabel("No selection")
        self.selection_label.setWordWrap(True)
        center_layout.addWidget(self.selection_label)

        action_row = QHBoxLayout()
        self.launch_button = QPushButton("Create New Session")
        self.resume_button = QPushButton("Resume Current Session")
        self.link_button = QPushButton("Link Existing")
        self.copy_session_button = QPushButton("Copy Codex Session")
        self.codex_iteration_button = QPushButton("Ask Codex To Create Iteration")
        action_row.addWidget(self.launch_button)
        action_row.addWidget(self.resume_button)
        action_row.addWidget(self.link_button)
        action_row.addWidget(self.copy_session_button)
        action_row.addWidget(self.codex_iteration_button)
        center_layout.addLayout(action_row)

        self.content_tabs = QTabWidget()
        center_layout.addWidget(self.content_tabs)
        main_splitter.addWidget(center)

        self.cli_tab = QWidget()
        cli_layout = QVBoxLayout(self.cli_tab)
        cli_layout.setContentsMargins(8, 8, 8, 8)
        self.cli_status = QPlainTextEdit()
        self.cli_status.setReadOnly(True)
        self.cli_attach_button = QPushButton("Copy Attach Command")
        cli_layout.addWidget(self.cli_status, 1)
        cli_layout.addWidget(self.cli_attach_button)
        main_splitter.setStretchFactor(0, 2)
        main_splitter.setStretchFactor(1, 7)

        self.project_list.itemClicked.connect(self._project_item_selected)
        self.tree.itemClicked.connect(self._tree_item_selected)
        self.experiment_group_list.itemClicked.connect(self._experiment_group_item_selected)
        self.experiment_group_list.itemDoubleClicked.connect(self._edit_selected_experiment_group)
        self.new_project_button.clicked.connect(self._create_project_dialog)
        self.new_analysis_button.clicked.connect(self._create_analysis_dialog)
        self.new_exp_group_button.clicked.connect(self._create_experiment_group_dialog)
        self.edit_exp_group_button.clicked.connect(self._edit_selected_experiment_group)
        self.rename_exp_group_button.clicked.connect(self._rename_selected_experiment_group)
        self.delete_exp_group_button.clicked.connect(self._delete_selected_experiment_group)
        self.move_exp_group_up_button.clicked.connect(lambda: self._move_selected_experiment_group(-1))
        self.move_exp_group_down_button.clicked.connect(lambda: self._move_selected_experiment_group(1))
        self.copy_analysis_button.clicked.connect(self._copy_selected_analysis)
        self.move_up_button.clicked.connect(lambda: self._move_selected_analysis(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected_analysis(1))
        self.delete_button.clicked.connect(self._delete_selected)
        self.launch_button.clicked.connect(self._launch_new_session)
        self.resume_button.clicked.connect(self._resume_selected_session)
        self.link_button.clicked.connect(self._link_existing_session)
        self.copy_session_button.clicked.connect(self._copy_current_session_command)
        self.codex_iteration_button.clicked.connect(self._ask_codex_to_create_iteration)
        self.cli_attach_button.clicked.connect(self._copy_current_session_command)
        self.content_tabs.currentChanged.connect(self._content_tab_changed)

        refresh_action = QAction("Reload", self)
        refresh_action.triggered.connect(self.reload_workspace)
        self.menuBar().addAction(refresh_action)
        self.launch_button.setText("Ensure Session Running")
        self.resume_button.hide()
        self.link_button.hide()

    def _build_dual_list_preview(self, empty_text: str) -> tuple[QListWidget, FigurePreviewLabel, QWidget]:
        page = QWidget()
        layout = QHBoxLayout(page)
        listing = QListWidget()
        preview = FigurePreviewLabel(empty_text)
        preview.setAlignment(Qt.AlignCenter)
        layout.addWidget(listing, 1)
        layout.addWidget(preview, 3)
        return listing, preview, page

    def _build_text_preview(self, empty_text: str) -> tuple[QListWidget, QPlainTextEdit, QWidget]:
        page = QWidget()
        layout = QHBoxLayout(page)
        listing = QListWidget()
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setPlaceholderText(empty_text)
        layout.addWidget(listing, 1)
        layout.addWidget(preview, 3)
        return listing, preview, page

    def reload_workspace(self) -> None:
        selection_key = self._selection_key(self.current_selection)
        expanded_analysis_ids = {
            self._tree_lookup[id(self.tree.topLevelItem(i))].analysis.analysis_id
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i).isExpanded()
            and id(self.tree.topLevelItem(i)) in self._tree_lookup
            and self._tree_lookup[id(self.tree.topLevelItem(i))].analysis is not None
        }
        self.projects = load_projects()
        self.project_list.clear()
        self._project_lookup.clear()
        self.tree.clear()
        self._tree_lookup.clear()
        self.experiment_group_list.clear()
        self._experiment_group_lookup.clear()
        first_project_item: QListWidgetItem | None = None
        selected_project_item: QListWidgetItem | None = None
        selected_item: QTreeWidgetItem | None = None
        selected_group_item: QListWidgetItem | None = None
        for project in self.projects:
            project_item = QListWidgetItem(project.title)
            self.project_list.addItem(project_item)
            self._project_lookup[id(project_item)] = project
            if first_project_item is None:
                first_project_item = project_item
            if self.selected_project_id == project.project_id or (self.selected_project_id is None and selection_key[1] == project.project_id):
                selected_project_item = project_item
        current_project_item = selected_project_item or first_project_item
        current_project = self._project_lookup.get(id(current_project_item)) if current_project_item is not None else None
        if current_project_item is not None:
            self.project_list.setCurrentItem(current_project_item)
        self.selected_project_id = current_project.project_id if current_project is not None else None
        if current_project is not None:
            for analysis in load_analyses(current_project):
                analysis_item = QTreeWidgetItem([analysis.title])
                self.tree.addTopLevelItem(analysis_item)
                analysis_selection = Selection(kind="analysis", project=current_project, analysis=analysis)
                self._tree_lookup[id(analysis_item)] = analysis_selection
                if selection_key == self._selection_key(analysis_selection):
                    selected_item = analysis_item
                if analysis.analysis_id in expanded_analysis_ids:
                    analysis_item.setExpanded(True)
                for iteration in load_iterations(current_project, analysis):
                    iteration_item = QTreeWidgetItem([iteration.iteration_id])
                    analysis_item.addChild(iteration_item)
                    iteration_selection = Selection(kind="iteration", project=current_project, analysis=analysis, iteration=iteration)
                    self._tree_lookup[id(iteration_item)] = iteration_selection
                    if selection_key == self._selection_key(iteration_selection):
                        selected_item = iteration_item
            for group in current_project.experiment_groups:
                group_item = QListWidgetItem(group.name)
                self.experiment_group_list.addItem(group_item)
                self._experiment_group_lookup[id(group_item)] = group
                group_selection = Selection(kind="experiment_group", project=current_project, experiment_group=group)
                if selection_key == self._selection_key(group_selection):
                    selected_group_item = group_item
        target_item = selected_item
        if target_item is not None:
            self.tree.setCurrentItem(target_item)
            self.current_selection = self._tree_lookup[id(target_item)]
            parent = target_item.parent()
            if parent is not None:
                parent.setExpanded(True)
            self._render_selection()
        elif selected_group_item is not None and current_project is not None:
            self.experiment_group_list.setCurrentItem(selected_group_item)
            group = self._experiment_group_lookup[id(selected_group_item)]
            self.current_selection = Selection(kind="experiment_group", project=current_project, experiment_group=group)
            self._render_selection()
        elif current_project is not None:
            self.current_selection = Selection(kind="project", project=current_project)
            self._render_selection()
        self._refresh_cli_status_panel()

    def _tree_item_selected(self, item: QTreeWidgetItem) -> None:
        selection = self._tree_lookup.get(id(item))
        if selection is None:
            return
        self.current_selection = selection
        self._render_selection()
        if selection.analysis is not None:
            self._ensure_associated_session_for_selection()
        self._refresh_cli_status_panel()

    def _project_item_selected(self, item: QListWidgetItem) -> None:
        project = self._project_lookup.get(id(item))
        if project is None:
            return
        self.selected_project_id = project.project_id
        self.current_selection = Selection(kind="project", project=project)
        self.reload_workspace()

    def _experiment_group_item_selected(self, item: QListWidgetItem) -> None:
        group = self._experiment_group_lookup.get(id(item))
        project = self._selected_project()
        if group is None or project is None:
            return
        self.tree.clearSelection()
        self.current_selection = Selection(kind="experiment_group", project=project, experiment_group=group)
        self._render_selection()
        self._refresh_cli_status_panel()

    def _render_selection(self) -> None:
        selection = self.current_selection
        if selection.project is None:
            return
        self.selection_label.setText(self._selection_summary(selection))
        self._refresh_experiment_group_move_buttons()
        self._rebuild_main_tabs(selection)

    def _refresh_experiment_group_move_buttons(self) -> None:
        group = self.current_selection.experiment_group
        project = self.current_selection.project
        if group is None or project is None:
            self.move_exp_group_up_button.setEnabled(False)
            self.move_exp_group_down_button.setEnabled(False)
            return
        index = next((index for index, item in enumerate(project.experiment_groups) if item.name == group.name), -1)
        self.move_exp_group_up_button.setEnabled(index > 0)
        self.move_exp_group_down_button.setEnabled(0 <= index < len(project.experiment_groups) - 1)

    def _selection_summary(self, selection: Selection) -> str:
        if selection.kind == "project" and selection.project:
            return f"Project {selection.project.project_id}: {selection.project.title}"
        if selection.kind == "analysis" and selection.analysis:
            return f"Analysis {selection.analysis.analysis_id}: {selection.analysis.title}"
        if selection.kind == "iteration" and selection.iteration and selection.analysis:
            return f"Iteration {selection.iteration.iteration_id} in {selection.analysis.analysis_id}"
        if selection.kind == "experiment_group" and selection.experiment_group:
            return f"Experiment Group: {selection.experiment_group.name}"
        if selection.kind == "experiment_entry" and selection.experiment_entry and selection.experiment_group:
            return f"Experiment {selection.experiment_entry.exp_id} in group {selection.experiment_group.name}"
        if selection.kind == "session" and selection.session:
            return f"Session {selection.session.session_id}"
        return "No selection"

    def _selected_paths(self, selection: Selection) -> tuple[Path | None, Path | None, Path | None]:
        if selection.kind == "project" and selection.project:
            return selection.project.notes_path, selection.project.metadata_path, None
        if selection.kind in {"analysis", "session"} and selection.analysis:
            return selection.analysis.notes_path, selection.analysis.metadata_path, None
        if selection.kind == "iteration" and selection.iteration:
            return selection.iteration.notes_path, selection.iteration.metadata_path, selection.iteration.task_path
        return None, None, None

    def _rebuild_main_tabs(self, selection: Selection) -> None:
        current = self.content_tabs.currentWidget()
        self.content_tabs.clear()
        self.content_tabs.addTab(self._overview_tab(selection), "Overview")
        if selection.analysis:
            self.content_tabs.addTab(self._analysis_sessions_tab(selection.analysis), "Sessions")
        if selection.iteration:
            self.content_tabs.addTab(self._iteration_files_tab(selection.iteration, "figures"), "Figures")
            self.content_tabs.addTab(self._iteration_files_tab(selection.iteration, "videos"), "Videos")
            self.content_tabs.addTab(self._iteration_files_tab(selection.iteration, "processed"), "Processed Data")
            self.content_tabs.addTab(self._iteration_files_tab(selection.iteration, "stats"), "Stats")
            self.content_tabs.addTab(self._iteration_files_tab(selection.iteration, "code"), "Code")
            self.content_tabs.addTab(self._editable_text_tab("Task", selection.iteration.task_path), "Task")
        notes_path, metadata_path, _ = self._selected_paths(selection)
        if notes_path is not None:
            self.content_tabs.addTab(self._editable_text_tab("Notes", notes_path), "Notes")
        if metadata_path is not None:
            self.content_tabs.addTab(self._editable_text_tab("Metadata", metadata_path, yaml_mode=True), "Metadata")
        self.content_tabs.addTab(self.cli_tab, "CLI")
        if current is self.cli_tab:
            self.content_tabs.setCurrentWidget(self.cli_tab)

    def _overview_tab(self, selection: Selection) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        summary = QPlainTextEdit()
        summary.setReadOnly(True)
        lines = [self._selection_summary(selection)]
        if selection.analysis:
            lines.append(f"Current session: {selection.analysis.current_session or 'none'}")
            lines.append(f"Linked sessions: {len(selection.analysis.linked_codex_sessions)}")
            lines.append(f"Included experiment groups: {', '.join(selection.analysis.included_experiment_groups) or 'none'}")
            lines.append(f"App tmux sessions: {len(tmux_list_sessions())}")
            current = self._current_session_link()
            if current is not None:
                lines.append(f"Identity status: {current.identity_status}")
                lines.append(f"Codex id: {current.codex_id or 'unverified'}")
                lines.append(f"Codex thread: {current.codex_thread_name or 'unverified'}")
                lines.append(f"Tmux session: {self._tmux_session_name(current)}")
                lines.append(f"Tmux running: {'yes' if tmux_has_session(self._tmux_session_name(current)) else 'no'}")
            lines.append(f"Tmux list command: {codex_herder_tmux_wrapper_command()} ls")
        if selection.iteration:
            lines.append(f"Iteration status: {selection.iteration.status}")
        if selection.experiment_group:
            lines.append(f"Experiments in group: {len(selection.experiment_group.experiments)}")
            for entry in selection.experiment_group.experiments:
                lines.append(f"- {entry.exp_id} ({entry.user_id})" if entry.user_id else f"- {entry.exp_id}")
        if selection.experiment_entry:
            lines.append(f"expID: {selection.experiment_entry.exp_id}")
            lines.append(f"userID: {selection.experiment_entry.user_id or 'none'}")
        summary.setPlainText("\n".join(lines))
        layout.addWidget(summary)
        return page

    def _analysis_sessions_tab(self, analysis: Analysis) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        listing = QListWidget()
        for session in analysis.linked_codex_sessions:
            label = session.session_id if session.session_id != analysis.current_session else f"{session.session_id} [current]"
            if session.reused_from_analysis:
                label += f"  (from {session.reused_from_analysis})"
            label += f"  [{session.identity_status}]"
            label += f"  [tmux:{'up' if tmux_has_session(self._tmux_session_name(session)) else 'down'}]"
            if session.codex_thread_name:
                label += f"  -> {session.codex_thread_name}"
            listing.addItem(label)
        layout.addWidget(listing)
        return page

    def _iteration_files_tab(self, iteration: Iteration, category: str) -> QWidget:
        if category == "figures":
            page = QWidget()
            layout = QHBoxLayout(page)
            left_panel = QWidget()
            left_layout = QVBoxLayout(left_panel)
            left_layout.setContentsMargins(0, 0, 0, 0)
            listing = QTreeWidget()
            listing.setHeaderLabels(["Figure"])
            button_row = QHBoxLayout()
            rename_button = QPushButton("Rename")
            delete_button = QPushButton("Delete")
            button_row.addWidget(rename_button)
            button_row.addWidget(delete_button)
            left_layout.addWidget(listing, 1)
            left_layout.addLayout(button_row)
            preview = FigurePreviewLabel("No figure selected")
            preview.set_open_callback(self._open_figure_window)
            preview.setAlignment(Qt.AlignCenter)
            splitter = QSplitter(Qt.Horizontal)
            splitter.addWidget(left_panel)
            splitter.addWidget(preview)
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 3)
            splitter.setSizes([260, 760])
            layout.addWidget(splitter)
            assets_dir = iteration.path / "output" / "figures"
            files: list[Path] = []
            item_targets: dict[int, Path] = {}
            page._pending_select_rel = None  # type: ignore[attr-defined]

            def _show_path(path: Path | None) -> None:
                if path is None or path.is_dir():
                    preview.set_figure_path(None)
                    preview.clear_source_pixmap("No figure selected" if path is None else str(path.relative_to(assets_dir)))
                    return
                preview.set_figure_path(path)
                if path.suffix.lower() in IMAGE_EXTENSIONS:
                    pixmap = QPixmap(str(path))
                    if pixmap.isNull():
                        preview.clear_source_pixmap(str(path.relative_to(assets_dir)))
                        return
                    preview.set_source_pixmap(pixmap)
                    return
                if path.suffix.lower() in SVG_EXTENSIONS:
                    pixmap = QPixmap(900, 720)
                    pixmap.fill(Qt.white)
                    painter = QPainter(pixmap)
                    renderer = QSvgRenderer(str(path))
                    renderer.render(painter)
                    painter.end()
                    preview.set_source_pixmap(pixmap)
                    return
                preview.clear_source_pixmap(str(path.relative_to(assets_dir)))

            def _current_selected_path() -> Path | None:
                item = listing.currentItem()
                if item is None:
                    return None
                return item_targets.get(id(item))

            def _on_item_selected(item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
                _show_path(item_targets.get(id(item)) if item is not None else None)
                _refresh_button_state()

            def _on_item_double_clicked(item: QTreeWidgetItem, _column: int) -> None:
                path = item_targets.get(id(item))
                if path is not None and path.is_file():
                    _show_path(path)
                    preview.zoom_in()

            def _refresh_button_state() -> None:
                enabled = _current_selected_path() is not None
                rename_button.setEnabled(enabled)
                delete_button.setEnabled(enabled)

            def _rename_selected() -> None:
                target = _current_selected_path()
                if target is None:
                    return
                new_name, ok = QInputDialog.getText(self, "Rename Figure Item", "New name:", text=target.name)
                if not ok:
                    return
                new_name = new_name.strip()
                if not new_name or new_name == target.name or "/" in new_name:
                    return
                destination = target.parent / new_name
                if destination.exists():
                    QMessageBox.warning(self, "Rename Figure Item", f"{new_name} already exists.")
                    return
                target.rename(destination)
                page._pending_select_rel = str(destination.relative_to(assets_dir))  # type: ignore[attr-defined]
                _refresh()

            def _delete_selected() -> None:
                target = _current_selected_path()
                if target is None:
                    return
                noun = "folder" if target.is_dir() else ("figure" if category == "figures" else "video")
                label = str(target.relative_to(assets_dir))
                if QMessageBox.question(self, "Delete Figure Item", f"Delete {noun} {label}?") != QMessageBox.Yes:
                    return
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
                page._pending_select_rel = None  # type: ignore[attr-defined]
                _refresh()

            def _refresh() -> None:
                nonlocal files
                selected_rel = getattr(page, "_pending_select_rel", None)  # type: ignore[attr-defined]
                selected_path = None
                if selected_rel is None:
                    selected_path = _current_selected_path()
                    if selected_path is not None:
                        selected_rel = str(selected_path.relative_to(assets_dir))
                scroll_values = (listing.verticalScrollBar().value(), listing.horizontalScrollBar().value())
                new_files = [
                    path
                    for path in list_tree_files(assets_dir)
                    if path.suffix.lower() in (IMAGE_EXTENSIONS | SVG_EXTENSIONS)
                ]
                new_keys = [str(path.relative_to(assets_dir)) for path in new_files]
                old_keys = [str(path.relative_to(assets_dir)) for path in files]
                if new_keys == old_keys:
                    _show_path(selected_path)
                    return
                files = new_files
                expanded_paths = _expanded_tree_paths(listing, item_targets, assets_dir)
                item_targets.clear()
                selected_item: QTreeWidgetItem | None = None
                listing.blockSignals(True)
                listing.clear()
                folder_items: dict[tuple[str, ...], QTreeWidgetItem] = {}
                for path in files:
                    relative = path.relative_to(assets_dir)
                    parts = relative.parts
                    parent_item: QTreeWidgetItem | None = None
                    parent_key: tuple[str, ...] = ()
                    for folder in parts[:-1]:
                        folder_key = parent_key + (folder,)
                        folder_item = folder_items.get(folder_key)
                        if folder_item is None:
                            folder_item = QTreeWidgetItem([folder])
                            if parent_item is None:
                                listing.addTopLevelItem(folder_item)
                            else:
                                parent_item.addChild(folder_item)
                            folder_items[folder_key] = folder_item
                            item_targets[id(folder_item)] = assets_dir.joinpath(*folder_key)
                            folder_item.setExpanded("/".join(folder_key) in expanded_paths)
                            if selected_rel is not None and "/".join(folder_key) == selected_rel:
                                selected_item = folder_item
                        parent_item = folder_item
                        parent_key = folder_key
                    file_item = QTreeWidgetItem([parts[-1]])
                    if parent_item is None:
                        listing.addTopLevelItem(file_item)
                    else:
                        parent_item.addChild(file_item)
                    item_targets[id(file_item)] = path
                    if selected_rel is not None and str(relative) == selected_rel:
                        selected_item = file_item
                if selected_rel is not None and selected_item is None and files:
                    target_path = files[0]
                    for _item_id, path in item_targets.items():
                        if path == target_path:
                            for tree_item in listing.findItems(path.name, Qt.MatchRecursive | Qt.MatchExactly, 0):
                                if item_targets.get(id(tree_item)) == path:
                                    selected_item = tree_item
                                    break
                            if selected_item is not None:
                                break
                if selected_item is not None:
                    ancestor = selected_item.parent()
                    while ancestor is not None:
                        ancestor.setExpanded(True)
                        ancestor = ancestor.parent()
                    listing.setCurrentItem(selected_item)
                else:
                    listing.setCurrentItem(None)
                listing.blockSignals(False)
                page._pending_select_rel = None  # type: ignore[attr-defined]
                _show_path(item_targets.get(id(selected_item)) if selected_item is not None else None)
                _refresh_button_state()
                QTimer.singleShot(
                    0,
                    lambda values=scroll_values: (
                        listing.verticalScrollBar().setValue(values[0]),
                        listing.horizontalScrollBar().setValue(values[1]),
                    ),
                )

            listing.currentItemChanged.connect(_on_item_selected)
            listing.itemDoubleClicked.connect(_on_item_double_clicked)
            rename_button.clicked.connect(_rename_selected)
            delete_button.clicked.connect(_delete_selected)
            page._refresh_callback = _refresh  # type: ignore[attr-defined]
            _refresh()
            return page
        if category == "videos":
            page = QWidget()
            layout = QHBoxLayout(page)
            left_panel = QWidget()
            left_layout = QVBoxLayout(left_panel)
            left_layout.setContentsMargins(0, 0, 0, 0)
            listing = QTreeWidget()
            listing.setHeaderLabels(["Video"])
            button_row = QHBoxLayout()
            rename_button = QPushButton("Rename")
            delete_button = QPushButton("Delete")
            button_row.addWidget(rename_button)
            button_row.addWidget(delete_button)
            left_layout.addWidget(listing, 1)
            left_layout.addLayout(button_row)

            preview_panel = QWidget()
            preview_layout = QVBoxLayout(preview_panel)
            preview_layout.setContentsMargins(0, 0, 0, 0)
            video_label = QLabel("No video selected")
            video_label.setAlignment(Qt.AlignCenter)
            video_scroll = QScrollArea()
            video_scroll.setWidgetResizable(True)
            video_scroll.setWidget(video_label)
            controls_row = QHBoxLayout()
            play_button = QPushButton("Play")
            prev_button = QPushButton("Prev")
            next_button = QPushButton("Next")
            frame_slider = QSlider(Qt.Horizontal)
            fps_label = QLabel("FPS 30")
            fps_slider = QSlider(Qt.Horizontal)
            fps_slider.setRange(5, 200)
            fps_slider.setValue(30)
            controls_row.addWidget(play_button)
            controls_row.addWidget(prev_button)
            controls_row.addWidget(next_button)
            controls_row.addWidget(frame_slider, 1)
            controls_row.addWidget(fps_label)
            controls_row.addWidget(fps_slider)
            min_label = QLabel("Min 2%")
            min_slider = QSlider(Qt.Horizontal)
            min_slider.setRange(0, 1000)
            max_label = QLabel("Max 98%")
            max_slider = QSlider(Qt.Horizontal)
            max_slider.setRange(0, 1000)
            preview_layout.addWidget(video_scroll, 1)
            preview_layout.addLayout(controls_row)
            preview_layout.addWidget(min_label)
            preview_layout.addWidget(min_slider)
            preview_layout.addWidget(max_label)
            preview_layout.addWidget(max_slider)

            layout.addWidget(left_panel, 1)
            layout.addWidget(preview_panel, 3)

            assets_dir = iteration.path / "output" / "videos"
            item_targets: dict[int, Path] = {}
            asset_targets: list[Path] = []
            asset_keys: list[str] = []
            page._pending_select_rel = None  # type: ignore[attr-defined]
            page._video_array = None  # type: ignore[attr-defined]
            page._video_frames = 0  # type: ignore[attr-defined]
            page._video_mode = None  # type: ignore[attr-defined]
            page._video_data_min = 0.0  # type: ignore[attr-defined]
            page._video_data_max = 1.0  # type: ignore[attr-defined]
            page._video_frame_index = 0  # type: ignore[attr-defined]
            page._video_timer = QTimer(page)  # type: ignore[attr-defined]
            page._video_timer.setInterval(33)  # type: ignore[attr-defined]
            page._video_selected_path = None  # type: ignore[attr-defined]
            page._video_source_fps = 10.0  # type: ignore[attr-defined]
            page._video_frame_step = 1  # type: ignore[attr-defined]

            def _video_pair_paths(target: Path) -> list[Path]:
                if target.is_dir():
                    return [target]
                if target.suffix.lower() == ".npy":
                    base = target.with_suffix("")
                else:
                    base = target.with_suffix("")
                companions = []
                for suffix in [".npy", ".mp4"]:
                    candidate = base.with_suffix(suffix)
                    if candidate.exists():
                        companions.append(candidate)
                if companions:
                    return companions
                return [target]

            def _preferred_video_path(target: Path) -> Path:
                if target.suffix.lower() == ".npy":
                    return target
                candidate = target.with_suffix(".npy")
                return candidate if candidate.exists() else target

            def _video_source_mp4_path(path: Path) -> Path | None:
                if path.suffix.lower() == ".mp4":
                    return path if path.exists() else None
                candidate = path.with_suffix(".mp4")
                return candidate if candidate.exists() else None

            def _read_video_fps(path: Path | None) -> float:
                if path is None or cv2 is None:
                    return 0.0
                cap = cv2.VideoCapture(str(path))
                if not cap.isOpened():
                    return 0.0
                try:
                    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                finally:
                    cap.release()
                return fps

            def _load_video_array(path: Path) -> tuple[np.ndarray, str, float]:
                array = np.load(path, mmap_mode="r")
                fps = _read_video_fps(_video_source_mp4_path(path))
                if fps <= 0:
                    fps = 10.0
                if array.ndim == 3:
                    return array, "gray", fps
                if array.ndim == 4:
                    if array.shape[-1] in (1, 3, 4):
                        return array, "channels_last", fps
                    if array.shape[1] in (1, 3, 4):
                        return np.transpose(array, (0, 2, 3, 1)), "channels_last", fps
                raise ValueError(f"Unsupported video array shape: {array.shape}")

            def _load_video_from_mp4(path: Path) -> tuple[np.ndarray, str, float]:
                if cv2 is None:
                    raise RuntimeError("OpenCV is not available to decode mp4 video.")
                cap = cv2.VideoCapture(str(path))
                if not cap.isOpened():
                    raise RuntimeError(f"Could not open video: {path}")
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                frames: list[np.ndarray] = []
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    frames.append(frame_rgb)
                cap.release()
                if not frames:
                    raise RuntimeError(f"No frames could be read from {path}")
                array = np.stack(frames, axis=0)
                if fps <= 0:
                    fps = 10.0
                return array, "channels_last", fps

            def _apply_video_playback_rate() -> None:
                effective_fps = float(fps_slider.value())
                tick_ms = 33
                page._video_timer.setInterval(tick_ms)  # type: ignore[attr-defined]
                frames_per_tick = max(1, int(round(effective_fps * (tick_ms / 1000.0))))
                page._video_frame_step = frames_per_tick  # type: ignore[attr-defined]
                fps_label.setText(f"FPS {int(round(effective_fps))}")

            def _sample_percentiles(array: np.ndarray) -> tuple[float, float, float, float]:
                if array.shape[0] == 0:
                    return 0.0, 1.0, 0.0, 1.0
                step = max(1, array.shape[0] // 20)
                sample = np.asarray(array[::step], dtype=np.float32)
                data_min = float(np.nanmin(sample))
                data_max = float(np.nanmax(sample))
                p2 = float(np.nanpercentile(sample, 2))
                p98 = float(np.nanpercentile(sample, 98))
                if data_max <= data_min:
                    data_max = data_min + 1.0
                return data_min, data_max, p2, p98

            def _slider_from_value(value: float, data_min: float, data_max: float) -> int:
                ratio = (value - data_min) / max(data_max - data_min, 1e-12)
                return int(round(max(0.0, min(1.0, ratio)) * 1000))

            def _value_from_slider(slider_value: int, data_min: float, data_max: float) -> float:
                return data_min + (slider_value / 1000.0) * (data_max - data_min)

            def _render_video_frame() -> None:
                array = getattr(page, "_video_array", None)
                if array is None:
                    return
                frame_index = int(getattr(page, "_video_frame_index", 0))
                frame = np.asarray(array[frame_index], dtype=np.float32)
                data_min = float(getattr(page, "_video_data_min", 0.0))
                data_max = float(getattr(page, "_video_data_max", 1.0))
                min_value = _value_from_slider(min_slider.value(), data_min, data_max)
                max_value = _value_from_slider(max_slider.value(), data_min, data_max)
                if max_value <= min_value:
                    max_value = min_value + 1e-6
                frame = np.clip((frame - min_value) / (max_value - min_value), 0.0, 1.0)
                if frame.ndim == 2:
                    frame_uint8 = (frame * 255.0).astype(np.uint8)
                    image = QImage(frame_uint8.data, frame_uint8.shape[1], frame_uint8.shape[0], frame_uint8.strides[0], QImage.Format_Grayscale8)
                else:
                    if frame.shape[-1] == 1:
                        frame = frame[..., 0]
                        frame_uint8 = (frame * 255.0).astype(np.uint8)
                        image = QImage(frame_uint8.data, frame_uint8.shape[1], frame_uint8.shape[0], frame_uint8.strides[0], QImage.Format_Grayscale8)
                    else:
                        rgb = frame[..., :3]
                        rgb_uint8 = (rgb * 255.0).astype(np.uint8)
                        image = QImage(rgb_uint8.data, rgb_uint8.shape[1], rgb_uint8.shape[0], rgb_uint8.strides[0], QImage.Format_RGB888)
                pixmap = QPixmap.fromImage(image.copy())
                target = video_scroll.viewport().size()
                if target.width() < 16 or target.height() < 16:
                    target = preview_panel.size()
                if target.width() < 16 or target.height() < 16:
                    target = QSize(pixmap.width(), pixmap.height())
                scaled = pixmap.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                video_label.setPixmap(scaled)
                video_label.setMinimumSize(scaled.size())
                video_label.setText("")

            def _set_video_frame(index: int) -> None:
                frame_count = int(getattr(page, "_video_frames", 0))
                if frame_count <= 0:
                    return
                page._video_frame_index = max(0, min(index, frame_count - 1))  # type: ignore[attr-defined]
                frame_slider.blockSignals(True)
                frame_slider.setValue(page._video_frame_index)  # type: ignore[arg-type]
                frame_slider.blockSignals(False)
                _render_video_frame()

            def _advance_video_frame() -> None:
                frame_count = int(getattr(page, "_video_frames", 0))
                if frame_count <= 0:
                    return
                frame_step = int(getattr(page, "_video_frame_step", 1))
                next_index = page._video_frame_index + max(1, frame_step)  # type: ignore[attr-defined]
                if next_index >= frame_count:
                    next_index = 0
                _set_video_frame(next_index)

            def _stop_video_playback() -> None:
                page._video_timer.stop()  # type: ignore[attr-defined]
                play_button.setText("Play")

            def _toggle_video_playback() -> None:
                frame_count = int(getattr(page, "_video_frames", 0))
                if frame_count <= 0:
                    return
                if page._video_timer.isActive():  # type: ignore[attr-defined]
                    _stop_video_playback()
                else:
                    _apply_video_playback_rate()
                    page._video_timer.start()  # type: ignore[attr-defined]
                    play_button.setText("Pause")

            def _clear_video_preview(message: str) -> None:
                _stop_video_playback()
                page._video_array = None  # type: ignore[attr-defined]
                page._video_frames = 0  # type: ignore[attr-defined]
                page._video_frame_index = 0  # type: ignore[attr-defined]
                page._video_selected_path = None  # type: ignore[attr-defined]
                page._video_source_fps = 10.0  # type: ignore[attr-defined]
                page._video_frame_step = 1  # type: ignore[attr-defined]
                video_label.setText(message)
                video_label.setPixmap(QPixmap())
                video_label.setMinimumSize(QSize(0, 0))
                frame_slider.setRange(0, 0)
                frame_slider.setValue(0)
                min_slider.setValue(20)
                max_slider.setValue(980)
                play_button.setEnabled(False)
                prev_button.setEnabled(False)
                next_button.setEnabled(False)
                fps_slider.setEnabled(False)
                min_slider.setEnabled(False)
                max_slider.setEnabled(False)

            def _show_video_path(path: Path | None) -> None:
                if path is None:
                    _clear_video_preview("No video selected")
                    return
                if path.is_dir():
                    _clear_video_preview(str(path.relative_to(assets_dir)))
                    return
                page._video_selected_path = path  # type: ignore[attr-defined]
                npy_path = _preferred_video_path(path)
                try:
                    if npy_path.suffix.lower() == ".npy" and npy_path.exists():
                        array, _mode, fps = _load_video_array(npy_path)
                    elif path.suffix.lower() == ".mp4":
                        array, _mode, fps = _load_video_from_mp4(path)
                        np.save(path.with_suffix(".npy"), array)
                        npy_path = path.with_suffix(".npy")
                    else:
                        raise RuntimeError(
                            "No companion .npy file found for GUI preview, and this file is not a supported mp4 fallback."
                        )
                    data_min, data_max, p2, p98 = _sample_percentiles(array)
                except Exception as exc:
                    _clear_video_preview(f"Unable to load video array.\n\n{npy_path}\n\n{exc}")
                    return
                page._video_array = array  # type: ignore[attr-defined]
                page._video_frames = int(array.shape[0])  # type: ignore[attr-defined]
                page._video_data_min = data_min  # type: ignore[attr-defined]
                page._video_data_max = data_max  # type: ignore[attr-defined]
                page._video_frame_index = 0  # type: ignore[attr-defined]
                page._video_source_fps = fps  # type: ignore[attr-defined]
                frame_slider.setRange(0, max(0, int(array.shape[0]) - 1))
                min_slider.setEnabled(True)
                max_slider.setEnabled(True)
                play_button.setEnabled(True)
                prev_button.setEnabled(True)
                next_button.setEnabled(True)
                fps_slider.setEnabled(True)
                min_slider.blockSignals(True)
                max_slider.blockSignals(True)
                min_slider.setValue(_slider_from_value(p2, data_min, data_max))
                max_slider.setValue(_slider_from_value(p98, data_min, data_max))
                min_slider.blockSignals(False)
                max_slider.blockSignals(False)
                min_label.setText(f"Min {p2:.3g}")
                max_label.setText(f"Max {p98:.3g}")
                _apply_video_playback_rate()
                _set_video_frame(0)
                QTimer.singleShot(0, _render_video_frame)

            def _current_selected_path() -> Path | None:
                item = listing.currentItem()
                if item is None:
                    return None
                return item_targets.get(id(item))

            def _on_item_selected(item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
                _show_video_path(item_targets.get(id(item)) if item is not None else None)
                _refresh_button_state()

            def _on_item_double_clicked(item: QTreeWidgetItem, _column: int) -> None:
                path = item_targets.get(id(item))
                if path is None or path.is_dir():
                    return
                _show_video_path(path)
                if int(getattr(page, "_video_frames", 0)) > 0 and not page._video_timer.isActive():  # type: ignore[attr-defined]
                    _toggle_video_playback()

            def _refresh_button_state() -> None:
                enabled = _current_selected_path() is not None
                rename_button.setEnabled(enabled)
                delete_button.setEnabled(enabled)

            def _rename_selected() -> None:
                target = _current_selected_path()
                if target is None:
                    return
                new_name, ok = QInputDialog.getText(self, "Rename Video Item", "New name:", text=target.stem if target.is_file() else target.name)
                if not ok:
                    return
                new_name = new_name.strip()
                if not new_name or "/" in new_name:
                    return
                if target.is_dir():
                    destination = target.parent / new_name
                    if destination.exists():
                        QMessageBox.warning(self, "Rename Video Item", f"{new_name} already exists.")
                        return
                    target.rename(destination)
                    page._pending_select_rel = str(destination.relative_to(assets_dir))  # type: ignore[attr-defined]
                    _refresh()
                    return
                siblings = _video_pair_paths(target)
                for sibling in siblings:
                    destination = sibling.with_name(f"{new_name}{sibling.suffix}")
                    if destination.exists() and destination != sibling:
                        QMessageBox.warning(self, "Rename Video Item", f"{destination.name} already exists.")
                        return
                for sibling in siblings:
                    sibling.rename(sibling.with_name(f"{new_name}{sibling.suffix}"))
                preferred = target.with_name(f"{new_name}{target.suffix}")
                page._pending_select_rel = str(preferred.relative_to(assets_dir))  # type: ignore[attr-defined]
                _refresh()

            def _delete_selected() -> None:
                target = _current_selected_path()
                if target is None:
                    return
                noun = "folder" if target.is_dir() else "video"
                label = str(target.relative_to(assets_dir))
                if QMessageBox.question(self, "Delete Video Item", f"Delete {noun} {label}?") != QMessageBox.Yes:
                    return
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    for sibling in _video_pair_paths(target):
                        sibling.unlink(missing_ok=True)
                page._pending_select_rel = None  # type: ignore[attr-defined]
                _refresh()

            def _refresh() -> None:
                nonlocal asset_keys
                selected_rel = getattr(page, "_pending_select_rel", None)  # type: ignore[attr-defined]
                selected_path = None
                if selected_rel is None:
                    selected_path = _current_selected_path()
                    if selected_path is not None:
                        selected_rel = str(selected_path.relative_to(assets_dir))
                scroll_values = (listing.verticalScrollBar().value(), listing.horizontalScrollBar().value())
                all_paths = [path for path in list_tree_files(assets_dir) if path.suffix.lower() in VIDEO_EXTENSIONS]
                groups: dict[tuple[str, str], list[Path]] = {}
                for path in all_paths:
                    relative = path.relative_to(assets_dir)
                    groups.setdefault(("/".join(relative.parts[:-1]), path.stem), []).append(path)
                new_asset_keys = [
                    str(
                        (
                            next((path for path in siblings if path.suffix.lower() == ".npy"), siblings[0])
                        ).relative_to(assets_dir)
                    )
                    for _, siblings in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1].lower()))
                ]
                if new_asset_keys == asset_keys:
                    if (
                        selected_path is not None
                        and selected_path == getattr(page, "_video_selected_path", None)
                        and getattr(page, "_video_array", None) is not None
                    ):
                        _render_video_frame()
                    else:
                        _show_video_path(selected_path)
                    _refresh_button_state()
                    return
                asset_keys = new_asset_keys
                expanded_paths = _expanded_tree_paths(listing, item_targets, assets_dir)
                asset_targets.clear()
                listing.blockSignals(True)
                listing.clear()
                folder_items: dict[tuple[str, ...], QTreeWidgetItem] = {}
                selected_item: QTreeWidgetItem | None = None
                for _, siblings in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1].lower())):
                    preferred = next((path for path in siblings if path.suffix.lower() == ".npy"), siblings[0])
                    asset_targets.append(preferred)
                    relative = preferred.relative_to(assets_dir)
                    parts = relative.parts
                    parent_item: QTreeWidgetItem | None = None
                    parent_key: tuple[str, ...] = ()
                    for folder in parts[:-1]:
                        folder_key = parent_key + (folder,)
                        folder_item = folder_items.get(folder_key)
                        if folder_item is None:
                            folder_item = QTreeWidgetItem([folder])
                            if parent_item is None:
                                listing.addTopLevelItem(folder_item)
                            else:
                                parent_item.addChild(folder_item)
                            folder_items[folder_key] = folder_item
                            item_targets[id(folder_item)] = assets_dir.joinpath(*folder_key)
                            folder_item.setExpanded("/".join(folder_key) in expanded_paths)
                            if selected_rel is not None and "/".join(folder_key) == selected_rel:
                                selected_item = folder_item
                        parent_item = folder_item
                        parent_key = folder_key
                    label = preferred.stem
                    if any(path.suffix.lower() == ".mp4" for path in siblings) and any(path.suffix.lower() == ".npy" for path in siblings):
                        label += " [npy+mp4]"
                    elif any(path.suffix.lower() == ".mp4" for path in siblings):
                        label += " [mp4]"
                    elif any(path.suffix.lower() == ".npy" for path in siblings):
                        label += " [npy]"
                    file_item = QTreeWidgetItem([label])
                    if parent_item is None:
                        listing.addTopLevelItem(file_item)
                    else:
                        parent_item.addChild(file_item)
                    item_targets[id(file_item)] = preferred
                    if selected_rel is not None and str(relative) == selected_rel:
                        selected_item = file_item
                if selected_item is not None:
                    ancestor = selected_item.parent()
                    while ancestor is not None:
                        ancestor.setExpanded(True)
                        ancestor = ancestor.parent()
                    listing.setCurrentItem(selected_item)
                else:
                    listing.setCurrentItem(None)
                listing.blockSignals(False)
                page._pending_select_rel = None  # type: ignore[attr-defined]
                _show_video_path(item_targets.get(id(selected_item)) if selected_item is not None else None)
                _refresh_button_state()
                QTimer.singleShot(
                    0,
                    lambda values=scroll_values: (
                        listing.verticalScrollBar().setValue(values[0]),
                        listing.horizontalScrollBar().setValue(values[1]),
                    ),
                )

            def _update_minmax_labels() -> None:
                data_min = float(getattr(page, "_video_data_min", 0.0))
                data_max = float(getattr(page, "_video_data_max", 1.0))
                min_value = _value_from_slider(min_slider.value(), data_min, data_max)
                max_value = _value_from_slider(max_slider.value(), data_min, data_max)
                if min_slider.value() >= max_slider.value():
                    if self.sender() is min_slider:
                        max_slider.setValue(min(min_slider.value() + 1, 1000))
                    else:
                        min_slider.setValue(max(max_slider.value() - 1, 0))
                    min_value = _value_from_slider(min_slider.value(), data_min, data_max)
                    max_value = _value_from_slider(max_slider.value(), data_min, data_max)
                min_label.setText(f"Min {min_value:.3g}")
                max_label.setText(f"Max {max_value:.3g}")
                _render_video_frame()

            page._video_timer.timeout.connect(_advance_video_frame)  # type: ignore[attr-defined]
            play_button.clicked.connect(_toggle_video_playback)
            prev_button.clicked.connect(lambda: _set_video_frame(int(getattr(page, "_video_frame_index", 0)) - 1))
            next_button.clicked.connect(lambda: _set_video_frame(int(getattr(page, "_video_frame_index", 0)) + 1))
            frame_slider.valueChanged.connect(_set_video_frame)
            fps_slider.valueChanged.connect(lambda value: (fps_label.setText(f"FPS {value}"), _apply_video_playback_rate()))
            min_slider.valueChanged.connect(_update_minmax_labels)
            video_scroll.viewport().installEventFilter(self)
            page._video_render_callback = _render_video_frame  # type: ignore[attr-defined]
            max_slider.valueChanged.connect(_update_minmax_labels)
            rename_button.clicked.connect(_rename_selected)
            delete_button.clicked.connect(_delete_selected)
            listing.currentItemChanged.connect(_on_item_selected)
            listing.itemDoubleClicked.connect(_on_item_double_clicked)
            _clear_video_preview("No video selected")
            page._refresh_callback = _refresh  # type: ignore[attr-defined]
            _refresh()
            return page
        listing, preview, page = self._build_text_preview("")
        if category == "processed":
            source_dir = iteration.path / "output" / "processed_data"
            source_loader = lambda: list_files(source_dir)
            display_name = lambda path: path.name
        elif category == "stats":
            source_dir = iteration.path / "output" / "stats"
            source_loader = lambda: list_files(source_dir)
            display_name = lambda path: path.name
        else:
            source_dir = iteration.path / "code"
            source_loader = lambda: list_tree_files(source_dir)
            display_name = lambda path: str(path.relative_to(source_dir))
        files = source_loader()
        for path in files:
            listing.addItem(display_name(path))
        def _show(row: int) -> None:
            if row < 0 or row >= len(files):
                preview.clear()
                return
            path = files[row]
            if path.suffix.lower() in PREVIEW_EXTENSIONS:
                preview.setPlainText(path.read_text(encoding="utf-8", errors="replace"))
            else:
                preview.setPlainText(path.name)
        listing.currentRowChanged.connect(_show)
        def _refresh() -> None:
            nonlocal files
            selected_key = display_name(files[listing.currentRow()]) if 0 <= listing.currentRow() < len(files) else None
            new_files = source_loader()
            new_keys = [display_name(path) for path in new_files]
            old_keys = [display_name(path) for path in files]
            if new_keys == old_keys:
                current_row = listing.currentRow()
                if 0 <= current_row < len(files):
                    _show(current_row)
                return
            files = new_files
            listing.blockSignals(True)
            listing.clear()
            for key in new_keys:
                listing.addItem(key)
            target_row = -1
            if selected_key is not None:
                for idx, key in enumerate(new_keys):
                    if key == selected_key:
                        target_row = idx
                        break
            if target_row < 0 and files:
                target_row = 0
            listing.setCurrentRow(target_row)
            listing.blockSignals(False)
            _show(target_row)
        page._refresh_callback = _refresh  # type: ignore[attr-defined]
        _refresh()
        return page

    def _editable_text_tab(self, label: str, path: Path, yaml_mode: bool = False) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        editor = QTextEdit() if not yaml_mode else QPlainTextEdit()
        if yaml_mode:
            editor.setPlainText(metadata_text(path))
        else:
            editor.setPlainText(read_notes(path))
        layout.addWidget(editor)
        save_button = QPushButton(f"Save {label}")
        layout.addWidget(save_button)

        def _save() -> None:
            try:
                if yaml_mode:
                    write_metadata_text(path, editor.toPlainText())  # type: ignore[attr-defined]
                else:
                    set_notes(path, editor.toPlainText())  # type: ignore[attr-defined]
            except Exception as exc:
                QMessageBox.critical(self, f"Save {label}", str(exc))
                return
            self.reload_workspace()

        save_button.clicked.connect(_save)
        return page

    def _create_project_dialog(self) -> None:
        label, ok = QInputDialog.getText(self, "New Project", "Project name:", text="")
        if not ok or not label.strip():
            return
        project = create_project(label.strip(), label.strip())
        self.selected_project_id = project.project_id
        self.current_selection = Selection(kind="project", project=project)
        self.reload_workspace()

    def _create_analysis_dialog(self) -> None:
        project = self._selected_project()
        if project is None:
            QMessageBox.warning(self, "Select Project", "Select a project first.")
            return
        label, ok = QInputDialog.getText(self, "New Analysis", "Analysis name:", text="")
        if not ok or not label.strip():
            return
        included_groups: list[str] = []
        if project.experiment_groups:
            group_dialog = ExperimentGroupSelectionDialog(project.experiment_groups, parent=self)
            if group_dialog.exec() == QDialog.Accepted:
                included_groups = group_dialog.selected_group_names()
        analysis_id = label.strip()
        title = label.strip()
        analysis = create_analysis(project, analysis_id, title, included_experiment_groups=included_groups, link_session=True)
        iteration = create_iteration(project, analysis, "iter_001")
        self.selected_project_id = project.project_id
        self.current_selection = Selection(kind="iteration", project=project, analysis=analysis, iteration=iteration)
        self.reload_workspace()
        if self.current_selection.analysis is not None:
            self._launch_session_for_bundle(
                self.current_selection.project,
                self.current_selection.analysis,
                self.current_selection.iteration,
                self.current_selection.analysis.current_session or f"{project.project_id}_{analysis_id}_main",
                "Main session",
                make_current=True,
            )
        self.content_tabs.setCurrentWidget(self.cli_tab)
        self._refresh_cli_status_panel()

    def _create_experiment_group_dialog(self) -> None:
        project = self._selected_project()
        if project is None:
            QMessageBox.warning(self, "Select Project", "Select a project first.")
            return
        name, ok = QInputDialog.getText(self, "New Experiment Group", "Group name:", text="")
        if not ok or not name.strip():
            return
        user_ids = self._available_user_ids()
        dialog = ExperimentGroupDialog(user_ids, name.strip(), parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        group = dialog.result_group()
        upsert_experiment_group(project, group)
        self.current_selection = Selection(kind="experiment_group", project=project, experiment_group=group)
        self.reload_workspace()

    def _edit_selected_experiment_group(self, *_args) -> None:
        project = self._selected_project()
        group = self.current_selection.experiment_group
        if project is None or group is None:
            return
        user_ids = self._available_user_ids()
        dialog = ExperimentGroupDialog(user_ids, group.name, group=group, parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        updated = dialog.result_group()
        if updated.name != group.name:
            delete_experiment_group(project, group.name)
        upsert_experiment_group(project, updated)
        self.current_selection = Selection(kind="experiment_group", project=project, experiment_group=updated)
        self.reload_workspace()

    def _rename_selected_experiment_group(self) -> None:
        project = self._selected_project()
        group = self.current_selection.experiment_group
        if project is None or group is None:
            QMessageBox.information(self, "Rename Experiment Group", "Select an experiment group first.")
            return
        new_name, ok = QInputDialog.getText(self, "Rename Experiment Group", "Group name:", text=group.name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == group.name:
            return
        if any(item.name == new_name for item in project.experiment_groups):
            QMessageBox.warning(self, "Rename Experiment Group", f"Experiment group {new_name} already exists.")
            return
        delete_experiment_group(project, group.name)
        updated = ExperimentGroup(name=new_name, experiments=list(group.experiments))
        upsert_experiment_group(project, updated)
        self.current_selection = Selection(kind="experiment_group", project=project, experiment_group=updated)
        self.reload_workspace()

    def _delete_selected_experiment_group(self) -> None:
        selection = self.current_selection
        if selection.experiment_group is None or selection.project is None:
            QMessageBox.information(self, "Delete Experiment Group", "Select an experiment group first.")
            return
        if QMessageBox.question(self, "Delete Experiment Group", f"Delete experiment group {selection.experiment_group.name}?") != QMessageBox.Yes:
            return
        delete_experiment_group(selection.project, selection.experiment_group.name)
        self.current_selection = Selection(kind="project", project=selection.project)
        self.reload_workspace()

    def _move_selected_experiment_group(self, direction: int) -> None:
        selection = self.current_selection
        if selection.project is None or selection.experiment_group is None:
            return
        move_experiment_group(selection.project, selection.experiment_group.name, direction)
        self.reload_workspace()

    def _launch_new_session(self) -> None:
        bundle = self._selection_bundle_for_analysis()
        if bundle is None:
            QMessageBox.warning(self, "Select Analysis", "Select an analysis or iteration first.")
            return
        self._ensure_associated_session_for_selection(show_errors=True, switch_to_cli=True, copy_command=True)

    def _launch_session_for_bundle(
        self,
        project: Project,
        analysis: Analysis,
        iteration: Iteration | None,
        session_id: str,
        label: str,
        *,
        make_current: bool,
        switch_to_cli: bool = True,
        copy_command: bool = True,
    ) -> None:
        existing = next((item for item in analysis.linked_codex_sessions if item.session_id == session_id), None)
        link = existing or session_link(session_id, label, bootstrap_sent=True)
        if not link.conda_env:
            conda_env = self._choose_conda_env()
            if conda_env is None:
                return
            link.conda_env = conda_env
        mark_launch_record(link, launched=True)
        upsert_session_link(analysis, link, make_current=False)
        if iteration is not None:
            self._set_iteration_session(iteration, session_id)
        tmux_name = self._tmux_session_name(link)
        if tmux_has_session(tmux_name):
            if make_current:
                upsert_session_link(analysis, link, make_current=True)
        else:
            task_text = iteration.task_path.read_text(encoding="utf-8", errors="replace") if iteration and iteration.task_path.exists() else ""
            index_snapshot = session_index_snapshot()
            db_snapshot = thread_db_snapshot()
            spec = build_new_session_spec(project, analysis, iteration, session_id, task_text, conda_env=link.conda_env)
            tmux_create_session(tmux_name, spec.command, spec.workdir)
            self._auto_accept_trust_prompt(tmux_name, allow_retry=False)
            self._start_pending_session_verification(
                project=project,
                analysis=analysis,
                session_id=session_id,
                tmux_name=tmux_name,
                index_snapshot=index_snapshot,
                db_snapshot=db_snapshot,
                repo_root=spec.workdir,
                first_user_message=spec.command[-1] if spec.command else "",
                launched_at=int(time.time()) - 1,
                make_current=make_current,
            )
        if switch_to_cli:
            self.content_tabs.setCurrentWidget(self.cli_tab)
        if copy_command:
            self._copy_current_session_command()
        self._refresh_cli_status_panel()
        self.reload_workspace()

    def _resume_selected_session(self) -> None:
        self._ensure_associated_session_for_selection(show_errors=True, switch_to_cli=True, copy_command=False)

    def _link_existing_session(self) -> None:
        QMessageBox.information(self, "One Session Per Analysis", "Each analysis now uses a single associated Codex session created when the analysis is made.")

    def _copy_current_session_command(self) -> None:
        session = self._target_session_for_selection(self.current_selection)
        if session is None:
            QMessageBox.information(self, "Copy Codex Session", "Select an analysis or iteration with a linked Codex session first.")
            return
        tmux_name = self._tmux_session_name(session)
        if not tmux_has_session(tmux_name):
            if not self._ensure_session_terminal(session):
                QMessageBox.warning(
                    self,
                    "Copy Codex Session",
                    (
                        f"The associated session `{session.session_id}` is not currently running in tmux "
                        "and cannot be resumed because no verified Codex identity is stored for it."
                    ),
                )
                return
            self.reload_workspace()
        command = (
            f"ssh -t dream \"cd /home/adamranson/code/codex_herder && "
            f"./bin/codex-herder-tmux attach -t '{tmux_name}'\""
        )
        QApplication.clipboard().setText(command)

    def _choose_conda_env(self) -> str | None:
        envs = self._available_conda_envs()
        default_index = envs.index("sci") if "sci" in envs else 0
        choice, ok = QInputDialog.getItem(
            self,
            "Conda Environment",
            "Conda environment for this Codex session:",
            envs,
            current=default_index,
            editable=False,
        )
        if not ok or not choice:
            return None
        return str(choice)

    def _available_conda_envs(self) -> list[str]:
        try:
            result = subprocess.run(
                ["conda", "env", "list", "--json"],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(result.stdout)
            names = []
            for env_path in payload.get("envs", []):
                env_name = Path(env_path).name
                names.append("base" if env_name == "miniconda3" else env_name)
            deduped = []
            for name in names:
                if name not in deduped:
                    deduped.append(name)
            if deduped:
                if "sci" in deduped:
                    deduped.insert(0, deduped.pop(deduped.index("sci")))
                return deduped
        except Exception:
            pass
        return ["sci"]

    def _ask_codex_to_create_iteration(self) -> None:
        bundle = self._selection_bundle_for_analysis()
        if bundle is None:
            QMessageBox.warning(self, "Select Analysis", "Select an analysis or iteration first.")
            return
        project, analysis, iteration = bundle
        iteration_id, ok = QInputDialog.getText(self, "Iteration ID", "New iteration id:", text="iter_002")
        if not ok or not iteration_id.strip():
            return
        task_text, _ = QInputDialog.getMultiLineText(self, "Iteration Task", "Task:", "")
        source = iteration.iteration_id if iteration else None
        prompt = [
            f"Create a new iteration for project {project.project_id}, analysis {analysis.analysis_id}.",
            f"New iteration id: {iteration_id.strip()}",
            f"Create it under: projects/{project.project_id}/analyses/{analysis.analysis_id}/iterations/{iteration_id.strip()}/",
            "Include these required entries:",
            "- code/",
            "- output/figures/",
            "- output/videos/",
            "- output/processed_data/",
            "- output/stats/",
            "- logs/",
            "- task.md",
            "- notes.md",
            "- iteration.yaml",
            "Do not copy raw data into the iteration folder.",
            "If you use a source iteration, copy only the code, processed_data, stats, task.md, and notes.md as appropriate.",
        ]
        if source:
            prompt.append(f"Created from iteration: {source}")
        if task_text.strip():
            prompt.append("Task text:")
            prompt.append(task_text.strip())
        prompt.append("After creating the files, update notes and metadata accordingly.")
        ok = self._ensure_session_terminal_for_selection()
        if not ok:
            choice = QMessageBox.question(self, "No Active CLI", "No Codex session is running. Launch the current analysis session first?")
            if choice == QMessageBox.Yes:
                self._resume_or_launch_current_session()
                ok = self._ensure_session_terminal_for_selection()
        if not ok:
            return
        session = self._target_session_for_selection(self.current_selection)
        if session is None:
            return
        tmux_send_text(self._tmux_session_name(session), "\n".join(prompt))
        self.content_tabs.setCurrentWidget(self.cli_tab)
        self._refresh_cli_status_panel()

    def _resume_or_launch_current_session(self) -> None:
        session = self._target_session_for_selection(self.current_selection)
        if session is not None and session.identity_status == "verified":
            if self._ensure_session_terminal(session):
                self._remember_session_for_current_iteration(session.session_id)
                self._refresh_cli_status_panel()
            return
        self._launch_new_session()

    def _delete_selected(self) -> None:
        selection = self.current_selection
        if selection.experiment_group and selection.project:
            self._delete_selected_experiment_group()
            return
        if selection.iteration and selection.analysis:
            if QMessageBox.question(self, "Delete Iteration", f"Delete {selection.iteration.iteration_id}?") == QMessageBox.Yes:
                delete_iteration(selection.analysis, selection.iteration)
                self.reload_workspace()
            return
        if selection.analysis and selection.project:
            if QMessageBox.question(self, "Delete Analysis", f"Delete {selection.analysis.analysis_id} and all its iterations?") == QMessageBox.Yes:
                delete_analysis(selection.project, selection.analysis)
                self.current_selection = Selection(kind="project", project=selection.project)
                self.reload_workspace()
            return
        QMessageBox.information(self, "Delete", "Select an analysis or iteration to delete.")

    def _copy_selected_analysis(self) -> None:
        if self.current_selection.analysis is None or self.current_selection.project is None:
            QMessageBox.information(self, "Copy Analysis", "Select an analysis first.")
            return
        label, ok = QInputDialog.getText(self, "Copy Analysis", "New analysis name:", text="")
        if not ok or not label.strip():
            return
        analysis = copy_analysis(self.current_selection.project, self.current_selection.analysis, label.strip(), label.strip())
        self.current_selection = Selection(kind="analysis", project=self.current_selection.project, analysis=analysis)
        self.reload_workspace()
        self._ensure_associated_session_for_selection(show_errors=True, switch_to_cli=False, copy_command=False)

    def _move_selected_analysis(self, direction: int) -> None:
        if self.current_selection.analysis is None or self.current_selection.project is None:
            return
        move_analysis(self.current_selection.project, self.current_selection.analysis.analysis_id, direction)
        self.reload_workspace()

    def _show_tmux_sessions(self) -> None:
        if self._tmux_dialog is None:
            self._tmux_dialog = TmuxSessionsDialog(self)
        self._tmux_dialog.reload()
        self._tmux_dialog.show()
        self._tmux_dialog.raise_()
        self._tmux_dialog.activateWindow()

    def _current_session_link(self) -> SessionLink | None:
        selection = self.current_selection
        if selection.session is not None:
            return selection.session
        if selection.analysis:
            for session in selection.analysis.linked_codex_sessions:
                if session.session_id == selection.analysis.current_session:
                    return session
            return selection.analysis.linked_codex_sessions[0] if selection.analysis.linked_codex_sessions else None
        return None

    def _project_session_link(self, project: Project) -> SessionLink:
        return session_link(
            project.main_codex_session or f"{project.project_id}_supervisor",
            "Project session",
            codex_id=project.main_codex_id,
            codex_thread_name=project.main_codex_thread_name,
            identity_status=project.main_codex_identity_status,
        )

    def _target_session_for_selection(self, selection: Selection) -> SessionLink | None:
        if selection.session is not None:
            return selection.session
        if selection.analysis is None:
            return None
        preferred_alias = None
        if selection.iteration is not None and selection.iteration.codex_session:
            preferred_alias = selection.iteration.codex_session
        elif selection.analysis.current_session:
            preferred_alias = selection.analysis.current_session
        if preferred_alias:
            session = self._session_by_alias(selection.analysis, preferred_alias)
            if session is not None:
                return session
        return self._current_session_link()

    def _session_by_alias(self, analysis: Analysis, alias: str) -> SessionLink | None:
        for session in analysis.linked_codex_sessions:
            if session.session_id == alias:
                return session
        return None

    def _capture_identity_with_events(
        self,
        index_snapshot: set[str],
        db_snapshot: set[str],
        repo_root: Path,
        first_user_message: str,
        *,
        tmux_name: str,
        launched_at: int,
        progress: QProgressDialog | None = None,
    ) -> object | None:
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            elapsed = 90.0 - max(deadline - time.monotonic(), 0.0)
            trust_prompt_seen = self._auto_accept_trust_prompt(tmux_name)
            if progress is not None:
                if trust_prompt_seen:
                    progress.setLabelText("Accepting trust prompt and waiting for Codex startup...")
                elif elapsed < 4.0:
                    progress.setLabelText("Starting Codex session...")
                elif elapsed < 45.0:
                    progress.setLabelText("Waiting for trust confirmation or session startup...")
                else:
                    progress.setLabelText("Verifying Codex session...")
            QApplication.processEvents()
            identity = capture_new_codex_session(index_snapshot, timeout_seconds=0.0)
            if identity is not None:
                return identity
            identity = capture_new_codex_session_from_db(
                db_snapshot,
                cwd=repo_root,
                first_user_message=first_user_message,
                min_created_at=launched_at,
                timeout_seconds=0.0,
            )
            if identity is not None:
                return identity
            time.sleep(0.1)
        if progress is not None:
            progress.setLabelText("Finalizing session detection...")
            QApplication.processEvents()
        return capture_new_codex_session_from_db(
            db_snapshot,
            cwd=repo_root,
            min_created_at=launched_at,
            timeout_seconds=0.0,
        )

    def _selection_bundle_for_analysis(self) -> tuple[Project, Analysis, Iteration | None] | None:
        selection = self.current_selection
        if selection.project and selection.analysis:
            return selection.project, selection.analysis, selection.iteration
        return None

    def _selected_project(self) -> Project | None:
        item = self.project_list.currentItem()
        if item is None:
            return self.current_selection.project
        return self._project_lookup.get(id(item), self.current_selection.project)

    def _selection_key(self, selection: Selection) -> tuple[str, str | None, str | None, str | None]:
        return (
            selection.kind,
            selection.project.project_id if selection.project else None,
            selection.analysis.analysis_id if selection.analysis else selection.experiment_group.name if selection.experiment_group else None,
            selection.iteration.iteration_id
            if selection.iteration
            else selection.session.session_id if selection.session
            else selection.experiment_entry.exp_id if selection.experiment_entry
            else None,
        )

    def _available_user_ids(self) -> list[str]:
        users = sorted(
            {
                entry.pw_name
                for entry in pwd.getpwall()
                if entry.pw_uid >= 1000 and "nologin" not in entry.pw_shell and "false" not in entry.pw_shell
            }
        )
        if "adamranson" in users:
            users.insert(0, users.pop(users.index("adamranson")))
        return users or [""]

    def _ensure_session_terminal(self, session: SessionLink) -> bool:
        tmux_name = self._tmux_session_name(session)
        self._recover_session_identity_for_selection(session)
        if not tmux_has_session(tmux_name):
            if session.identity_status != "verified" or not (session.codex_id or session.codex_thread_name):
                return False
            spec = build_resume_session_spec(session, self._session_workdir(self.current_selection))
            tmux_create_session(tmux_name, spec.command, spec.workdir)
            self._auto_accept_trust_prompt(tmux_name, allow_retry=False)
        return True

    def _ensure_session_terminal_for_selection(self) -> bool:
        session = self._target_session_for_selection(self.current_selection)
        if session is None:
            return False
        ok = self._ensure_session_terminal(session)
        if ok:
            self._remember_session_for_current_iteration(session.session_id)
        return ok

    def _ensure_associated_session_for_selection(
        self,
        *,
        show_errors: bool = False,
        switch_to_cli: bool = False,
        copy_command: bool = False,
    ) -> bool:
        bundle = self._selection_bundle_for_analysis()
        if bundle is None:
            return False
        project, analysis, iteration = bundle
        session = self._target_session_for_selection(self.current_selection)
        if session is None:
            session_id = analysis.current_session or f"{project.project_id}_{analysis.analysis_id}_main"
            session = session_link(session_id, "Main session")
            upsert_session_link(analysis, session, make_current=True)
            if iteration is not None:
                self._set_iteration_session(iteration, session_id)
        else:
            self._recover_session_identity_for_selection(session)
        if tmux_has_session(self._tmux_session_name(session)):
            self._remember_session_for_current_iteration(session.session_id)
            if switch_to_cli:
                self.content_tabs.setCurrentWidget(self.cli_tab)
            if copy_command:
                self._copy_current_session_command()
            self._refresh_cli_status_panel()
            return True
        if self._ensure_session_terminal(session):
            self._remember_session_for_current_iteration(session.session_id)
            if switch_to_cli:
                self.content_tabs.setCurrentWidget(self.cli_tab)
            if copy_command:
                self._copy_current_session_command()
            self._refresh_cli_status_panel()
            self.reload_workspace()
            return True
        self._launch_session_for_bundle(
            project,
            analysis,
            iteration,
            session.session_id,
            session.label or "Main session",
            make_current=True,
            switch_to_cli=switch_to_cli,
            copy_command=copy_command,
        )
        return True

    def _recover_session_identity_for_selection(self, session: SessionLink) -> bool:
        if session.identity_status == "verified" and (session.codex_id or session.codex_thread_name):
            return True
        bundle = self._selection_bundle_for_analysis()
        if bundle is None:
            return False
        project, analysis, iteration = bundle
        bootstrap_text = ""
        bootstrap_path = bootstrap_log_path(session.session_id)
        if bootstrap_path.exists():
            bootstrap_text = bootstrap_path.read_text(encoding="utf-8", errors="replace")
        identity = resolve_codex_session_from_db(
            cwd=self._session_workdir(self.current_selection),
            first_user_message=bootstrap_text or None,
        )
        if identity is None:
            return False
        link = self._session_by_alias(analysis, session.session_id)
        if link is None:
            return False
        mark_launch_record(link, verified=identity)
        upsert_session_link(analysis, link, make_current=analysis.current_session == link.session_id)
        if link.session_id == project.main_codex_session:
            project.main_codex_id = identity.codex_id
            project.main_codex_thread_name = identity.codex_thread_name
            project.main_codex_identity_status = "verified"
            save_project(project)
        if self.current_selection.iteration is not None and self.current_selection.iteration.codex_session != link.session_id:
            self._set_iteration_session(self.current_selection.iteration, link.session_id)
        self.current_selection = Selection(
            kind=self.current_selection.kind,
            project=project,
            analysis=analysis,
            iteration=iteration,
            session=link if self.current_selection.session is not None else None,
            experiment_group=self.current_selection.experiment_group,
            experiment_entry=self.current_selection.experiment_entry,
        )
        return True

    def _session_workdir(self, selection: Selection) -> Path:
        if selection.iteration is not None:
            return selection.iteration.path
        if selection.analysis is not None:
            iterations = load_iterations(selection.project, selection.analysis) if selection.project is not None else []
            if iterations:
                return iterations[0].path
            return selection.analysis.path
        if selection.project is not None:
            return selection.project.path
        return APP_ROOT

    def _tmux_session_name(self, session: SessionLink) -> str:
        return session.tmux_session_name or session.session_id

    def _content_tab_changed(self, _index: int) -> None:
        if self.content_tabs.currentWidget() is self.cli_tab:
            self._asset_refresh_timer.stop()
            self._refresh_cli_status_panel()
        else:
            self._refresh_current_asset_tab()
            if self._current_tab_label() in {"Figures", "Videos", "Processed Data", "Stats", "Code"}:
                self._asset_refresh_timer.start()
            else:
                self._asset_refresh_timer.stop()

    def _set_iteration_session(self, iteration: Iteration, session_id: str) -> None:
        iteration.codex_session = session_id
        save_iteration(iteration)

    def _remember_session_for_current_iteration(self, session_id: str) -> None:
        if self.current_selection.iteration is not None and self.current_selection.iteration.codex_session != session_id:
            self._set_iteration_session(self.current_selection.iteration, session_id)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._asset_refresh_timer.stop()
        self._verification_timer.stop()
        super().closeEvent(event)

    def _current_tab_label(self) -> str | None:
        index = self.content_tabs.currentIndex()
        if index < 0:
            return None
        return self.content_tabs.tabText(index)

    def _refresh_current_asset_tab(self) -> None:
        if self._current_tab_label() not in {"Figures", "Videos", "Processed Data", "Stats", "Code"}:
            return
        widget = self.content_tabs.currentWidget()
        refresh = getattr(widget, "_refresh_callback", None)
        if callable(refresh):
            refresh()

    def _refresh_cli_status_panel(self) -> None:
        session = self._target_session_for_selection(self.current_selection)
        if session is None:
            self.cli_status.setPlainText("No linked Codex session for the current selection.")
            self.cli_attach_button.setEnabled(False)
            return
        tmux_name = self._tmux_session_name(session)
        tmux_running = tmux_has_session(tmux_name)
        attach_command = (
            f"ssh -t dream \"cd /home/adamranson/code/codex_herder && "
            f"./bin/codex-herder-tmux attach -t '{tmux_name}'\""
        )
        lines = [
            f"Session: {session.session_id}",
            f"Codex identity: {session.codex_id or session.codex_thread_name or 'unverified'}",
            f"Identity status: {session.identity_status}",
            f"Tmux session: {tmux_name}",
            f"Tmux running: {'yes' if tmux_running else 'no'}",
            f"Verification pending: {'yes' if session.session_id in self._pending_session_verifications else 'no'}",
            "",
            "Use the button below to copy the external attach command.",
            "",
            attach_command,
        ]
        self.cli_status.setPlainText("\n".join(lines))
        self.cli_attach_button.setEnabled(True)

    def _start_pending_session_verification(
        self,
        *,
        project: Project,
        analysis: Analysis,
        session_id: str,
        tmux_name: str,
        index_snapshot: set[str],
        db_snapshot: set[str],
        repo_root: Path,
        first_user_message: str,
        launched_at: int,
        make_current: bool,
    ) -> None:
        self._pending_session_verifications[session_id] = {
            "project": project,
            "analysis_id": analysis.analysis_id,
            "tmux_name": tmux_name,
            "index_snapshot": index_snapshot,
            "db_snapshot": db_snapshot,
            "repo_root": repo_root,
            "first_user_message": first_user_message,
            "launched_at": launched_at,
            "make_current": make_current,
            "deadline": time.monotonic() + 120.0,
        }
        if not self._verification_timer.isActive():
            self._verification_timer.start()

    def _poll_pending_session_verifications(self) -> None:
        if not self._pending_session_verifications:
            self._verification_timer.stop()
            return
        completed: list[str] = []
        for session_id, state in list(self._pending_session_verifications.items()):
            tmux_name = str(state["tmux_name"])
            self._auto_accept_trust_prompt(tmux_name)
            identity = capture_new_codex_session(state["index_snapshot"], timeout_seconds=0.0)
            if identity is None:
                identity = capture_new_codex_session_from_db(
                    state["db_snapshot"],
                    cwd=state["repo_root"],
                    first_user_message=state["first_user_message"],
                    min_created_at=int(state["launched_at"]),
                    timeout_seconds=0.0,
                )
            project = state["project"]
            analysis = next((item for item in load_analyses(project) if item.analysis_id == state["analysis_id"]), None)
            if identity is not None and analysis is not None:
                link = self._session_by_alias(analysis, session_id)
                if link is not None:
                    mark_launch_record(link, verified=identity)
                    upsert_session_link(analysis, link, make_current=bool(state["make_current"]) or analysis.current_session is None)
                if session_id == project.main_codex_session:
                    project.main_codex_id = identity.codex_id
                    project.main_codex_thread_name = identity.codex_thread_name
                    project.main_codex_identity_status = "verified"
                    save_project(project)
                completed.append(session_id)
                continue
            if time.monotonic() >= float(state["deadline"]):
                if analysis is not None:
                    link = self._session_by_alias(analysis, session_id)
                    if link is not None:
                        mark_launch_record(link, capture_failed=True)
                        upsert_session_link(analysis, link, make_current=False)
                completed.append(session_id)
        for session_id in completed:
            self._pending_session_verifications.pop(session_id, None)
        self.reload_workspace()

    def _auto_accept_trust_prompt(self, tmux_name: str, *, allow_retry: bool = True) -> bool:
        pane = tmux_capture_pane(tmux_name, lines=120, include_escape=False)
        lower = pane.lower()
        trust_prompt = "trust" in lower and any(token in lower for token in ("folder", "directory", "repo", "repository"))
        if not trust_prompt:
            return False
        now = time.monotonic()
        last = self._trust_auto_sent_at.get(tmux_name, 0.0)
        if last and (not allow_retry or now - last < 3.0):
            return True
        tmux_send_text(tmux_name, "1")
        self._trust_auto_sent_at[tmux_name] = now
        return True

    def _open_figure_window(self, path: Path) -> None:
        window = FigureWindow(path)
        window.showFullScreen()
        window.show()
        self._figure_windows.append(window)
        window.destroyed.connect(lambda *_: self._figure_windows.remove(window) if window in self._figure_windows else None)

    def _open_video_window(self, path: Path) -> None:
        if QT_VIDEO_AVAILABLE:
            window = VideoWindow(path)
            window.showFullScreen()
            window.show()
            self._video_windows.append(window)
            window.destroyed.connect(lambda *_: self._video_windows.remove(window) if window in self._video_windows else None)
            return
        if QUrl is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

def run() -> int:
    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "xcb" if os.environ.get("DISPLAY") else "offscreen"
    app = QApplication.instance() or QApplication([])
    window = CodexHerderApp()
    window.show()
    return app.exec()
