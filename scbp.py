"""
SC Blueprint Updater
Reads Star Citizen log files, extracts acquired blueprints,
and highlights them in the global.ini localization file.
"""

import sys
import os
import re
import json
import subprocess
import platform
import traceback
import threading
from pathlib import Path
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QListWidget, QListWidgetItem,
    QTextEdit, QGroupBox, QSplitter, QStatusBar, QFrame, QStackedWidget,
    QSizePolicy, QScrollArea, QMessageBox, QDialog, QProgressBar, QLineEdit
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QObject, QMetaObject, Slot
from PySide6.QtGui import QFont, QColor, QPalette, QIcon, QTextCursor


# ── Paths ────────────────────────────────────────────────────────────────────

def get_app_data_dir() -> Path:
    """
    Returns a config directory outside of MS Store VFS redirection zones.

    MS Store Python virtualizes writes to AppData/Roaming and AppData/Local,
    silently redirecting them to a package-specific sandbox. To avoid this,
    we use Documents/SCBlueprintUpdater on Windows — Documents is not subject
    to VFS redirection and is always the real user folder.

    Windows: C:/Users/<name>/Documents/SCBlueprintUpdater
    Other:   ~/.config/SCBlueprintUpdater
    """
    if platform.system() == "Windows":
        # Use SHGetKnownFolderPath to get the real Documents path,
        # falling back to USERPROFILE/Documents if ctypes is unavailable.
        try:
            import ctypes
            import ctypes.wintypes
            CSIDL_PERSONAL = 5  # My Documents
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(0, CSIDL_PERSONAL, 0, 0, buf)
            base = Path(buf.value)
        except Exception:
            user_profile = os.environ.get("USERPROFILE", str(Path.home()))
            base = Path(user_profile) / "Documents"
    else:
        base = Path.home() / ".config"
    d = base / "SCBlueprintUpdater"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_settings_path() -> Path:
    return get_app_data_dir() / "settings.json"


def get_env_data_dir(env_name: str) -> Path:
    d = get_app_data_dir() / env_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_blueprints_path(env_name: str) -> Path:
    return get_env_data_dir(env_name) / "blueprints.txt"


def get_scanned_log_path(env_name: str) -> Path:
    return get_env_data_dir(env_name) / "scanned_logs.json"


# ── Settings ─────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    p = get_settings_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(data: dict):
    get_settings_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Blueprint list persistence ────────────────────────────────────────────────

def load_blueprints(env_name: str) -> set:
    p = get_blueprints_path(env_name)
    if not p.exists():
        return set()
    lines = p.read_text(encoding="utf-8").splitlines()
    return {l.strip() for l in lines if l.strip()}


def save_blueprints(env_name: str, bps: set):
    p = get_blueprints_path(env_name)
    p.write_text("\n".join(sorted(bps)) + "\n", encoding="utf-8")


# ── Scanned log tracking ──────────────────────────────────────────────────────

