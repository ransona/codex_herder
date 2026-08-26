from __future__ import annotations

import os
import pty
import signal
import struct
import subprocess
from pathlib import Path

import pyte
from PySide6.QtCore import QSocketNotifier, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QKeyEvent, QTextCharFormat, QTextCursor, QTextOption
from PySide6.QtWidgets import QApplication, QTextEdit

from .sessions import SessionLaunchSpec

try:
    import fcntl
    import termios
except ImportError:  # pragma: no cover
    fcntl = None
    termios = None


class TerminalPane(QTextEdit):
    status_changed = Signal(str)

    DEFAULT_BG = "#111111"
    DEFAULT_FG = "#f2f2f2"
    ANSI_COLORS = {
        "black": "#3b4252",
        "red": "#bf616a",
        "green": "#a3be8c",
        "brown": "#ebcb8b",
        "blue": "#81a1c1",
        "magenta": "#b48ead",
        "cyan": "#88c0d0",
        "white": "#e5e9f0",
        "brightblack": "#4c566a",
        "brightred": "#bf616a",
        "brightgreen": "#a3be8c",
        "brightbrown": "#ebcb8b",
        "brightblue": "#81a1c1",
        "brightmagenta": "#b48ead",
        "brightcyan": "#8fbcbb",
        "brightwhite": "#eceff4",
    }

    def __init__(self) -> None:
        super().__init__()
        self.master_fd: int | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.current_log_path: Path | None = None
        self._socket_notifier: QSocketNotifier | None = None
        self._dirty = False
        self._closed_message = ""
        self._format_cache: dict[tuple[str, str, bool, bool, bool, bool], QTextCharFormat] = {}
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(33)
        self._refresh_timer.timeout.connect(self._render_if_dirty)
        self._refresh_timer.start()

        self.setReadOnly(False)
        self.setLineWrapMode(QTextEdit.NoWrap)
        self.setUndoRedoEnabled(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.StrongFocus)
        font = QFont("Monospace")
        font.setStyleHint(QFont.Monospace)
        font.setFixedPitch(True)
        self.setFont(font)
        self.document().setDocumentMargin(0)
        option = QTextOption()
        option.setWrapMode(QTextOption.NoWrap)
        self.document().setDefaultTextOption(option)
        self.setFrameStyle(0)
        self.setStyleSheet(
            f"QTextEdit {{ background: {self.DEFAULT_BG}; color: {self.DEFAULT_FG}; padding: 0px; border: none; }}"
        )
        self._reset_screen()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._resize_pty()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        payload = self._encode_key_event(event)
        if payload is not None:
            self._write_pty(payload)
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        if source and source.hasText():
            self._write_pty(source.text().encode("utf-8"))

    def launch(self, spec: SessionLaunchSpec, workdir: Path | None = None) -> None:
        self.stop()
        self._reset_screen()
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.current_log_path = spec.log_path
        self.append_text(f"$ {' '.join(spec.command)}\n")
        launch_dir = workdir or spec.workdir
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        self.process = subprocess.Popen(
            spec.command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=launch_dir,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        self._socket_notifier = QSocketNotifier(master_fd, QSocketNotifier.Read, self)
        self._socket_notifier.activated.connect(self._read_pty)
        self._resize_pty()
        self.status_changed.emit(f"running {spec.session_id}")
        if spec.initial_input:
            QTimer.singleShot(50, lambda: self._send_initial_input(spec.initial_input))

    def stop(self) -> None:
        if self._socket_notifier is not None:
            self._socket_notifier.setEnabled(False)
            try:
                self._socket_notifier.activated.disconnect(self._read_pty)
            except (RuntimeError, TypeError):
                pass
            self._socket_notifier.deleteLater()
            self._socket_notifier = None
        if self.process is not None and self.process.poll() is None:
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
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        self.process = None
        self.status_changed.emit("idle")

    def clear(self) -> None:
        self._reset_screen()
        self._dirty = True

    def send_text(self, text: str) -> None:
        if self.master_fd is None:
            return
        payload = text if text.endswith("\n") else text + "\n"
        os.write(self.master_fd, payload.encode("utf-8"))

    def append_text(self, text: str) -> None:
        if self.stream is not None:
            self.stream.feed(text)
        self._dirty = True
        if self.current_log_path is not None:
            with self.current_log_path.open("a", encoding="utf-8") as handle:
                handle.write(text)

    def _send_initial_input(self, text: str) -> None:
        if self.master_fd is None:
            return
        os.write(self.master_fd, text.encode("utf-8"))

    def _reset_screen(self) -> None:
        rows, cols = self._terminal_size()
        self.screen = pyte.HistoryScreen(cols, rows, history=5000)
        self.stream = pyte.Stream(self.screen)
        self._closed_message = ""
        self._dirty = True

    def _terminal_size(self) -> tuple[int, int]:
        metrics = QFontMetricsF(self.font())
        char_width = max(metrics.horizontalAdvance("W"), metrics.horizontalAdvance("M"), metrics.horizontalAdvance(" "), 1.0)
        char_height = max(metrics.height(), 1.0)
        viewport = self.viewport().size()
        usable_width = max(0.0, float(viewport.width()) - 2.0)
        usable_height = max(0.0, float(viewport.height()) - 2.0)
        raw_cols = int(usable_width // char_width)
        raw_rows = int(usable_height // char_height)
        cols = max(20, raw_cols - 1)
        rows = max(8, raw_rows)
        return rows, cols

    def _resize_pty(self) -> None:
        if self.master_fd is None or fcntl is None or termios is None:
            return
        rows, cols = self._terminal_size()
        winsz = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsz)
        except OSError:
            return
        if self.screen is not None and (self.screen.lines != rows or self.screen.columns != cols):
            self.screen.resize(rows, cols)
            self._dirty = True

    def _read_pty(self, *_args) -> None:
        if self.master_fd is None:
            return
        try:
            data = os.read(self.master_fd, 65536)
        except OSError as exc:
            self._mark_closed(f"Lost terminal connection: {exc}")
            return
        if not data:
            self._mark_closed("Terminal process closed.")
            return
        self.append_text(data.decode("utf-8", errors="ignore"))

    def _render_if_dirty(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        if self._closed_message:
            self.setPlainText(self._closed_message)
            self.setExtraSelections([])
            return
        if self.screen is None:
            return
        document = self.document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        cursor.select(QTextCursor.Document)
        cursor.removeSelectedText()
        for row in range(self.screen.lines):
            line = self.screen.buffer.get(row, {})
            run_chars: list[str] = []
            run_format: QTextCharFormat | None = None
            for col in range(self.screen.columns):
                char = line.get(col)
                glyph = char.data if char is not None and char.data else " "
                char_format = self._format_for_char(char)
                if run_format is None or self._formats_match(run_format, char_format):
                    run_chars.append(glyph)
                    run_format = char_format
                else:
                    cursor.insertText("".join(run_chars), run_format)
                    run_chars = [glyph]
                    run_format = char_format
            if run_chars and run_format is not None:
                cursor.insertText("".join(run_chars), run_format)
            if row < self.screen.lines - 1:
                cursor.insertBlock()
        cursor.endEditBlock()
        self._render_cursor()

    def _render_cursor(self) -> None:
        if self.screen is None:
            self.setExtraSelections([])
            return
        row = min(max(self.screen.cursor.y, 0), max(self.screen.lines - 1, 0))
        col = min(max(self.screen.cursor.x, 0), max(self.screen.columns - 1, 0))
        position = row * (self.screen.columns + 1) + col
        cursor = QTextCursor(self.document())
        cursor.setPosition(position)
        cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, 1)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(QColor(self.DEFAULT_FG))
        selection.format.setForeground(QColor(self.DEFAULT_BG))
        self.setExtraSelections([selection])

    def _mark_closed(self, message: str) -> None:
        self._closed_message = message
        if self._socket_notifier is not None:
            self._socket_notifier.setEnabled(False)
        self.master_fd = None
        self.process = None
        self._dirty = True
        self.status_changed.emit("idle")

    def _write_pty(self, payload: bytes) -> None:
        if self.master_fd is None:
            return
        try:
            os.write(self.master_fd, payload)
        except OSError as exc:
            self._mark_closed(f"Failed to send input: {exc}")

    def _encode_key_event(self, event: QKeyEvent) -> bytes | None:
        key = event.key()
        modifiers = event.modifiers()
        if modifiers & Qt.ControlModifier:
            text = event.text()
            if text and len(text) == 1:
                codepoint = ord(text.upper())
                if 64 < codepoint < 91:
                    return bytes([codepoint - 64])
        special_keys = {
            Qt.Key_Return: b"\r",
            Qt.Key_Enter: b"\r",
            Qt.Key_Backspace: b"\x7f",
            Qt.Key_Tab: b"\t",
            Qt.Key_Escape: b"\x1b",
            Qt.Key_Left: b"\x1b[D",
            Qt.Key_Right: b"\x1b[C",
            Qt.Key_Up: b"\x1b[A",
            Qt.Key_Down: b"\x1b[B",
            Qt.Key_Home: b"\x1b[H",
            Qt.Key_End: b"\x1b[F",
            Qt.Key_Delete: b"\x1b[3~",
            Qt.Key_PageUp: b"\x1b[5~",
            Qt.Key_PageDown: b"\x1b[6~",
        }
        if key in special_keys:
            return special_keys[key]
        text = event.text()
        if text:
            return text.encode("utf-8")
        return None

    def _format_for_char(self, char) -> QTextCharFormat:
        fg_name = getattr(char, "fg", "default") if char is not None else "default"
        bg_name = getattr(char, "bg", "default") if char is not None else "default"
        bold = bool(getattr(char, "bold", False)) if char is not None else False
        italics = bool(getattr(char, "italics", False)) if char is not None else False
        underscore = bool(getattr(char, "underscore", False)) if char is not None else False
        reverse = bool(getattr(char, "reverse", False)) if char is not None else False
        key = (fg_name, bg_name, bold, italics, underscore, reverse)
        cached = self._format_cache.get(key)
        if cached is not None:
            return cached
        fg = self._ansi_color(fg_name, default=self.DEFAULT_FG)
        bg = self._ansi_color(bg_name, default=self.DEFAULT_BG)
        if reverse:
            fg, bg = bg, fg
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(fg))
        fmt.setBackground(QColor(bg))
        fmt.setFontWeight(QFont.Bold if bold else QFont.Normal)
        fmt.setFontItalic(italics)
        fmt.setFontUnderline(underscore)
        self._format_cache[key] = fmt
        return fmt

    def _ansi_color(self, name: str | None, default: str) -> str:
        if not name or name == "default":
            return default
        return self.ANSI_COLORS.get(name.lower(), default)

    def _formats_match(self, left: QTextCharFormat, right: QTextCharFormat) -> bool:
        return (
            left.foreground().color() == right.foreground().color()
            and left.background().color() == right.background().color()
            and left.fontWeight() == right.fontWeight()
            and left.fontItalic() == right.fontItalic()
            and left.fontUnderline() == right.fontUnderline()
        )
