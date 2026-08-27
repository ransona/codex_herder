from __future__ import annotations

import argparse
import json
import os
import selectors
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .storage import STATE_ROOT


DEFAULT_DB_PATH = STATE_ROOT / "codex_usage.sqlite3"
SAMPLE_INTERVAL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class UsageSample:
    sampled_at: int
    account_key: str
    email: str | None
    plan_type: str | None
    auth_type: str | None
    limit_id: str | None
    primary_used_percent: float | None
    primary_window_minutes: int | None
    primary_resets_at: int | None
    secondary_used_percent: float | None
    secondary_window_minutes: int | None
    secondary_resets_at: int | None
    credits_balance: float | None
    credits_available: bool | None
    credits_unlimited: bool | None
    rate_limit_reached_type: str | None
    raw_json: str


class UsageDatabase:
    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sampled_at INTEGER NOT NULL,
                account_key TEXT NOT NULL,
                email TEXT,
                plan_type TEXT,
                auth_type TEXT,
                limit_id TEXT,
                primary_used_percent REAL,
                primary_window_minutes INTEGER,
                primary_resets_at INTEGER,
                secondary_used_percent REAL,
                secondary_window_minutes INTEGER,
                secondary_resets_at INTEGER,
                credits_balance REAL,
                credits_available INTEGER,
                credits_unlimited INTEGER,
                rate_limit_reached_type TEXT,
                raw_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_samples_time ON usage_samples(sampled_at)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_samples_account ON usage_samples(account_key, sampled_at)"
        )
        self._connection.commit()

    def insert(self, sample: UsageSample) -> None:
        values = (
            sample.sampled_at,
            sample.account_key,
            sample.email,
            sample.plan_type,
            sample.auth_type,
            sample.limit_id,
            sample.primary_used_percent,
            sample.primary_window_minutes,
            sample.primary_resets_at,
            sample.secondary_used_percent,
            sample.secondary_window_minutes,
            sample.secondary_resets_at,
            sample.credits_balance,
            None if sample.credits_available is None else int(sample.credits_available),
            None if sample.credits_unlimited is None else int(sample.credits_unlimited),
            sample.rate_limit_reached_type,
            sample.raw_json,
        )
        self._connection.execute(
            """
            INSERT INTO usage_samples (
                sampled_at, account_key, email, plan_type, auth_type, limit_id,
                primary_used_percent, primary_window_minutes, primary_resets_at,
                secondary_used_percent, secondary_window_minutes, secondary_resets_at,
                credits_balance, credits_available, credits_unlimited,
                rate_limit_reached_type, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        self._connection.commit()

    def latest(self, account_key: str | None = None) -> sqlite3.Row | None:
        if account_key:
            return self._connection.execute(
                "SELECT * FROM usage_samples WHERE account_key = ? ORDER BY sampled_at DESC LIMIT 1",
                (account_key,),
            ).fetchone()
        return self._connection.execute("SELECT * FROM usage_samples ORDER BY sampled_at DESC LIMIT 1").fetchone()

    def samples_since(self, since: int, account_key: str | None = None) -> list[sqlite3.Row]:
        if account_key:
            rows = self._connection.execute(
                "SELECT * FROM usage_samples WHERE sampled_at >= ? AND account_key = ? ORDER BY sampled_at",
                (since, account_key),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM usage_samples WHERE sampled_at >= ? ORDER BY sampled_at", (since,)
            ).fetchall()
        return list(rows)

    def accounts(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT account_key FROM usage_samples GROUP BY account_key ORDER BY account_key"
        ).fetchall()
        return [str(row["account_key"]) for row in rows]

    def close(self) -> None:
        self._connection.close()


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _request_codex_app_server() -> dict[str, Any]:
    """Read account and rate-limit state through the installed Codex CLI."""
    process = subprocess.Popen(
        [os.environ.get("CODEX_HERDER_CODEX_BIN", "codex"), "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    requests = [
        {"method": "initialize", "id": 1, "params": {"clientInfo": {
            "name": "codex_herder_usage", "title": "Codex Herder Usage", "version": "0.1.0"
        }}},
        {"method": "initialized", "params": {}},
        {"method": "account/read", "id": 2, "params": {"refreshToken": False}},
        {"method": "account/rateLimits/read", "id": 3, "params": {}},
    ]
    assert process.stdin is not None
    for request in requests:
        process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()

    responses: dict[int, dict[str, Any]] = {}
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 20
    while len(responses) < 3 and time.monotonic() < deadline:
        events = selector.select(max(0.1, deadline - time.monotonic()))
        if not events:
            break
        line = process.stdout.readline()
        if not line:
            break
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message.get("id"), int) and message["id"] in (1, 2, 3):
            responses[message["id"]] = message
    process.kill()
    process.wait(timeout=3)
    if 2 not in responses or 3 not in responses:
        stderr = process.stderr.read().strip() if process.stderr else ""
        raise RuntimeError(stderr or "Codex app-server did not return account and rate-limit data")
    for request_id in (2, 3):
        if "error" in responses[request_id]:
            raise RuntimeError(str(responses[request_id]["error"]))
    return {"account": responses[2]["result"], "rate_limits": responses[3]["result"]}


def collect_sample() -> UsageSample:
    payload = _request_codex_app_server()
    account = payload["account"].get("account") or {}
    limits_result = payload["rate_limits"]
    limits = limits_result.get("rateLimits") or {}
    account_key = str(account.get("email") or account.get("id") or account.get("type") or "unknown")
    primary = limits.get("primary") or {}
    secondary = limits.get("secondary") or {}
    credits = limits.get("credits") or {}
    return UsageSample(
        sampled_at=int(time.time()),
        account_key=account_key,
        email=account.get("email"),
        plan_type=account.get("planType") or limits.get("planType"),
        auth_type=account.get("type"),
        limit_id=limits.get("limitId"),
        primary_used_percent=_number(primary.get("usedPercent")),
        primary_window_minutes=_integer(primary.get("windowDurationMins")),
        primary_resets_at=_integer(primary.get("resetsAt")),
        secondary_used_percent=_number(secondary.get("usedPercent")),
        secondary_window_minutes=_integer(secondary.get("windowDurationMins")),
        secondary_resets_at=_integer(secondary.get("resetsAt")),
        credits_balance=_number(credits.get("balance")),
        credits_available=credits.get("hasCredits"),
        credits_unlimited=credits.get("unlimited"),
        rate_limit_reached_type=limits.get("rateLimitReachedType"),
        raw_json=json.dumps(payload, sort_keys=True),
    )


class RemainingPieChart(QWidget):
    def __init__(self, title: str, color: str) -> None:
        super().__init__()
        self.title = title
        self.color = QColor(color)
        self.used_percent: float | None = None
        self.setMinimumSize(190, 190)

    def set_used_percent(self, value: Any) -> None:
        self.used_percent = _number(value)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        painter.setPen(QPen(self.palette().mid(), 1))
        title = f"{self.title} ({100.0 - self.used_percent:.1f}%)" if self.used_percent is not None else f"{self.title} (—)"
        painter.drawText(self.rect().adjusted(0, 6, 0, 0), Qt.AlignTop | Qt.AlignHCenter, title)
        diameter = min(self.width(), self.height()) - 55
        x = (self.width() - diameter) // 2
        y = 30 + (self.height() - 30 - diameter) // 2
        remaining = 100.0 if self.used_percent is None else max(0.0, min(100.0, 100.0 - self.used_percent))
        painter.setBrush(self.palette().mid())
        painter.drawEllipse(x, y, diameter, diameter)
        painter.setBrush(self.color)
        painter.drawPie(x, y, diameter, diameter, 90 * 16, int(-remaining * 3.6 * 16))
        painter.setPen(self.palette().text())
        painter.drawText(x, y + diameter // 2 - 12, diameter, 24, Qt.AlignCenter, f"{remaining:.1f}%")
        painter.drawText(x, y + diameter + 4, diameter, 20, Qt.AlignCenter, "remaining")


class SamplerSignals(QObject):
    sample_ready = Signal(object)
    error = Signal(str)


class SamplerTask(QRunnable):
    def __init__(self) -> None:
        super().__init__()
        self.signals = SamplerSignals()

    def run(self) -> None:
        try:
            self.signals.sample_ready.emit(collect_sample())
        except Exception as exc:
            self.signals.error.emit(str(exc))


class UsageLineChart(QWidget):
    def __init__(self, title: str, window_seconds: int, grid_seconds: int, axis_unit: str) -> None:
        super().__init__()
        self.rows: list[sqlite3.Row] = []
        self.title = title
        self.window_seconds = window_seconds
        self.grid_seconds = grid_seconds
        self.axis_unit = axis_unit
        self.setMinimumHeight(260)

    def set_rows(self, rows: list[sqlite3.Row]) -> None:
        self.rows = rows
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.palette().base())
        left, top, right, bottom = 55, 42, 20, 35
        plot = self.rect().adjusted(left, top, -right, -bottom)
        painter.setPen(QPen(self.palette().text(), 1))
        painter.drawText(plot.left(), 5, plot.width(), 18, Qt.AlignCenter, self.title)
        if not self.rows:
            painter.drawText(plot, Qt.AlignCenter, "No samples for this range")
            return
        painter.setPen(QPen(self.palette().mid(), 1))
        for tick in range(0, 101, 25):
            y = plot.bottom() - (plot.height() * tick / 100)
            painter.drawLine(plot.left(), int(y), plot.right(), int(y))
            painter.drawText(2, int(y) - 8, 45, 16, Qt.AlignRight, f"{tick}%")
        end_time = time.time()
        start_time = end_time - self.window_seconds
        painter.setPen(QPen(self.palette().mid(), 1, Qt.DotLine))
        tick_time = end_time - (end_time % self.grid_seconds)
        while tick_time >= start_time:
            fraction = (tick_time - start_time) / self.window_seconds
            x = int(plot.left() + plot.width() * fraction)
            painter.drawLine(x, plot.top(), x, plot.bottom())
            elapsed = max(0, int(round((end_time - tick_time) / self.grid_seconds)))
            label = "now" if elapsed == 0 else f"-{elapsed * self.grid_seconds // (3600 if self.axis_unit == 'h' else 86400)}{self.axis_unit}"
            painter.drawText(x - 25, self.height() - 10, 50, 16, Qt.AlignCenter, label)
            tick_time -= self.grid_seconds
        self._line(painter, plot, [(row["sampled_at"], row["primary_used_percent"]) for row in self.rows], "#2f80ed", start_time, end_time)
        self._line(painter, plot, [(row["sampled_at"], row["secondary_used_percent"]) for row in self.rows], "#eb5757", start_time, end_time)

    @staticmethod
    def _line(painter: QPainter, plot, values: list[tuple[int, Any]], color: str, start_time: float, end_time: float) -> None:
        points = [(timestamp, value) for timestamp, value in values if value is not None]
        if not points:
            return
        dense = len(points) > 1000
        if dense:
            stride = max(1, len(points) // 1000)
            points = points[::stride]
            last_point = next((point for point in reversed(values) if point[1] is not None), None)
            if last_point is not None and points[-1][0] != last_point[0]:
                points.append(last_point)
        painter.setPen(QPen(QColor(color), 2))
        previous = None
        for timestamp, value in points:
            x = plot.left() + plot.width() * (timestamp - start_time) / (end_time - start_time)
            y = plot.bottom() - plot.height() * max(0, min(100, float(value))) / 100
            current = (int(x), int(y))
            if previous is not None:
                painter.drawLine(previous[0], previous[1], current[0], current[1])
            if not dense:
                painter.drawEllipse(current[0] - 2, current[1] - 2, 4, 4)
            previous = current


class UsageWindow(QMainWindow):
    def __init__(self, database: UsageDatabase) -> None:
        super().__init__()
        self.database = database
        self.setWindowTitle("Codex Credit Usage")
        self.resize(1000, 700)
        self.account_label = QLabel("No accounts sampled")
        self.account_label.setAlignment(Qt.AlignCenter)
        self.account_keys: list[str] = []
        self.selected_account: str | None = None
        self.previous_account_button = QPushButton("◀")
        self.next_account_button = QPushButton("▶")
        for button in (self.previous_account_button, self.next_account_button):
            button.setFixedWidth(36)
        self.primary_pie = RemainingPieChart("5-hour window", "#2f80ed")
        self.secondary_pie = RemainingPieChart("7-day window", "#eb5757")
        self.hour_chart = UsageLineChart("Past 5 hours", 5 * 3600, 3600, "h")
        self.day_chart = UsageLineChart("Past 24 hours", 24 * 3600, 5 * 3600, "h")
        self.week_chart = UsageLineChart("Past 7 days", 7 * 86400, 86400, "d")
        self.status_label = QLabel(f"Database: {database.path}")
        self.thread_pool = QThreadPool(self)
        self.sampling_in_progress = False
        self.active_task: SamplerTask | None = None
        self._build_ui()
        self.previous_account_button.clicked.connect(lambda: self._move_account(-1))
        self.next_account_button.clicked.connect(lambda: self._move_account(1))
        self.refresh_view()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.start_sampling)
        self.timer.start(SAMPLE_INTERVAL_SECONDS * 1000)
        QTimer.singleShot(0, self.start_sampling)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        account_box = QHBoxLayout()
        account_box.addWidget(QLabel("Account:"))
        account_box.addWidget(self.previous_account_button)
        account_box.addWidget(self.account_label, 1)
        account_box.addWidget(self.next_account_button)
        layout.addLayout(account_box)
        charts = QHBoxLayout()
        pies = QVBoxLayout()
        pies.addWidget(self.primary_pie, 1)
        pies.addWidget(self.secondary_pie, 1)
        charts.addLayout(pies, 1)
        lines = QVBoxLayout()
        lines.addWidget(self.hour_chart, 1)
        lines.addWidget(self.day_chart, 1)
        lines.addWidget(self.week_chart, 1)
        charts.addLayout(lines, 3)
        layout.addLayout(charts, 1)
        layout.addWidget(self.status_label)
        self.setCentralWidget(root)

    def start_sampling(self) -> None:
        if self.sampling_in_progress:
            return
        self.sampling_in_progress = True
        self.status_label.setText("Sampling Codex usage…")
        task = SamplerTask()
        task.signals.sample_ready.connect(self._sample_succeeded)
        task.signals.error.connect(self._sample_failed)
        self.active_task = task
        self.thread_pool.start(task)

    def _sample_succeeded(self, sample: UsageSample) -> None:
        self.sampling_in_progress = False
        self.active_task = None
        self.database.insert(sample)
        self.selected_account = sample.account_key
        self.status_label.setText(f"Last sample saved; database: {self.database.path}")
        self.refresh_view()

    def _sample_failed(self, message: str) -> None:
        self.sampling_in_progress = False
        self.active_task = None
        self.status_label.setText(f"Sampling failed: {message}")

    def _move_account(self, direction: int) -> None:
        if not self.account_keys:
            return
        current_index = self.account_keys.index(self.selected_account) if self.selected_account in self.account_keys else 0
        self.selected_account = self.account_keys[(current_index + direction) % len(self.account_keys)]
        self.refresh_view()

    def refresh_view(self) -> None:
        self.account_keys = self.database.accounts()
        latest = self.database.latest()
        if latest is not None:
            if self.selected_account not in self.account_keys:
                self.selected_account = str(latest["account_key"])
        if self.selected_account is None:
            self.account_label.setText("No accounts sampled")
        else:
            selected_latest = self.database.latest(self.selected_account)
            account_name = (selected_latest["email"] if selected_latest else None) or self.selected_account
            plan = (selected_latest["plan_type"] if selected_latest else None) or "unknown plan"
            self.account_label.setText(f"{account_name} ({plan})")
        self.previous_account_button.setEnabled(len(self.account_keys) > 1)
        self.next_account_button.setEnabled(len(self.account_keys) > 1)
        now = int(time.time())
        selected_latest = self.database.latest(self.selected_account) if self.selected_account else None
        if selected_latest is not None:
            self.primary_pie.set_used_percent(selected_latest["primary_used_percent"])
            self.secondary_pie.set_used_percent(selected_latest["secondary_used_percent"])
        else:
            self.primary_pie.set_used_percent(None)
            self.secondary_pie.set_used_percent(None)
        hour_rows = self.database.samples_since(now - 5 * 3600, self.selected_account)
        day_rows = self.database.samples_since(now - 24 * 3600, self.selected_account)
        week_rows = self.database.samples_since(now - 7 * 86400, self.selected_account)
        self.hour_chart.set_rows(hour_rows)
        self.week_chart.set_rows(week_rows)
        self.day_chart.set_rows(day_rows)


def run_usage_tracker(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample and display Codex account credit availability.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    parser.add_argument("--once", action="store_true", help="Take one sample and exit")
    args = parser.parse_args(argv)
    database = UsageDatabase(args.db)
    if args.once:
        try:
            sample = collect_sample()
            database.insert(sample)
            print(json.dumps({"account": sample.account_key, "sampled_at": sample.sampled_at,
                              "primary_used_percent": sample.primary_used_percent,
                              "secondary_used_percent": sample.secondary_used_percent,
                              "credits_balance": sample.credits_balance}, indent=2))
            return 0
        except Exception as exc:
            print(f"Unable to sample Codex usage: {exc}", file=sys.stderr)
            return 1
        finally:
            database.close()
    app = QApplication.instance() or QApplication(sys.argv)
    window = UsageWindow(database)
    window.show()
    result = app.exec()
    database.close()
    return result


if __name__ == "__main__":
    raise SystemExit(run_usage_tracker())