def load_scanned_logs(env_name: str) -> dict:
    """Returns dict of {filepath: {"size": int, "mtime": float}}"""
    p = get_scanned_log_path(env_name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_scanned_logs(env_name: str, data: dict):
    get_scanned_log_path(env_name).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


# ── Log parsing ───────────────────────────────────────────────────────────────

BP_PATTERN = re.compile(r"Received Blueprint:\s+(.+?):\s")


def extract_blueprints_from_text(text: str) -> set:
    return {bp.strip() for bp in BP_PATTERN.findall(text)}


def scan_log_file(path: Path) -> set:
    """Read entire file, return found blueprints."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return extract_blueprints_from_text(text)
    except Exception:
        return set()


def scan_log_file_from_offset(path: Path, offset: int) -> tuple[set, int]:
    """Read file from byte offset, return (blueprints, new_offset)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        bps = extract_blueprints_from_text(text)
        new_offset = offset + len(data)
        return bps, new_offset
    except Exception:
        return set(), offset


# ── INI modification ──────────────────────────────────────────────────────────

_ini_lock = threading.Lock()  # prevents concurrent writes to global.ini

DEFAULT_TEMPLATE = "$NAME [+]"
DEFAULT_INI_URL = (
    "https://raw.githubusercontent.com/MrKraken/StarStrings/master"
    "/Data/Localization/english/global.ini"
)

def get_ini_path(env_path: Path) -> Path:
    return env_path / "data" / "Localization" / "english" / "global.ini"


def get_ini_backup_path(env_path: Path) -> Path:
    return env_path / "data" / "Localization" / "english" / "global.ini.original"


def is_valid_sc_env(env_path: Path) -> bool:
    """Return True if env_path looks like a valid Star Citizen installation directory."""
    return (
        (env_path / "Bin64").is_dir()
        and (env_path / "data").is_dir()
        and (env_path / "Data.p4k").is_file()
    )


def parse_template(template: str) -> tuple[str, str]:
    """Split template into (prefix, postfix) around $NAME placeholder."""
    parts = template.split("$NAME", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", ""  # fallback: no wrapping


# Pre-compiled BP patterns: list of (pattern, bp_name) sorted longest-first
_BPPatterns = list[tuple[re.Pattern, str]]


def compile_bp_patterns(blueprints: set) -> _BPPatterns:
    """Compile regex patterns for all BPs once, sorted longest-first."""
    return [
        (re.compile(re.escape(bp)), bp)
        for bp in sorted(blueprints, key=len, reverse=True)
    ]


def apply_highlights_to_value(value: str, patterns: _BPPatterns, prefix: str, postfix: str) -> str:
    """
    Apply prefix+bp+postfix to every occurrence of every BP in value.
    Longer BPs take priority over shorter substrings.
    Already-wrapped occurrences are left unchanged.
    Accepts pre-compiled patterns for efficiency.
    """
    # Find all match spans, resolving overlaps (longest wins)
    claimed: list[tuple[int, int]] = []
    for pattern, _ in patterns:
        for m in pattern.finditer(value):
            if not any(m.start() < ce and m.end() > cs for cs, ce in claimed):
                claimed.append((m.start(), m.end()))

    if not claimed:
        return value

    # Rebuild the string
    parts = []
    i = 0
    for start, end in sorted(claimed):
        parts.append(value[i:start])
        bp_text = value[start:end]
        already = value[:start].endswith(prefix) and value[end:].startswith(postfix)
        parts.append(bp_text if already else f"{prefix}{bp_text}{postfix}")
        i = end
    parts.append(value[i:])
    return "".join(parts)


def update_ini_file(ini_path: Path, blueprints: set, template: str) -> tuple[int, int]:
    """
    Update global.ini: apply highlight template to blueprint names.
    Streams line-by-line via a temp file to minimise peak memory usage.
    Uses a lock to prevent concurrent writes.
    Returns (lines_changed, total_replacements).
    """
    import tempfile, shutil

    if not ini_path.exists():
        return 0, 0

    prefix, postfix = parse_template(template)
    if not prefix and not postfix:
        return 0, 0

    # Compile patterns once before iterating over 46k+ lines
    patterns = compile_bp_patterns(blueprints)

    bom = b"\xef\xbb\xbf"
    lines_changed = 0
    total_replacements = 0

    with _ini_lock:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=ini_path.parent, suffix=".tmp")
        try:
            with open(tmp_fd, "wb") as out_f:
                with open(ini_path, "rb") as in_f:
                    first_bytes = in_f.read(3)
                    has_bom = first_bytes == bom
                    if has_bom:
                        out_f.write(bom)
                    else:
                        in_f.seek(0)

                    for raw_line in in_f:
                        line = raw_line.decode("utf-8", errors="replace")
                        if "=" not in line:
                            out_f.write(line.encode("utf-8"))
                            continue

                        key, sep, value = line.partition("=")
                        new_value = value.rstrip("\r\n")
                        line_ending = line[len(key) + len(sep) + len(new_value):]
                        original_value = new_value

                        new_value = apply_highlights_to_value(new_value, patterns, prefix, postfix)

                        if new_value != original_value:
                            lines_changed += 1
                            total_replacements += new_value.count(prefix) - original_value.count(prefix)

                        out_f.write((key + sep + new_value + line_ending).encode("utf-8"))

            shutil.move(tmp_path, str(ini_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    return lines_changed, total_replacements


def backup_ini_if_needed(env_path: Path) -> bool:
    """Save original global.ini on first run. Returns True if backup was created."""
    ini_path = get_ini_path(env_path)
    backup_path = get_ini_backup_path(env_path)
    if ini_path.exists() and not backup_path.exists():
        import shutil
        shutil.copy2(str(ini_path), str(backup_path))
        return True
    return False


def restore_ini_from_backup(env_path: Path) -> bool:
    """Restore global.ini from backup. Returns True on success."""
    import shutil
    backup_path = get_ini_backup_path(env_path)
    ini_path = get_ini_path(env_path)
    if backup_path.exists():
        shutil.copy2(str(backup_path), str(ini_path))
        return True
    return False


# ── Download worker & dialog ──────────────────────────────────────────────────

class DownloadWorker(QObject):
    """Downloads a file from a URL or copies from a local path in a background thread."""
    progress = Signal(int, int)   # (bytes_downloaded, total_bytes)
    finished = Signal(bool, str)  # (success, error_message)

    def __init__(self, source: str, dest: Path):
        super().__init__()
        self._source = source
        self._dest = dest
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @Slot()
    def run(self):
        try:
            self._run_impl()
        except InterruptedError:
            self.finished.emit(False, "Cancelled")
        except Exception:
            self.finished.emit(False, traceback.format_exc())

    def _run_impl(self):
        source = self._source
        dest = self._dest
        dest.parent.mkdir(parents=True, exist_ok=True)

        if source.startswith("http://") or source.startswith("https://"):
            import urllib.request
            tmp = dest.parent / (dest.name + ".downloading")
            try:
                with urllib.request.urlopen(source) as resp:
                    total = int(resp.headers.get("Content-Length") or 0)
                    downloaded = 0
                    with open(tmp, "wb") as f:
                        while True:
                            if self._cancelled:
                                raise InterruptedError
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            self.progress.emit(downloaded, total)
                import shutil
                shutil.move(str(tmp), str(dest))
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        else:
            import shutil
            shutil.copy2(source, str(dest))
            size = dest.stat().st_size
            self.progress.emit(size, size)

        self.finished.emit(True, "")


class DownloadProgressDialog(QDialog):
    """Modal dialog that downloads or copies global.ini and shows progress."""

    def __init__(self, source: str, dest: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Downloading global.ini")
        self.setModal(True)
        self.setMinimumWidth(500)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        src_label = QLabel(f"<b>Source:</b> {source}")
        src_label.setWordWrap(True)
        src_label.setObjectName("subtitle")
        layout.addWidget(src_label)

        self._status = QLabel("Connecting…")
        layout.addWidget(self._status)

        self._bar = QProgressBar()
        self._bar.setRange(0, 0)  # indeterminate until Content-Length is known
        layout.addWidget(self._bar)

        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.clicked.connect(self._cancel)
        layout.addWidget(self._btn_cancel, alignment=Qt.AlignRight)

        self._thread = QThread()
        self._worker = DownloadWorker(source, dest)
        self._worker.moveToThread(self._thread)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _on_progress(self, downloaded: int, total: int):
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(downloaded)
            self._status.setText(
                f"Downloaded {downloaded / 1048576:.1f} MB of {total / 1048576:.1f} MB"
            )
        else:
            self._status.setText(f"Downloaded {downloaded / 1048576:.1f} MB…")

    def _on_finished(self, success: bool, error: str):
        self._thread.quit()
        self._thread.wait()
        if success:
            self._status.setText("Done!")
            self.accept()
        elif error == "Cancelled":
            self.reject()
        else:
            QMessageBox.critical(
                self, "Download Failed",
                f"Could not fetch global.ini:\n\n{error}"
            )
            self.reject()

    def _cancel(self):
        self._worker.cancel()
        self._btn_cancel.setEnabled(False)
        self._status.setText("Cancelling…")


# ── Worker thread ─────────────────────────────────────────────────────────────
# ScanWorker lives in a background thread but owns NO QTimers.
# All timers stay in the main thread (MonitorPage); they call worker slots
# via queued signals, which is safe cross-thread in Qt.

class ScanWorker(QObject):
    log_message = Signal(str)
    blueprints_updated = Signal(set)
    ini_updated = Signal(int, int)

    def __init__(self, env_path: Path, env_name: str, template: str):
        super().__init__()
        self.env_path = env_path
        self.env_name = env_name
        self.template = template
        self._game_log_offset = 0

    @Slot()
    def initial_scan(self):
        """Run once at startup: scan logbackups + full Game.log read."""
        try:
            self._initial_scan_impl()
        except Exception:
            self.log_message.emit(f"💥 ERROR in initial_scan:\n{traceback.format_exc()}")

    def _initial_scan_impl(self):
        self.log_message.emit("🔍 Starting initial scan of logbackups/...")

        if backup_ini_if_needed(self.env_path):
            self.log_message.emit("💾 Original global.ini backed up")

        logbackups_dir = self.env_path / "logbackups"
        scanned = load_scanned_logs(self.env_name)
        bps = load_blueprints(self.env_name)
        new_bps_found = set()

        if logbackups_dir.exists():
            log_files = sorted(logbackups_dir.glob("*.log"))
            for lf in log_files:
                key = str(lf)
                stat = lf.stat()
                prev = scanned.get(key)
                if prev and prev.get("size") == stat.st_size and prev.get("mtime") == stat.st_mtime:
                    continue
                found = scan_log_file(lf)
                new_bps_found |= found - bps
                bps |= found
                scanned[key] = {"size": stat.st_size, "mtime": stat.st_mtime}
                self.log_message.emit(f"  📄 {lf.name}: found {len(found)} BPs")
            save_scanned_logs(self.env_name, scanned)
        else:
            self.log_message.emit("  ⚠️  logbackups/ directory not found")

        game_log = self.env_path / "Game.log"
        if game_log.exists():
            found, self._game_log_offset = scan_log_file_from_offset(game_log, 0)
            new_bps_found |= found - bps
            bps |= found
            self.log_message.emit(f"  📄 Game.log: found {len(found)} BPs (initial read)")
        else:
            self.log_message.emit("  ⚠️  Game.log not found")
            self._game_log_offset = 0

        if new_bps_found:
            save_blueprints(self.env_name, bps)
            self.log_message.emit(f"✅ {len(new_bps_found)} new BPs added to list")
            self.blueprints_updated.emit(bps)
            self._update_ini(bps)
        else:
            self.log_message.emit("✅ Initial scan complete, no new BPs found")
            self.blueprints_updated.emit(bps)

        self.log_message.emit("⏱  Polling Game.log every 60 seconds...")

    @Slot()
    def poll_game_log(self):
        """Called by the main-thread timer every 60 s."""
        try:
            self._poll_game_log_impl()
        except Exception:
            self.log_message.emit(f"💥 ERROR in poll_game_log:\n{traceback.format_exc()}")

    def _poll_game_log_impl(self):
        game_log = self.env_path / "Game.log"
        if not game_log.exists():
            return

        current_size = game_log.stat().st_size

        if current_size < self._game_log_offset:
            self.log_message.emit("🔄 Game.log shrank — game restarted, re-reading from start")
            self._game_log_offset = 0

        if current_size == self._game_log_offset:
            return

        bps = load_blueprints(self.env_name)
        found, new_offset = scan_log_file_from_offset(game_log, self._game_log_offset)
        self._game_log_offset = new_offset

        new_bps = found - bps
        if new_bps:
            bps |= new_bps
            save_blueprints(self.env_name, bps)
            self.log_message.emit(f"🆕 Found {len(new_bps)} new BP(s): {', '.join(sorted(new_bps))}")
            self.blueprints_updated.emit(bps)
            self._update_ini(bps)

    def _update_ini(self, bps: set):
        try:
            ini_path = get_ini_path(self.env_path)
            if not ini_path.exists():
                self.log_message.emit(f"  ⚠️  global.ini not found at {ini_path}")
                return
            self.log_message.emit("✏️  Updating global.ini...")
            changed, replacements = update_ini_file(ini_path, bps, self.template)
            self.log_message.emit(f"  ✅ global.ini updated: {changed} lines, {replacements} new tags")
            self.ini_updated.emit(changed, replacements)
        except Exception:
            self.log_message.emit(f"💥 ERROR in _update_ini:\n{traceback.format_exc()}")

    @Slot()
    def rescan_ini(self):
        bps = load_blueprints(self.env_name)
        self._update_ini(bps)

    def update_template(self, template: str):
        self.template = template
        self.rescan_ini()

    @Slot()
    def reload_blueprints(self):
        bps = load_blueprints(self.env_name)
        self.blueprints_updated.emit(bps)
        self.log_message.emit(f"🔄 Blueprint list reloaded ({len(bps)} items)")
        self._update_ini(bps)


# ── Main Window ───────────────────────────────────────────────────────────────

DARK_STYLE = """
QMainWindow, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
}
QGroupBox {
    border: 1px solid #45475a;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 8px;
    font-weight: bold;
    color: #89b4fa;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}
QPushButton {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    border-radius: 5px;
    padding: 6px 14px;
    min-height: 28px;
}
QPushButton:hover {
    background-color: #45475a;
    border-color: #89b4fa;
}
QPushButton:pressed {
    background-color: #585b70;
}
QPushButton:disabled {
    color: #6c7086;
    border-color: #313244;
    background-color: #252535;
}
QPushButton#primary {
    background-color: #89b4fa;
    color: #1e1e2e;
    font-weight: bold;
    border: none;
}
QPushButton#primary:hover {
    background-color: #b4befe;
}
QPushButton#danger {
    background-color: #f38ba8;
    color: #1e1e2e;
    font-weight: bold;
    border: none;
}
QPushButton#danger:hover {
    background-color: #fab387;
}
QListWidget {
    background-color: #181825;
    border: 1px solid #45475a;
    border-radius: 5px;
    outline: none;
}
QListWidget::item {
    padding: 4px 8px;
}
QListWidget::item:selected {
    background-color: #313244;
    color: #cdd6f4;
}
QListWidget::item:hover {
    background-color: #252535;
}
QTextEdit {
    background-color: #181825;
    border: 1px solid #45475a;
    border-radius: 5px;
    font-family: "Consolas", monospace;
    font-size: 12px;
}
QLabel#title {
    font-size: 22px;
    font-weight: bold;
    color: #cba6f7;
}
QLabel#subtitle {
    font-size: 13px;
    color: #a6adc8;
}
QLabel#envname {
    font-size: 16px;
    font-weight: bold;
    color: #89dceb;
}
QLabel#path {
    font-size: 11px;
    color: #6c7086;
}
QStatusBar {
    background-color: #181825;
    color: #a6adc8;
    border-top: 1px solid #313244;
}
QScrollBar:vertical {
    background: #181825;
    width: 8px;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: #45475a;
    border-radius: 4px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover {
    background: #585b70;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QFrame#divider {
    color: #45475a;
}
"""


class SetupPage(QWidget):
    """Page 1: select root directory and environment."""
    env_selected = Signal(Path, str, str)  # (env_path, env_name, template)

    def __init__(self):
        super().__init__()
        self._root_path: Path | None = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 20)
        layout.setSpacing(16)

        # ── Header ────────────────────────────────────────────────────────────
        title = QLabel("SC Blueprint Updater")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        subtitle = QLabel("Automatically highlights acquired blueprints in your localization file")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout.addWidget(title)
        layout.addWidget(subtitle)

        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setObjectName("divider")
        layout.addWidget(div)

        # ── Root Directory (fixed height) ─────────────────────────────────────
        root_group = QGroupBox("Root Directory")
        root_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        root_layout = QVBoxLayout(root_group)
        root_layout.setContentsMargins(12, 16, 12, 12)

        root_row = QHBoxLayout()
        self._root_label = QLabel("Not selected")
        self._root_label.setObjectName("path")
        self._root_label.setWordWrap(True)
        btn_browse = QPushButton("Browse...")
        btn_browse.setFixedWidth(100)
        btn_browse.clicked.connect(self._browse_root)
        root_row.addWidget(self._root_label, 1)
        root_row.addWidget(btn_browse)
        root_layout.addLayout(root_row)
        layout.addWidget(root_group)

        # ── Select Environment (stretches to fill available space) ────────────
        env_group = QGroupBox("Select Environment")
        env_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        env_outer = QVBoxLayout(env_group)
        env_outer.setContentsMargins(12, 16, 12, 12)
        env_outer.setSpacing(8)

        env_splitter = QSplitter(Qt.Horizontal)
        env_splitter.setChildrenCollapsible(False)

        self._env_list = QListWidget()
        self._env_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._env_list.currentRowChanged.connect(self._on_env_selected)

        self._env_info = QLabel("Select an environment to see details")
        self._env_info.setObjectName("subtitle")
        self._env_info.setWordWrap(True)
        self._env_info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._env_info.setContentsMargins(8, 4, 4, 4)
        self._env_info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        env_splitter.addWidget(self._env_list)
        env_splitter.addWidget(self._env_info)
        env_splitter.setSizes([220, 340])
        env_outer.addWidget(env_splitter)
        layout.addWidget(env_group, 1)   # stretch factor 1 — takes all spare space

        # ── Highlight Template (fixed height) ─────────────────────────────────
        tpl_group = QGroupBox("Highlight Template")
        tpl_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        tpl_layout = QVBoxLayout(tpl_group)
        tpl_layout.setContentsMargins(12, 16, 12, 12)
        tpl_layout.setSpacing(8)

        tpl_note = QLabel("Use <b>$NAME</b> as placeholder for the blueprint name.")
        tpl_note.setObjectName("subtitle")
        tpl_note.setTextFormat(Qt.RichText)
        tpl_layout.addWidget(tpl_note)

        tpl_row = QHBoxLayout()
        self._tpl_edit = QLineEdit()
        self._tpl_edit.setPlaceholderText("e.g.  $NAME [+]  or  <EM2>$NAME</EM2>")
        self._tpl_edit.setText(DEFAULT_TEMPLATE)
        tpl_row.addWidget(self._tpl_edit)
        btn_tpl_reset = QPushButton("Reset")
        btn_tpl_reset.setFixedWidth(60)
        btn_tpl_reset.clicked.connect(lambda: self._tpl_edit.setText(DEFAULT_TEMPLATE))
        tpl_row.addWidget(btn_tpl_reset)
        tpl_layout.addLayout(tpl_row)
        layout.addWidget(tpl_group)

        # ── global.ini Source (fixed height) ─────────────────────────────────
        ini_src_group = QGroupBox("global.ini Source")
        ini_src_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        ini_src_layout = QVBoxLayout(ini_src_group)
        ini_src_layout.setContentsMargins(12, 16, 12, 12)
        ini_src_layout.setSpacing(8)

        ini_src_note = QLabel(
            "URL or local file path to fetch <b>global.ini</b> from when it is missing. "
            "Leave blank to use the built-in default."
        )
        ini_src_note.setObjectName("subtitle")
        ini_src_note.setWordWrap(True)
        ini_src_note.setTextFormat(Qt.RichText)
        ini_src_layout.addWidget(ini_src_note)

        ini_src_row = QHBoxLayout()
        self._ini_src_edit = QLineEdit()
        self._ini_src_edit.setPlaceholderText(DEFAULT_INI_URL)
        ini_src_row.addWidget(self._ini_src_edit)
        btn_ini_browse = QPushButton("Browse…")
        btn_ini_browse.setFixedWidth(80)
        btn_ini_browse.clicked.connect(self._browse_ini_src)
        ini_src_row.addWidget(btn_ini_browse)
        btn_ini_reset = QPushButton("Reset")
        btn_ini_reset.setFixedWidth(60)
        btn_ini_reset.clicked.connect(lambda: self._ini_src_edit.clear())
        ini_src_row.addWidget(btn_ini_reset)
        ini_src_layout.addLayout(ini_src_row)
        layout.addWidget(ini_src_group)

        # ── Start button (fixed height) ───────────────────────────────────────
        self._btn_start = QPushButton("Start Monitoring")
        self._btn_start.setObjectName("primary")
        self._btn_start.setFixedHeight(40)
        self._btn_start.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._btn_start.setEnabled(False)
        self._btn_start.clicked.connect(self._on_start)
        layout.addWidget(self._btn_start)

    def restore_settings(self, settings: dict):
        root = settings.get("root_path")
        if root and Path(root).exists():
            self._set_root(Path(root))
        tpl = settings.get("template", DEFAULT_TEMPLATE)
        self._tpl_edit.setText(tpl)
        self._ini_src_edit.setText(settings.get("ini_source", ""))

    def _browse_root(self):
        start = str(self._root_path) if self._root_path and self._root_path.exists() else ""
        path = QFileDialog.getExistingDirectory(self, "Select Root Directory", start)
        if path:
            self._set_root(Path(path))
            s = load_settings()
            s["root_path"] = str(path)
            save_settings(s)

    def _set_root(self, path: Path):
        self._root_path = path
        self._root_label.setText(str(path))
        self._populate_envs()

    def _populate_envs(self):
        self._env_list.clear()
        if not self._root_path:
            return

        envs = sorted(
            [d for d in self._root_path.iterdir() if d.is_dir()],
            key=lambda x: x.name,
        )
        valid = [e for e in envs if is_valid_sc_env(e)]
        invalid = [e for e in envs if not is_valid_sc_env(e)]

        for env in valid:
            item = QListWidgetItem(env.name)
            item.setData(Qt.UserRole, True)
            self._env_list.addItem(item)

        if invalid:
            sep = QListWidgetItem("── Not an SC Environment ──")
            sep.setFlags(Qt.NoItemFlags)
            sep.setTextAlignment(Qt.AlignCenter)
            sep.setForeground(QColor("#585b70"))
            sep.setData(Qt.UserRole, None)
            self._env_list.addItem(sep)

            for env in invalid:
                item = QListWidgetItem(env.name)
                item.setData(Qt.UserRole, False)
                item.setForeground(QColor("#585b70"))
                self._env_list.addItem(item)

        self._btn_start.setEnabled(False)

    def _on_env_selected(self, row: int):
        if row < 0 or not self._root_path:
            self._btn_start.setEnabled(False)
            return

        item = self._env_list.item(row)
        valid_env = item.data(Qt.UserRole)

        if valid_env is None:  # separator row
            self._btn_start.setEnabled(False)
            self._env_info.setText("Select an environment to see details")
            return

        env_name = item.text()
        env_path = self._root_path / env_name
        lines = [f"<b>Path:</b> {env_path}"]

        if not valid_env:
            lines.append(
                "<b>SC installation:</b> ❌ Bin64/, data/ or Data.p4k not found"
            )
            lines.append(
                "<br>This folder does not look like a Star Citizen environment. "
                "Check that you selected the correct root directory.<br>"
                "<br>Star Citizen is typically installed at:<br>"
                "<code>C:\\Program Files\\Roberts Space Industries\\StarCitizen\\</code>"
            )
            self._env_info.setText("<br>".join(lines))
            self._btn_start.setEnabled(False)
            return

        ini_path = get_ini_path(env_path)
        logbackups = env_path / "logbackups"
        game_log = env_path / "Game.log"

        lines.append("<b>SC installation:</b> ✅ valid")

        if ini_path.exists():
            lines.append("<b>global.ini:</b> ✅ found")
        else:
            lines.append("<b>global.ini:</b> ❌ not found — will be downloaded on Start")

        lines.append(f"<b>Game.log:</b> {'✅ found' if game_log.exists() else '❌ not found'}")
        if logbackups.exists():
            count = len(list(logbackups.glob("*.log")))
            lines.append(f"<b>logbackups:</b> {count} file(s)")
        else:
            lines.append("<b>logbackups:</b> ❌ not found")

        bp_count = len(load_blueprints(env_name))
        lines.append(f"<b>Known blueprints:</b> {bp_count}")

        self._env_info.setText("<br>".join(lines))
        self._btn_start.setEnabled(True)

    def _on_start(self):
        row = self._env_list.currentRow()
        if row < 0 or not self._root_path:
            return

        item = self._env_list.item(row)
        if not item.data(Qt.UserRole):
            QMessageBox.warning(
                self, "Not a valid SC environment",
                "The selected folder does not appear to be a Star Citizen environment.\n\n"
                "Required files/dirs: Bin64\\, data\\, Data.p4k\n\n"
                "Star Citizen is typically installed at:\n"
                "C:\\Program Files\\Roberts Space Industries\\StarCitizen\\\n\n"
                "Select that folder as the root directory, then choose "
                "an environment (e.g. LIVE or PTU) from the list.",
            )
            return

        env_name = item.text()
        env_path = self._root_path / env_name
        template = self._tpl_edit.text().strip() or DEFAULT_TEMPLATE
        ini_src = self._ini_src_edit.text().strip()

        s = load_settings()
        s["template"] = template
        s["ini_source"] = ini_src
        save_settings(s)

        ini_path = get_ini_path(env_path)
        if not ini_path.exists():
            source = ini_src or DEFAULT_INI_URL
            reply = QMessageBox.question(
                self, "Download global.ini",
                f"global.ini is not present in this environment.\n\n"
                f"Download from:\n{source}\n\nProceed?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return
            dlg = DownloadProgressDialog(source, ini_path, self)
            if dlg.exec() != QDialog.Accepted:
                return
            backup_ini_if_needed(env_path)

        self.env_selected.emit(env_path, env_name, template)

    def _browse_ini_src(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select global.ini", "", "INI files (*.ini);;All files (*)"
        )
        if path:
            self._ini_src_edit.setText(path)


class MonitorPage(QWidget):
    """Page 2: active monitoring view."""
    stop_requested = Signal()

    def __init__(self):
        super().__init__()
        self._env_name = ""
        self._env_path: Path | None = None
        self._worker: ScanWorker | None = None
        self._thread: QThread | None = None
        self._poll_timer: QTimer | None = None
        self._countdown_timer: QTimer | None = None
        self._seconds_left = 60
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # Header row
        header = QHBoxLayout()
        env_col = QVBoxLayout()
        self._env_label = QLabel("Environment")
        self._env_label.setObjectName("envname")
        self._path_label = QLabel("")
        self._path_label.setObjectName("path")
        env_col.addWidget(self._env_label)
        env_col.addWidget(self._path_label)

        btn_stop = QPushButton("◀  Back")
        btn_stop.setFixedWidth(100)
        btn_stop.clicked.connect(self._on_stop)

        self._btn_restore = QPushButton("🔙 Restore Original Localization")
        self._btn_restore.setObjectName("danger")
        self._btn_restore.setToolTip("Restore global.ini from the backup taken on first run")
        self._btn_restore.clicked.connect(self._restore_ini)

        header.addLayout(env_col, 1)
        header.addWidget(self._btn_restore)
        header.addWidget(btn_stop)
        layout.addLayout(header)

        # Status indicator
        self._status_label = QLabel("● Idle")
        self._status_label.setStyleSheet("color: #a6adc8; font-weight: bold;")
        layout.addWidget(self._status_label)

        # Splitter: blueprint list | log
        splitter = QSplitter(Qt.Horizontal)

        # Left: blueprint list
        bp_widget = QWidget()
        bp_layout = QVBoxLayout(bp_widget)
        bp_layout.setContentsMargins(0, 0, 0, 0)
        bp_layout.setSpacing(6)

        bp_header = QHBoxLayout()
        self._bp_count_label = QLabel("Blueprints (0)")
        self._bp_count_label.setObjectName("subtitle")
        bp_header.addWidget(self._bp_count_label, 1)

        btn_edit = QPushButton("✏️ Edit list")
        btn_edit.setToolTip("Open blueprints.txt in system editor")
        btn_edit.clicked.connect(self._open_editor)
        btn_reload = QPushButton("🔁 Re-apply")
        btn_reload.setToolTip("Reload blueprint list from file and re-apply to global.ini")
        btn_reload.clicked.connect(self._reload_blueprints)
        bp_header.addWidget(btn_edit)
        bp_header.addWidget(btn_reload)

        bp_layout.addLayout(bp_header)

        self._bp_list = QListWidget()
        bp_layout.addWidget(self._bp_list)

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("Filter…")
        bp_layout.addWidget(self._filter_edit)

        # Right: log
        log_widget = QWidget()
        log_layout = QVBoxLayout(log_widget)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)

        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("Activity Log"))
        btn_clear = QPushButton("Clear")
        btn_clear.setFixedWidth(60)
        btn_clear.clicked.connect(lambda: self._log.clear())
        log_header.addWidget(btn_clear)
        log_layout.addLayout(log_header)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        log_layout.addWidget(self._log)

        splitter.addWidget(bp_widget)
        splitter.addWidget(log_widget)
        splitter.setSizes([300, 500])

        layout.addWidget(splitter)

    def start_monitoring(self, env_path: Path, env_name: str, template: str):
        self._env_name = env_name
        self._env_path = env_path
        self._template = template
        self._env_label.setText(env_name)
        self._path_label.setText(str(env_path))
        self._log.clear()
        self._bp_list.clear()

        # Enable restore button only if backup exists
        backup_exists = get_ini_backup_path(env_path).exists()
        self._btn_restore.setEnabled(backup_exists)

        self._set_status_active()

        # Worker in background thread (owns no timers)
        self._thread = QThread()
        self._worker = ScanWorker(env_path, env_name, template)
        self._worker.moveToThread(self._thread)
        self._worker.log_message.connect(self._append_log)
        self._worker.blueprints_updated.connect(self._update_bp_list)
        self._worker.ini_updated.connect(self._on_ini_updated)
        self._thread.started.connect(self._worker.initial_scan)
        self._thread.start()

        # Poll timer — lives in main thread, invokes worker slot via queued connection
        self._seconds_left = 60
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(60000)
        self._poll_timer.timeout.connect(self._on_poll_tick)
        self._poll_timer.start()

        # Countdown timer — 1 s ticks, main thread only
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._on_countdown_tick)
        self._countdown_timer.start()

    def stop_monitoring(self):
        # Stop main-thread timers first (they are ours, no cross-thread issues)
        if self._poll_timer:
            self._poll_timer.stop()
            self._poll_timer.deleteLater()
            self._poll_timer = None
        if self._countdown_timer:
            self._countdown_timer.stop()
            self._countdown_timer.deleteLater()
            self._countdown_timer = None

        # Shut down the worker thread cleanly
        if self._thread:
            self._thread.quit()
            if not self._thread.wait(3000):
                self._thread.terminate()
                self._thread.wait(1000)
        self._worker = None
        self._thread = None
        self._set_status_idle()

    def _on_stop(self):
        self.stop_monitoring()
        self.stop_requested.emit()

    def _set_status_active(self):
        self._status_label.setText("● Monitoring — next scan in 60s")
        self._status_label.setStyleSheet("color: #a6e3a1; font-weight: bold;")

    def _set_status_idle(self):
        self._status_label.setText("● Idle")
        self._status_label.setStyleSheet("color: #a6adc8; font-weight: bold;")

    def _on_poll_tick(self):
        """Called every 60 s in main thread; delegates work to the worker thread."""
        self._seconds_left = 60
        if self._worker:
            QMetaObject.invokeMethod(self._worker, "poll_game_log", Qt.QueuedConnection)

    def _on_countdown_tick(self):
        self._seconds_left = max(0, self._seconds_left - 1)
        self._status_label.setText(f"● Monitoring — next scan in {self._seconds_left}s")

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.append(f"<span style='color:#6c7086'>[{ts}]</span> {msg}")
        # Auto-scroll
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _update_bp_list(self, bps: set):
        self._bp_list.clear()
        for bp in sorted(bps):
            self._bp_list.addItem(bp)
        self._bp_count_label.setText(f"Blueprints ({len(bps)})")

    def _on_ini_updated(self, changed: int, replacements: int):
        self._append_log(
            f"<span style='color:#a6e3a1'>global.ini: {changed} lines changed, "
            f"{replacements} new tags added</span>"
        )

    def _restore_ini(self):
        reply = QMessageBox.question(
            self, "Restore Original Localization",
            "This will overwrite global.ini with the original backup.\n"
            "All blueprint highlights will be lost.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        if restore_ini_from_backup(self._env_path):
            self._append_log("🔙 global.ini restored from original backup")
        else:
            self._append_log("⚠️  No backup found — cannot restore")

    def _open_editor(self):
        p = get_blueprints_path(self._env_name)
        if not p.exists():
            # Create empty file
            p.write_text("", encoding="utf-8")
        if platform.system() == "Windows":
            os.startfile(str(p))
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(p)])
        else:
            subprocess.run(["xdg-open", str(p)])
        self._append_log(f"📝 Opened {p.name} in system editor")

    def _reload_blueprints(self):
        if self._worker:
            QMetaObject.invokeMethod(self._worker, "reload_blueprints", Qt.QueuedConnection)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SC Blueprint Updater")
        self.setMinimumSize(800, 560)
        self.resize(900, 620)

        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        self._setup_page = SetupPage()
        self._monitor_page = MonitorPage()
        self._stack.addWidget(self._setup_page)
        self._stack.addWidget(self._monitor_page)

        self._setup_page.env_selected.connect(self._on_env_selected)
        self._monitor_page.stop_requested.connect(self._on_back_to_setup)

        status = QStatusBar()
        self.setStatusBar(status)
        status.showMessage("Ready")

        # Restore settings
        settings = load_settings()
        self._setup_page.restore_settings(settings)

    def _on_env_selected(self, env_path: Path, env_name: str, template: str):
        self._monitor_page.start_monitoring(env_path, env_name, template)
        self._stack.setCurrentWidget(self._monitor_page)
        self.statusBar().showMessage(f"Monitoring: {env_name}")

    def _on_back_to_setup(self):
        self._stack.setCurrentWidget(self._setup_page)
        self.statusBar().showMessage("Ready")
        # Refresh env info if same env is selected
        row = self._setup_page._env_list.currentRow()
        if row >= 0:
            self._setup_page._on_env_selected(row)

    def closeEvent(self, event):
        self._monitor_page.stop_monitoring()
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    # Diagnostic: print actual paths so sandbox issues are visible
    print(f"[SCBPUpdater] USERPROFILE = {os.environ.get('USERPROFILE', 'NOT SET')}")
    print(f"[SCBPUpdater] APPDATA     = {os.environ.get('APPDATA', 'NOT SET')}")
    cfg = get_app_data_dir()
    print(f"[SCBPUpdater] Config dir  = {cfg}")
    print(f"[SCBPUpdater] Dir exists  = {cfg.exists()}")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLE)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
