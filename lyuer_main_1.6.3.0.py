#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MienArch Lyuer v1.6.3.0 - EPUB & PDF & TXT Reader
版权所有 (c) MienArch | 面包图书馆 | 2026
许可证：GNU GPL v3.0 or later
基于 PySide6 / Qt WebEngine / QtPdf 的电子书阅读器

v1.6.3.0: Bug 修复
         1) [严重] 修复启动后自定义阅读背景/文字颜色被静默清空：apply_theme
            恢复主题下拉框时未屏蔽信号，同步触发 on_theme_changed 将
            reader_bg/reader_fg 置空并立即写回配置（主题非默认「黑色」时
            必现），现对 setCurrentIndex 加 blockSignals；
         2) [严重] 修复书库树触屏轻点选中错位：itemAt/indexAt 与触摸事件
            同为 viewport 坐标系，此前误加表头高度，轻点实际选中偏下方
            约一行处的条目，现移除多余偏移；
         3) [中] 退出时后台解析线程等待 5 秒仍未结束则兜底 terminate，
            避免超大文件解析卡死导致程序无法退出；
         4) [轻] 「未安装 PyMuPDF」扫描提示改为每次扫描仅提示一次（此前
            按关联目录数重复弹出状态栏消息）；
         5) [轻] PDF 滚动换页回调移除重复的 update_nav_buttons 调用；
         （另见 lyuer_epubviewer v1.0.4 / lyuer_pdfviewer v2.1.2 /
           lyuer_database v1.4.1 / lyuer_manager v1.1.1）

"""

import sys
import os
import platform
import subprocess
import shutil
import re
import json
import html as html_module
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import lyuer_database as lyuer_db
except ImportError as e:
    print('无法导入 lyuer_database.py，请确认它与本脚本位于同一目录。', file=sys.stderr)
    print(str(e), file=sys.stderr)
    raise

# =============================================================================
# 依赖自检
# =============================================================================
_MISSING_DEPS = []

def _check_deps():
    missing = []
    try:
        import PySide6  # noqa: F401
    except ImportError:
        missing.append("PySide6")
    try:
        from PySide6 import QtWebEngineWidgets  # noqa: F401
    except ImportError:
        missing.append("PySide6-WebEngine")
    try:
        import ebooklib  # noqa: F401
    except ImportError:
        missing.append("ebooklib")
    try:
        import bs4  # noqa: F401
    except ImportError:
        missing.append("beautifulsoup4")
    try:
        import lxml  # noqa: F401
    except ImportError:
        missing.append("lxml")
    try:
        import fitz  # noqa: F401
    except ImportError:
        missing.append("PyMuPDF")
    return missing

_MISSING_DEPS = _check_deps()

if _MISSING_DEPS:
    msg_lines = [
        "检测到以下 Python 依赖包尚未安装：",
        "",
        ", ".join(_MISSING_DEPS),
        "",
        "请在终端中运行以下命令安装：",
        f"  pip install {' '.join(_MISSING_DEPS)}",
        "",
        "或分别安装："
    ]
    for pkg in _MISSING_DEPS:
        msg_lines.append(f"  pip install {pkg}")
    msg_lines.append("")
    msg_lines.append("安装完成后请重新运行本程序。")
    full_msg = "\n".join(msg_lines)

    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, full_msg, "MienArchReader - 依赖缺失", 0x10)
        else:
            try:
                import tkinter
                from tkinter import messagebox
                root = tkinter.Tk()
                root.withdraw()
                messagebox.showerror("MienArchReader - 依赖缺失", full_msg)
                root.destroy()
            except Exception:
                pass
    except Exception:
        pass

    print("=" * 60, file=sys.stderr)
    print("MienArchReader 启动失败：缺少依赖包", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(full_msg, file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    input("按 Enter 键退出...")
    sys.exit(1)

# PDF 浏览器式渲染引擎已拆分到 lyuer_pdfviewer.py（v1.4.0）
import lyuer_pdfviewer as pdf_viewer
fitz = pdf_viewer.fitz  # 兼容旧引用（依赖自检 / 扫描线程）

# EPUB 引擎已拆分到 lyuer_epubviewer.py（v1.6.0）
# 注意：本模块必须在 QApplication 创建前导入，以初始化 Qt WebEngine。
import lyuer_epubviewer as epub_viewer

# =============================================================================
# 标准库 & 第三方库导入
# =============================================================================
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QHeaderView, QTextBrowser,
    QFileDialog, QLabel, QPushButton, QComboBox, QMenu,
    QMessageBox, QAbstractItemView, QListWidget, QListWidgetItem,
    QColorDialog, QFontComboBox, QLineEdit, QCheckBox, QStackedWidget,
    QInputDialog, QProgressBar
)
from PySide6.QtCore import Qt, QSettings, QThread, Signal, QTimer, QEvent, QElapsedTimer, QPoint
from PySide6.QtGui import (QAction, QFont, QDragEnterEvent, QDropEvent, QKeySequence, QColor, QPalette, QIcon, QShortcut, QPainter)


# =============================================================================
# 配置路径与迁移工具
# =============================================================================
def _get_app_dir() -> str:
    """返回应用（脚本/可执行文件）所在目录。兼容 PyInstaller 打包。"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后 sys.executable 为 exe 路径
        exe_path = sys.executable
    else:
        exe_path = os.path.abspath(__file__)
    return os.path.dirname(exe_path)


def _get_config_path() -> str:
    """返回 JSON 配置文件完整路径：应用目录/.config/Lyuer_config.json"""
    app_dir = _get_app_dir()
    config_dir = os.path.join(app_dir, ".config")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "Lyuer_config.json")


def _get_resource_dir() -> str:
    """返回资源目录。

    - 开发运行：脚本所在目录；
    - PyInstaller onedir：exe 同级的 _internal 目录（v1.6.2 起图标等资源
      统一放这里，与 onedir 打包输出结构一致）；
    - PyInstaller onefile：解压临时目录 sys._MEIPASS。
    """
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        for cand in ("_internal", "internal"):
            full = os.path.join(exe_dir, cand)
            if os.path.isdir(full):
                return full
        return getattr(sys, "_MEIPASS", exe_dir)
    return os.path.dirname(os.path.abspath(__file__))


def _get_icon_path() -> str:
    """返回图标文件完整路径：优先 _internal/lyuer.png，兼容旧名 Lyuer.ico。"""
    for name in ("lyuer.png", "Lyuer.png", "lyuer.ico", "Lyuer.ico"):
        full = os.path.join(_get_resource_dir(), name)
        if os.path.exists(full):
            return full
    return ""


def _migrate_qsettings_to_json(config_path: str) -> Optional[Dict]:
    """如存在旧版 QSettings 配置，则迁移到 JSON 并返回其字典；否则返回 None。"""
    try:
        old_settings = QSettings("MienArch", "MienArchReader")
        if not old_settings.allKeys():
            return None

        migrated: Dict = {}
        key_map = {
            "font_size": ("font_size", int, 18),
            "theme": ("theme", str, "mien"),
            "custom_bg": ("custom_bg", str, ""),
            "custom_fg": ("custom_fg", str, ""),
            "custom_font_western": ("custom_font_western", str, "Palatino Linotype"),
            "custom_font_chinese": ("custom_font_chinese", str, "华文仿宋"),
            "text_bold": ("text_bold", bool, False),
            "toc_panel_visible": ("toc_panel_visible", bool, True),
            "filter_epub": ("filter_epub", bool, True),
            "filter_pdf": ("filter_pdf", bool, True),
            "filter_txt": ("filter_txt", bool, True),
        }
        for old_key, (new_key, cast, default) in key_map.items():
            val = old_settings.value(old_key, default)
            if cast is bool and isinstance(val, str):
                migrated[new_key] = val.lower() in ("true", "1", "yes", "on")
            else:
                try:
                    migrated[new_key] = cast(val)
                except Exception:
                    migrated[new_key] = default
        return migrated
    except Exception:
        return None


# =============================================================================
# TXT 数据模型与解析器（v1.6.0）
# =============================================================================
@dataclass
class TxtChapter:
    id: str
    title: str
    content: str
    order: int
    file_name: str = ""
    anchor: str = ""
    page_start: int = -1
    page_end: int = -1


@dataclass
class TxtBookInfo:
    title: str
    author: str
    cover_path: Optional[str] = None
    chapters: List[TxtChapter] = field(default_factory=list)
    images: dict = field(default_factory=dict)


class TxtParser:
    """TXT 文本解析器：自动识别常见编码，并按常见网文/出版目录格式拆分章节。"""

    _ENC_RE = re.compile(
        r'<\?xml[^>]*encoding\s*=\s*["\']([\w\-]+)["\']'
        r'|<meta[^>]*charset\s*=\s*["\']?\s*([\w\-]+)',
        re.IGNORECASE,
    )
    _CHAPTER_RE = re.compile(
        r'^\s{0,8}(?:'
        r'第\s*[0-9０-９零〇一二三四五六七八九十百千两]+\s*[章节回卷部篇折幕场集卷][^\n]{0,80}'
        r'|[0-9０-９]{1,4}\s*[\.、．]\s*[^\n]{1,80}'
        r'|Chapter\s+[0-9]+[^\n]{0,80}'
        r'|CHAPTER\s+[0-9]+[^\n]{0,80}'
        r'|楔子|序言|前言|引子|序章|终章|尾声|后记|番外(?:\s+[^\n]{1,80})?'
        r')\s*$',
        re.IGNORECASE,
    )
    _CHUNK_SIZE = 30000

    @staticmethod
    def _decode_bytes(raw: bytes) -> str:
        if isinstance(raw, str):
            return raw
        if not raw:
            return ""
        for bom, enc in ((b'\xef\xbb\xbf', 'utf-8-sig'),
                         (b'\xff\xfe\x00\x00', 'utf-32'),
                         (b'\x00\x00\xfe\xff', 'utf-32'),
                         (b'\xff\xfe', 'utf-16'),
                         (b'\xfe\xff', 'utf-16')):
            if raw.startswith(bom):
                try:
                    return raw.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    break
        head = raw[:1024].decode('ascii', errors='ignore')
        m = TxtParser._ENC_RE.search(head)
        if m:
            enc = (m.group(1) or m.group(2) or '').strip().lower()
            if enc:
                try:
                    return raw.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    pass
        for enc in ('utf-8', 'gb18030', 'big5', 'shift_jis', 'euc-kr', 'latin-1'):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode('utf-8', errors='replace')

    @staticmethod
    def _guess_title(file_path: str, text: str = "") -> str:
        stem = os.path.splitext(os.path.basename(file_path))[0].strip()
        if stem:
            return stem
        for line in (text or "").splitlines():
            line = line.strip()
            if line:
                return line[:60]
        return "未知标题"

    @staticmethod
    def read_metadata(file_path: str) -> Tuple[str, str]:
        title = TxtParser._guess_title(file_path)
        return title, "未知作者"

    @staticmethod
    def _clean_text(text: str) -> str:
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = ''.join(ch if (ch == '\n' or ch == '\t' or ord(ch) >= 32) else ' ' for ch in text)
        return text.strip()

    @staticmethod
    def _make_chunked_chapters(text: str, file_name: str) -> List[TxtChapter]:
        if len(text) <= TxtParser._CHUNK_SIZE:
            return [TxtChapter(id='txt_all', title='全文', content=text, order=0,
                               file_name=file_name)]

        paragraphs = re.split(r'\n\s*\n', text)
        chapters: List[TxtChapter] = []
        buf: List[str] = []
        size = 0
        for para in paragraphs:
            add = len(para) + 2
            if buf and size + add > TxtParser._CHUNK_SIZE:
                content = '\n\n'.join(buf).strip()
                if content:
                    chapters.append(TxtChapter(id=f'txt_part_{len(chapters)}',
                                               title=f'第 {len(chapters) + 1} 部分',
                                               content=content, order=len(chapters),
                                               file_name=file_name))
                buf, size = [], 0
            buf.append(para)
            size += add
        if buf:
            content = '\n\n'.join(buf).strip()
            if content:
                chapters.append(TxtChapter(id=f'txt_part_{len(chapters)}',
                                           title=f'第 {len(chapters) + 1} 部分',
                                           content=content, order=len(chapters),
                                           file_name=file_name))
        return chapters or [TxtChapter(id='txt_all', title='全文', content=text, order=0,
                                       file_name=file_name)]

    @staticmethod
    def _split_chapters(text: str, file_name: str) -> List[TxtChapter]:
        lines = text.splitlines(keepends=True)
        matches: List[Tuple[int, str]] = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or len(stripped) > 100:
                continue
            if stripped.endswith(('。', '，', ',', ';', '；', '！', '！', '?', '？')):
                continue
            if TxtParser._CHAPTER_RE.match(stripped):
                matches.append((i, stripped))

        if not matches:
            return TxtParser._make_chunked_chapters(text, file_name)

        chapters: List[TxtChapter] = []
        preamble = ''.join(lines[:matches[0][0]]).strip()
        if preamble:
            chapters.append(TxtChapter(id='txt_preamble', title='正文开始',
                                       content=preamble, order=0, file_name=file_name))

        for idx, (line_idx, title) in enumerate(matches):
            next_idx = matches[idx + 1][0] if idx + 1 < len(matches) else len(lines)
            content = ''.join(lines[line_idx + 1:next_idx]).strip()
            if not content and chapters:
                continue
            chapters.append(TxtChapter(id=f'txt_ch_{idx}', title=title,
                                       content=content, order=len(chapters),
                                       file_name=file_name))

        return [ch for ch in chapters if ch.content] or TxtParser._make_chunked_chapters(text, file_name)

    @staticmethod
    def parse_txt(file_path: str) -> TxtBookInfo:
        with open(file_path, 'rb') as f:
            raw = f.read()
        text = TxtParser._clean_text(TxtParser._decode_bytes(raw))
        title, author = TxtParser.read_metadata(file_path)
        chapters = TxtParser._split_chapters(text, os.path.basename(file_path))
        for i, ch in enumerate(chapters):
            ch.order = i
        return TxtBookInfo(title=title, author=author, chapters=chapters, images={})


# =============================================================================
# 后台扫描线程
# =============================================================================
class ScanThread(QThread):
    file_found = Signal(str, str, str)
    scan_finished = Signal(int)
    scan_error = Signal(str)

    def __init__(self, folder_paths, formats: set = None):
        super().__init__()
        # v1.6.2：支持一次扫描多个关联目录
        if isinstance(folder_paths, (list, tuple)):
            self.folder_paths = list(folder_paths)
        else:
            self.folder_paths = [folder_paths]
        self.formats = formats or {"epub", "pdf", "txt"}
        self._is_running = True

    def run(self):
        count = 0
        # v1.6.3.0：PyMuPDF 缺失提示改为每次扫描只发一次（此前放在 _scan_one
        # 中，按关联目录数重复弹出状态栏消息）
        if "pdf" in self.formats and not pdf_viewer.is_parser_available():
            self.scan_error.emit("未安装 PyMuPDF，无法扫描 PDF。请运行: pip install PyMuPDF")
        for folder in self.folder_paths:
            if not self._is_running:
                break
            count += self._scan_one(folder)
        self.scan_finished.emit(count)

    def _scan_one(self, folder_path: str) -> int:
        count = 0
        has_fitz = pdf_viewer.is_parser_available()

        try:
            for root, dirs, files in os.walk(folder_path):
                if not self._is_running:
                    break
                for i, file in enumerate(files):
                    if not self._is_running:
                        break
                    if i % 10 == 0:
                        self.msleep(1)

                    ext = file.lower().split('.')[-1] if '.' in file else ''
                    if ext not in self.formats:
                        continue

                    file_path = os.path.join(root, file)
                    try:
                        if ext == 'epub':
                            title, author = epub_viewer.EpubParser.read_metadata(file_path)
                        elif ext == 'txt':
                            title, author = TxtParser.read_metadata(file_path)
                        elif ext == 'pdf' and has_fitz:
                            title, author = pdf_viewer.PdfParser.read_metadata(file_path)
                        else:
                            continue
                        self.file_found.emit(file_path, title, author)
                        count += 1
                    except Exception as e:
                        self.scan_error.emit(f"无法解析 {file}: {str(e)}")
        except Exception as e:
            self.scan_error.emit(str(e))
        return count

    def stop(self):
        self._is_running = False


class BookLoadWorker(QThread):
    """v1.6.2：书籍解析后台线程。

    大文件 EPUB/TXT/PDF 的解析（PyMuPDF 封面、lxml/bs4 拆章等）不再阻塞
    UI 主循环，加载期间仅阅读区显示转圈占位页（LoadingWidget），窗口其余
    面板保持响应。
    """

    loaded = Signal(object, str)  # book_info, ext
    failed = Signal(str)

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path

    def run(self):
        ext = self.file_path.lower().split('.')[-1] if '.' in self.file_path else ''
        try:
            if ext == 'epub':
                book = epub_viewer.EpubParser.parse_epub(self.file_path)
            elif ext == 'txt':
                book = TxtParser.parse_txt(self.file_path)
            elif ext == 'pdf':
                book = pdf_viewer.PdfParser.parse_pdf(self.file_path)
            else:
                raise Exception(f"不支持的文件格式: {ext}")
            self.loaded.emit(book, ext)
        except Exception as e:
            self.failed.emit(str(e))


class LoadingWidget(QWidget):
    """v1.6.2：加载占位页（不确定进度条转圈 + 提示文字）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#252526"))
        self.setPalette(pal)

        lay = QVBoxLayout(self)
        lay.addStretch(1)
        bar_row = QHBoxLayout()
        bar_row.addStretch(1)
        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)  # 不确定模式：转圈动画
        self.spinner.setFixedSize(240, 6)
        self.spinner.setStyleSheet("""
            QProgressBar { border: none; background: #3e3e42; border-radius: 3px; }
            QProgressBar::chunk { background: #a78bfa; border-radius: 3px; }
        """)
        bar_row.addWidget(self.spinner)
        bar_row.addStretch(1)
        lay.addLayout(bar_row)
        self.msg = QLabel("正在加载书籍…")
        self.msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet("color: #858585; font-size: 13px; padding-top: 10px;")
        lay.addWidget(self.msg)
        lay.addStretch(2)

    def set_message(self, text: str):
        self.msg.setText(text)


# =============================================================================
# TXT 阅读器组件（QTextBrowser；EPUB 阅读器位于 lyuer_epubviewer.py）
# =============================================================================
class TxtReaderWidget(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)

        self.font_size = 18
        self.line_height = 1.8
        self.theme = "mien"
        self.custom_bg = None
        self.custom_fg = None
        self.custom_font_western = "Palatino Linotype"
        self.custom_font_chinese = "华文仿宋"
        self.text_bold_enabled = False
        self.current_chapter_file = ""

        # 触屏手势：捏合缩放字体 / 上下滑动滚动 / 左右滑动翻章
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
        self.viewport().grabGesture(Qt.GestureType.PinchGesture)
        self._touch_start = None
        self._touch_last = None
        self._pinch_acc = 1.0
        self._touch_scroll_total = 0.0
        self._pinch_active = False
        self._touch_elapsed = QElapsedTimer()

    def get_theme_colors(self):
        themes = {
            "light": {"bg": "#fafafa", "fg": "#333333"},
            "dark":  {"bg": "#1e1e1e", "fg": "#d4d4d4"},
            "sepia": {"bg": "#f4ecd8", "fg": "#5b4636"},
            "mien":  {"bg": "#252526", "fg": "#cccccc"},
        }
        t = themes.get(self.theme, themes["mien"])
        bg = self.custom_bg if self.custom_bg else t["bg"]
        fg = self.custom_fg if self.custom_fg else t["fg"]
        return bg, fg

    def update_style(self):
        themes = {
            "light": {"bg": "#fafafa", "fg": "#333333", "link": "#0066cc", "sel": "#0066cc", "sel_fg": "#ffffff"},
            "dark":  {"bg": "#1e1e1e", "fg": "#d4d4d4", "link": "#4da6ff", "sel": "#094771", "sel_fg": "#ffffff"},
            "sepia": {"bg": "#f4ecd8", "fg": "#5b4636", "link": "#8b4513", "sel": "#8b4513", "sel_fg": "#ffffff"},
            "mien":  {"bg": "#252526", "fg": "#cccccc", "link": "#a78bfa", "sel": "#37373d", "sel_fg": "#ffffff"},
        }
        t = themes.get(self.theme, themes["mien"])
        bg = self.custom_bg or t["bg"]
        fg = self.custom_fg or t["fg"]
        sel_fg = t.get("sel_fg", "#ffffff")

        self.setStyleSheet(f"""
            QTextBrowser {{
                background-color: {bg};
                color: {fg};
                border: 1px solid #3e3e42;
                border-radius: 6px;
                padding: 40px 60px;
                selection-background-color: {t['sel']};
                selection-color: {sel_fg};
            }}
            QScrollBar:vertical {{
                background: {bg};
                width: 10px;
                margin: 0px;
                border-radius: 5px;
            }}
            QScrollBar::handle:vertical {{
                background: #424242;
                min-height: 30px;
                border-radius: 5px;
            }}
            QScrollBar::handle:vertical:hover {{ background: #4f4f4f; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
        """)
        font = QFont(self.custom_font_western, self.font_size)
        font.setStyleHint(QFont.StyleHint.Serif)
        if self.text_bold_enabled:
            font.setWeight(QFont.Weight.Bold)
        self.setFont(font)
        doc = self.document()
        if doc:
            doc.setDefaultFont(font)

    def viewportEvent(self, event):
        """触屏手势：捏合缩放字号，上下滑动滚动，左右滑动翻章。"""
        t = event.type()
        if t == QEvent.Type.Gesture:
            gesture = event.gesture(Qt.GestureType.PinchGesture)
            if gesture is not None:
                state = gesture.state()
                if state == Qt.GestureState.GestureStarted:
                    self._pinch_active = True
                    self._touch_start = None
                    self._touch_last = None
                elif state in (Qt.GestureState.GestureFinished,
                               Qt.GestureState.GestureCanceled):
                    self._pinch_active = False
                    self._pinch_acc = 1.0
                if state != Qt.GestureState.GestureCanceled:
                    self._pinch_acc *= gesture.scaleFactor()
                    if self._pinch_acc >= 1.08:
                        self._pinch_acc = 1.0
                        mw = self.window()
                        if hasattr(mw, "change_font_size"):
                            mw.change_font_size(1)
                    elif self._pinch_acc <= 0.92:
                        self._pinch_acc = 1.0
                        mw = self.window()
                        if hasattr(mw, "change_font_size"):
                            mw.change_font_size(-1)
                return True
        elif t == QEvent.Type.TouchBegin:
            points = event.points()
            self._touch_elapsed.start()
            self._touch_scroll_total = 0.0
            if len(points) == 1 and not self._pinch_active:
                self._touch_start = points[0].position()
                self._touch_last = self._touch_start
            else:
                self._touch_start = None
                self._touch_last = None
            event.accept()
            return True
        elif t == QEvent.Type.TouchUpdate:
            points = event.points()
            if self._pinch_active or len(points) != 1:
                self._touch_last = None
            elif self._touch_last is not None:
                pos = points[0].position()
                delta = pos.y() - self._touch_last.y()
                sb = self.verticalScrollBar()
                sb.setValue(sb.value() - int(delta))
                self._touch_scroll_total += abs(delta)
                self._touch_last = pos
            event.accept()
            return True
        elif t == QEvent.Type.TouchCancel:
            self._touch_start = None
            self._touch_last = None
            event.accept()
            return True
        elif t == QEvent.Type.TouchEnd:
            if (self._touch_start is not None and self._touch_last is not None
                    and self._touch_elapsed.isValid()
                    and self._touch_elapsed.elapsed() < 350
                    and self._touch_scroll_total < 40):
                dx = self._touch_last.x() - self._touch_start.x()
                dy = self._touch_last.y() - self._touch_start.y()
                vw = max(1, self.viewport().width())
                vh = max(1, self.viewport().height())
                if abs(dx) > vw * 0.18 and abs(dx) > abs(dy) * 1.2:
                    mw = self.window()
                    if dx < 0 and hasattr(mw, "next_chapter"):
                        mw.next_chapter()
                    elif dx > 0 and hasattr(mw, "prev_chapter"):
                        mw.prev_chapter()
                else:
                    sb = self.verticalScrollBar()
                    if dy < -vh * 0.35:
                        sb.setValue(sb.value() + sb.pageStep())
                    elif dy > vh * 0.35:
                        sb.setValue(sb.value() - sb.pageStep())
            self._touch_start = None
            self._touch_last = None
            event.accept()
            return True
        return super().viewportEvent(event)

    @staticmethod
    def _text_to_html(text: str) -> str:
        paragraphs = re.split(r'\n\s*\n', (text or '').strip())
        blocks = []
        for para in paragraphs:
            escaped = html_module.escape(para).replace('\n', '<br>\n')
            blocks.append(f"<p>{escaped}</p>")
        return '\n'.join(blocks)

    def set_text_content(self, text: str):
        bg, fg = self.get_theme_colors()
        is_light = self.theme in ("light", "sepia")
        link_color = "#0066cc" if is_light else "#4da6ff" if self.theme == "dark" else "#a78bfa"
        blockquote_color = "#666666" if is_light else "#999999"
        code_bg = "#e8e8e8" if is_light else "#1e1e1e"
        base_weight = 600 if self.text_bold_enabled else 400
        bold_weight = 800 if self.text_bold_enabled else 700
        font_western = self.custom_font_western.replace('"', '')
        font_chinese = self.custom_font_chinese.replace('"', '')
        body_html = self._text_to_html(text)

        wrapped = f"""
        <html>
        <head>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: "{font_western}", "{font_chinese}", serif;
                font-size: {self.font_size}px;
                line-height: {self.line_height};
                color: {fg};
                background-color: {bg};
                font-weight: {base_weight};
            }}
            p {{ margin: 0.8em 0; text-align: justify; overflow-wrap: break-word; }}
            a {{ color: {link_color}; }}
            blockquote {{ border-left: 3px solid {link_color}; padding-left: 1em; color: {blockquote_color}; }}
            code {{ background: {code_bg}; padding: 2px 6px; border-radius: 4px; font-family: Consolas, monospace; }}
            strong, b {{ font-weight: {bold_weight}; }}
        </style>
        </head>
        <body>
        {body_html}
        </body>
        </html>
        """
        self.setHtml(wrapped)

    def scroll_ratio(self) -> float:
        sb = self.verticalScrollBar()
        if sb is None or sb.maximum() <= 0:
            return 0.0
        return max(0.0, min(1.0, sb.value() / max(1, sb.maximum())))

    def set_scroll_ratio(self, ratio: float):
        sb = self.verticalScrollBar()
        if sb is not None:
            sb.setValue(int(round(max(0.0, min(1.0, float(ratio))) * sb.maximum())))

    def clear(self):
        self.setHtml("")
        self.setPlaceholderText("将 EPUB / TXT / PDF 文件拖拽到此处，或从左侧书库打开")

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                path = urls[0].toLocalFile().lower()
                if path.endswith(('.epub', '.pdf', '.txt')):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            if file_path.lower().endswith(('.epub', '.pdf', '.txt')):
                main_window = self.window()
                if hasattr(main_window, 'open_book_file'):
                    event.acceptProposedAction()
                    try:
                        main_window.open_book_file(file_path)
                    except Exception:
                        pass


# =============================================================================
# 书库表头：高亮排序列 + ▲/▼ 方向指示（v1.6.2.1）
# =============================================================================
class SortableHeaderView(QHeaderView):
    """自定义表头。

    当前排序列以高亮底色 + 加粗主题色文字显示，并在文字右侧绘制 ▲（正序）
    或 ▼（倒序）；其余列保持默认暗色样式。
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.sort_column = 0
        self.sort_ascending = True

    def set_sort_indicator(self, column: int, ascending: bool) -> None:
        self.sort_column = column
        self.sort_ascending = ascending
        self.viewport().update()

    def paintSection(self, painter, rect, logicalIndex):
        model = self.model()
        text = ""
        if model is not None:
            v = model.headerData(logicalIndex, self.orientation(),
                                 Qt.ItemDataRole.DisplayRole)
            text = str(v) if v is not None else ""
        selected = logicalIndex == self.sort_column
        painter.save()
        painter.fillRect(rect,
                         QColor("#37373d") if selected else QColor("#252526"))
        font = painter.font()
        font.setBold(selected)
        painter.setFont(font)
        painter.setPen(QColor("#a78bfa") if selected else QColor("#cccccc"))
        if selected:
            text += "  ▲" if self.sort_ascending else "  ▼"
        painter.drawText(rect.adjusted(8, 0, -6, 0),
                         Qt.AlignmentFlag.AlignVCenter
                         | Qt.AlignmentFlag.AlignLeft, text)
        painter.setPen(QColor("#3e3e42"))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.restore()


# =============================================================================
# 触屏滚动列表控件：单指上下滑动 + 轻点选中（v1.6.2.1）
# =============================================================================
class _TouchScrollMixin:
    """触屏手势：单指上下滑动滚动视图；轻点仍触发 itemClicked 选中。

    注意：触点序列一旦被接受，Qt 便不再合成鼠标事件，轻点选中会失效；
    因此在 TouchEnd 里按「快速 + 几乎未移动」判定轻点并手动派发 itemClicked。
    """

    def _init_touch_scroll(self) -> None:
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
        self._touch_start = None
        self._touch_last = None
        self._touch_moved = 0.0
        self._touch_elapsed = QElapsedTimer()

    def _touch_scroll_event(self, event) -> bool:
        t = event.type()
        if t == QEvent.Type.TouchBegin:
            points = event.points()
            self._touch_elapsed.start()
            self._touch_moved = 0.0
            if len(points) == 1:
                self._touch_start = points[0].position()
                self._touch_last = self._touch_start
            else:
                self._touch_start = None
                self._touch_last = None
            event.accept()
            return True
        if t == QEvent.Type.TouchUpdate:
            points = event.points()
            if self._touch_last is not None and len(points) == 1:
                pos = points[0].position()
                delta = pos.y() - self._touch_last.y()
                sb = self.verticalScrollBar()
                sb.setValue(sb.value() - int(delta))
                self._touch_moved += abs(delta)
                self._touch_last = pos
            elif len(points) != 1:
                # 多指触点跳变时停用滚动基准，避免滚动条猛跳
                self._touch_last = None
            event.accept()
            return True
        if t == QEvent.Type.TouchEnd:
            if (self._touch_start is not None
                    and self._touch_elapsed.isValid()
                    and self._touch_elapsed.elapsed() < 300
                    and self._touch_moved < 10):
                self._emit_touch_tap(self._touch_start)
            self._touch_start = None
            self._touch_last = None
            event.accept()
            return True
        if t == QEvent.Type.TouchCancel:
            self._touch_start = None
            self._touch_last = None
            event.accept()
            return True
        return False

    def _emit_touch_tap(self, pos) -> None:
        raise NotImplementedError


class TouchScrollTreeWidget(_TouchScrollMixin, QTreeWidget):
    """书库树：支持触屏上下滑动，轻点选中并打开。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_touch_scroll()

    def viewportEvent(self, event):
        if self._touch_scroll_event(event):
            return True
        return super().viewportEvent(event)

    def _emit_touch_tap(self, pos) -> None:
        # v1.6.3.0 修复：触摸事件坐标与 itemAt/indexAt 同为 viewport 坐标系
        # （Qt 实测确认），此前误加表头高度，轻点实际命中偏下方一行处的条目，
        # 现直接使用触点的视口坐标。
        item = self.itemAt(QPoint(int(pos.x()), int(pos.y())))
        if item is not None:
            self.setCurrentItem(item)
            self.itemClicked.emit(item, 0)


class TouchScrollListWidget(_TouchScrollMixin, QListWidget):
    """章节列表：支持触屏上下滑动，轻点选中。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_touch_scroll()

    def viewportEvent(self, event):
        if self._touch_scroll_event(event):
            return True
        return super().viewportEvent(event)

    def _emit_touch_tap(self, pos) -> None:
        item = self.itemAt(QPoint(int(pos.x()), int(pos.y())))
        if item is not None:
            self.setCurrentItem(item)
            self.itemClicked.emit(item)


# =============================================================================
# 主窗口
# =============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MienArch Lyuer v1.6.3.0")
        self.setMinimumSize(1280, 840)

        self.current_book = None   # EpubBookInfo / TxtBookInfo / PdfBookInfo（鸭子类型）
        self.current_chapter_index = 0
        self.scan_thread: Optional[ScanThread] = None
        self.library = {}
        self.current_format = ""   # 当前打开文件的扩展名（epub/pdf/txt）
        self._sort_col = 0         # 书库排序列（默认第 0 列「文件名」正序）
        self._sort_order = Qt.SortOrder.AscendingOrder

        self.config_path = _get_config_path()
        self.settings = {}  # 内存中的配置字典
        self.load_settings()
        self.database_dir = self.settings.get("database_dir") or self._default_database_dir()
        self.db = lyuer_db.LibraryDatabase(self.database_dir)
        self._current_book_path = ""
        self._is_loading = False           # v1.6.2：后台解析期间为 True
        self._load_worker: Optional[BookLoadWorker] = None
        self._pending_open_path = ""
        self._prev_view = None
        self.init_ui()
        self.apply_theme()
        self.reload_library_from_database()

    # ------------------------------------------------------------------
    # Database helpers (v1.3.0): UI 仍负责解析/显示，持久化交给 lyuer_database.py
    # ------------------------------------------------------------------
    def _default_database_dir(self) -> str:
        return os.path.join(_get_app_dir(), "lyuer_database")

    def _database_file(self, database_dir: str) -> str:
        return os.path.join(database_dir, lyuer_db.DB_FILENAME)

    def choose_database_dir(self):
        current_dir = os.path.abspath(self.database_dir)
        folder = QFileDialog.getExistingDirectory(self, "选择 Lyuer 数据库目录", current_dir)
        if not folder:
            return
        new_dir = os.path.abspath(folder)
        if os.path.normcase(new_dir) == os.path.normcase(current_dir):
            return

        old_db = self._database_file(current_dir)
        new_db = self._database_file(new_dir)
        try:
            os.makedirs(new_dir, exist_ok=True)
            if self.db:
                # 先落盘并 checkpoint WAL，避免只复制主库文件时丢失最近事务。
                self.db.conn.commit()
                self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if os.path.exists(old_db) and not os.path.exists(new_db):
                shutil.copy2(old_db, new_db)
                # SQLite WAL/SHM 是缓存副产物；新位置从主库文件重建即可。
                for suffix in ("-wal", "-shm"):
                    try:
                        os.remove(old_db + suffix)
                    except OSError:
                        pass
        except Exception as e:
            QMessageBox.critical(self, "数据库迁移失败", f"无法迁移数据库：\n{e}")
            return

        self.database_dir = new_dir
        self.settings["database_dir"] = new_dir
        try:
            if self.db:
                self.db.close()
        except Exception:
            pass
        self.db = lyuer_db.LibraryDatabase(new_dir)
        self.reload_library_from_database()
        self.save_settings()
        self.statusBar().showMessage(f"数据库位置：{new_db}")

    def reload_library_from_database(self):
        self.tree_library.clear()
        self.library.clear()
        if not self.db:
            return
        records = self.db.all_books(include_missing=True)
        missing_count = 0
        for rec in records:
            # v1.6.2：按磁盘实际情况刷新缺失标记
            missing = rec.missing or not os.path.exists(rec.path)
            if missing != rec.missing:
                try:
                    self.db.mark_missing(rec.path, missing)
                except Exception:
                    pass
            if missing:
                missing_count += 1
            self.library[rec.path] = (rec.title, rec.author)
            self._add_library_tree_item(rec.path, rec.title, rec.author, rec.format,
                                        missing, rec.mtime, rec.size)
        self._apply_library_sort()
        missing_hint = f"，其中缺失 {missing_count} 本" if missing_count else ""
        self.lbl_status.setText(f"书库就绪：{len(records)} 本{missing_hint}")

    @staticmethod
    def _format_size(size: int) -> str:
        """把字节数格式化为易读大小，用于「大小」栏显示。"""
        n = int(size or 0)
        if n <= 0:
            return "—"
        if n < 1024:
            return f"{n} B"
        if n < 1024 ** 2:
            return f"{n / 1024:.1f} KB"
        if n < 1024 ** 3:
            return f"{n / 1024 ** 2:.1f} MB"
        return f"{n / 1024 ** 3:.2f} GB"

    def _add_library_tree_item(self, path: str, title: str, author: str, fmt: str = "",
                               missing: bool = False, mtime: float = 0.0,
                               size: int = 0):
        file_name = os.path.basename(path)
        title_text = ("[缺失] " if missing else "") + (title or "未知标题")
        author_text = author or "未知作者"
        if not mtime:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
        if not size and not missing:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        date_text = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "—"
        size_text = "—" if missing else self._format_size(size)
        # v1.6.2.1：列序「文件名 / 书名 / 作者 / 修改日期 / 大小」
        item = QTreeWidgetItem([file_name, title_text, author_text, date_text, size_text])
        item.setData(0, Qt.ItemDataRole.UserRole, path)
        item.setData(4, Qt.ItemDataRole.UserRole, int(size or 0))  # 原始字节数供排序
        prefix = "[缺失] " if missing else ""
        item.setToolTip(0, f"{prefix}{title_text}\n{author_text}\n{path}\n修改日期: {date_text}\n大小: {size_text}")
        self.tree_library.addTopLevelItem(item)
        return item

    def _ensure_library_item(self, path: str, title: str, author: str, fmt: str = ""):
        path = lyuer_db.normalize_path(path)
        self.library[path] = (title, author)
        for i in range(self.tree_library.topLevelItemCount()):
            item = self.tree_library.topLevelItem(i)
            if lyuer_db.normalize_path(item.data(0, Qt.ItemDataRole.UserRole) or "") == path:
                item.setText(0, os.path.basename(path))
                item.setText(1, title or "未知标题")
                item.setText(2, author or "未知作者")
                try:
                    size_now = os.path.getsize(path)
                except OSError:
                    size_now = 0
                item.setText(4, self._format_size(size_now))
                item.setData(4, Qt.ItemDataRole.UserRole, int(size_now))
                item.setToolTip(0, f"{title or '未知标题'}\n{author or '未知作者'}\n{path}")
                return item
        return self._add_library_tree_item(path, title, author, fmt, False)

    def current_book_path(self) -> str:
        return self._current_book_path

    def _is_pdf_active(self) -> bool:
        return (self.pdf_viewer is not None
                and self.view_stack.currentWidget() is self.pdf_viewer)

    def _active_text_viewer(self):
        """返回当前 EPUB/TXT 阅读器；PDF 模式返回 None。"""
        if self._is_pdf_active():
            return None
        if self.current_format == "epub" and self.epub_viewer is not None:
            return self.epub_viewer
        return self.txt_reader

    def _apply_reader_preferences(self, viewer):
        """把字体、主题与颜色设置同步到 EPUB/TXT 阅读器。"""
        if viewer is None:
            return
        viewer.theme = self.reader_theme
        viewer.font_size = self.reader_font_size
        viewer.custom_bg = self.reader_bg if self.reader_bg else None
        viewer.custom_fg = self.reader_fg if self.reader_fg else None
        viewer.custom_font_western = self.reader_font_western
        viewer.custom_font_chinese = self.reader_font_chinese
        viewer.text_bold_enabled = self.reader_text_bold
        viewer.update_style()

    def _update_text_viewer_styles(self):
        for viewer in (getattr(self, "epub_viewer", None), getattr(self, "txt_reader", None)):
            self._apply_reader_preferences(viewer)

    def _save_reading_position(self):
        if not self.db or not self._current_book_path:
            return

        if self._is_pdf_active():
            # PDF：按当前所在页反推章节，并保存滚动比例
            if not self.current_book or not self.current_book.chapters:
                return
            cur_page = self.pdf_viewer.current_page()
            ch_idx = 0
            for i, ch in enumerate(self.current_book.chapters):
                if ch.page_start <= cur_page:
                    ch_idx = i
                else:
                    break
            try:
                self.db.set_reading_position(
                    self._current_book_path, ch_idx, self.pdf_viewer.scroll_ratio())
            except Exception:
                pass
            return

        viewer = self._active_text_viewer()
        ratio = viewer.scroll_ratio() if viewer is not None else 0.0
        try:
            self.db.set_reading_position(self._current_book_path, self.current_chapter_index, ratio)
        except Exception:
            pass

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(self.splitter)

        # ===== 左侧：文件库 =====
        left_panel = QWidget()
        left_panel.setObjectName("leftPanel")
        self.left_panel = left_panel
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(10, 10, 10, 6)
        left_layout.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.btn_add_folder = QPushButton("➕ 添加目录")
        self.btn_add_folder.setToolTip("扫描并关联新文件夹，书籍追加到书库（可累积多个目录）")
        self.btn_add_folder.clicked.connect(self.add_folder_association)
        self._style_action_btn(self.btn_add_folder)

        self.btn_remove_folder = QPushButton("🗂 管理关联")
        self.btn_remove_folder.setToolTip("管理已关联的文件夹（可选择移除某个目录及其书籍记录，不删除磁盘文件）")
        self.btn_remove_folder.clicked.connect(self.remove_folder_association)
        self._style_action_btn(self.btn_remove_folder)

        self.btn_open_file = QPushButton("📖 打开文件")
        self.btn_open_file.setToolTip("打开单个 EPUB/TXT/PDF 文件")
        self.btn_open_file.clicked.connect(self.open_file_dialog)
        self._style_action_btn(self.btn_open_file)

        toolbar.addWidget(self.btn_add_folder)
        toolbar.addWidget(self.btn_remove_folder)
        toolbar.addWidget(self.btn_open_file)
        left_layout.addLayout(toolbar)

        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText(" 筛选书名或作者...")
        self.edit_search.textChanged.connect(self.filter_library)
        self.edit_search.setStyleSheet("""
            QLineEdit {
                background-color: #3c3c3c;
                color: #cccccc;
                border: 1px solid #3e3e42;
                border-radius: 4px;
                padding: 6px 10px;
                font-size: 13px;
            }
            QLineEdit:focus {
                border: 1px solid #a78bfa;
            }
        """)
        left_layout.addWidget(self.edit_search)

        self.lbl_status = QLabel("就绪 - 拖拽 EPUB/TXT/PDF 文件到阅读区")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color: #858585; font-size: 11px; padding: 2px 4px;")
        left_layout.addWidget(self.lbl_status)

        self.tree_library = TouchScrollTreeWidget()
        # v1.6.2.1：列序对调为「文件名 / 书名 / 作者 / 修改日期 / 大小」
        self.tree_library.setHeaderLabels(["文件名", "书名", "作者", "修改日期", "大小"])
        self.tree_library.setHeader(SortableHeaderView(self.tree_library))
        self.tree_library.setColumnWidth(0, 190)
        self.tree_library.setColumnWidth(1, 150)
        self.tree_library.setColumnWidth(2, 100)
        self.tree_library.setColumnWidth(3, 110)
        self.tree_library.setColumnWidth(4, 80)
        self.tree_library.setAlternatingRowColors(True)
        self.tree_library.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree_library.itemClicked.connect(self.on_library_item_clicked)
        self.tree_library.header().setSectionsClickable(True)
        self.tree_library.header().setHighlightSections(True)
        self.tree_library.header().sectionClicked.connect(self.on_library_header_clicked)
        self.tree_library.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_library.customContextMenuRequested.connect(self.show_library_context_menu)
        self.tree_library.setStyleSheet("""
            QTreeWidget {
                background-color: #1e1e1e;
                color: #cccccc;
                border: 1px solid #3e3e42;
                border-radius: 6px;
                outline: none;
                padding: 4px;
            }
            QTreeWidget::item {
                padding: 6px 4px;
                border-radius: 4px;
            }
            QTreeWidget::item:selected {
                background-color: #37373d;
                color: #ffffff;
            }
            QTreeWidget::item:hover {
                background-color: #2a2d2e;
            }
            QHeaderView::section {
                background-color: #252526;
                color: #cccccc;
                padding: 6px;
                border: none;
                border-bottom: 1px solid #3e3e42;
            }
            QHeaderView::section:hover {
                background-color: #37373d;
                color: #a78bfa;
            }
        """)
        left_layout.addWidget(self.tree_library)

        self.lbl_copyright = QLabel("© 2026 MienArch Lyuer v1.6.3.0 2026.Ulia")
        self.lbl_copyright.setStyleSheet("color: #555555; font-size: 10px; padding: 4px 2px;")
        self.lbl_copyright.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        left_layout.addWidget(self.lbl_copyright)

        self.splitter.addWidget(left_panel)
        self.splitter.setCollapsible(0, False)

        # ===== 中间：章节列表 =====
        mid_panel = QWidget()
        mid_panel.setObjectName("midPanel")
        self.mid_panel = mid_panel
        mid_layout = QVBoxLayout(mid_panel)
        mid_layout.setContentsMargins(8, 10, 8, 6)
        mid_layout.setSpacing(8)

        self.lbl_book_title = QLabel("未打开书籍")
        self.lbl_book_title.setWordWrap(True)
        self.lbl_book_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #cccccc; padding: 4px;")
        mid_layout.addWidget(self.lbl_book_title)

        self.list_chapters = TouchScrollListWidget()
        self.list_chapters.itemClicked.connect(self.on_chapter_clicked)
        self.list_chapters.setAlternatingRowColors(True)
        self.list_chapters.setStyleSheet("""
            QListWidget {
                background-color: #1e1e1e;
                color: #cccccc;
                border: 1px solid #3e3e42;
                border-radius: 6px;
                outline: none;
                padding: 4px;
            }
            QListWidget::item {
                padding: 8px 6px;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #37373d;
                color: #ffffff;
            }
            QListWidget::item:hover {
                background-color: #2a2d2e;
            }
        """)
        mid_layout.addWidget(self.list_chapters)

        self.splitter.addWidget(mid_panel)
        self.splitter.setCollapsible(1, False)

        # ===== 右侧：阅读器 =====
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 10, 6)
        right_layout.setSpacing(8)

        # v1.6.0：EPUB 使用 Qt WebEngine，TXT 使用轻量文本阅读器，PDF 使用 QtPdf
        self.epub_viewer = None
        try:
            if epub_viewer.is_epub_available():
                self.epub_viewer = epub_viewer.EpubViewerWidget()
                # v1.6.1：EPUB 正文/目录页链接跳转（Qt WebEngine 导航拦截）
                self.epub_viewer.navigateToChapter.connect(self._on_epub_navigate)
                # v1.6.2.1：EPUB 触屏捏合映射为字号调整（见 lyuer_epubviewer v1.0.2/1.0.3）
                self.epub_viewer.fontZoomRequested.connect(self.change_font_size)
        except Exception:
            self.epub_viewer = None
        self.txt_reader = TxtReaderWidget()

        reader_toolbar = QHBoxLayout()
        reader_toolbar.setContentsMargins(2, 2, 2, 2)
        reader_toolbar.setSpacing(4)

        self.btn_prev = QPushButton("◀ 上一章")
        self.btn_prev.clicked.connect(self.prev_chapter)
        self.btn_prev.setEnabled(False)
        self._style_nav_btn(self.btn_prev)

        self.btn_next = QPushButton("下一章 ▶")
        self.btn_next.clicked.connect(self.next_chapter)
        self.btn_next.setEnabled(False)
        self._style_nav_btn(self.btn_next)

        self.btn_font_dec = QPushButton("A-")
        self.btn_font_dec.setFixedWidth(42)
        self.btn_font_dec.clicked.connect(lambda: self.change_font_size(-1))
        self._style_small_btn(self.btn_font_dec)

        self.btn_font_inc = QPushButton("A+")
        self.btn_font_inc.setFixedWidth(42)
        self.btn_font_inc.clicked.connect(lambda: self.change_font_size(1))
        self._style_small_btn(self.btn_font_inc)

        self.combo_theme = QComboBox()
        self.combo_theme.addItems(["黑色", "白色", "自然", "深灰"])
        self.combo_theme.setFixedWidth(90)
        self.combo_theme.currentIndexChanged.connect(self.on_theme_changed)
        self.combo_theme.setStyleSheet("""
            QComboBox {
                background-color: #3c3c3c;
                color: #cccccc;
                border: 1px solid #3e3e42;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QComboBox:focus {
                border: 1px solid #a78bfa;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox QAbstractItemView {
                background-color: #252526;
                color: #cccccc;
                selection-background-color: #37373d;
                border: 1px solid #3e3e42;
            }
        """)

        self.btn_pick_bg = QPushButton("🎨")
        self.btn_pick_bg.setToolTip("自定义阅读背景色")
        self.btn_pick_bg.setFixedWidth(36)
        self.btn_pick_bg.clicked.connect(self.pick_background_color)
        self._style_small_btn(self.btn_pick_bg)

        self.btn_pick_fg = QPushButton("✎")
        self.btn_pick_fg.setToolTip("自定义阅读文字颜色")
        self.btn_pick_fg.setFixedWidth(36)
        self.btn_pick_fg.clicked.connect(self.pick_text_color)
        self._style_small_btn(self.btn_pick_fg)

        self.btn_reset_color = QPushButton("↺")
        self.btn_reset_color.setToolTip("重置为默认颜色")
        self.btn_reset_color.setFixedWidth(36)
        self.btn_reset_color.clicked.connect(self.reset_colors)
        self._style_small_btn(self.btn_reset_color)

        font_combo_style = """
            QFontComboBox {
                background-color: #3c3c3c;
                color: #cccccc;
                border: 1px solid #3e3e42;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QFontComboBox:focus {
                border: 1px solid #a78bfa;
            }
            QFontComboBox::drop-down {
                border: none;
            }
            QFontComboBox QAbstractItemView {
                background-color: #252526;
                color: #cccccc;
                selection-background-color: #37373d;
                border: 1px solid #3e3e42;
            }
        """

        self.combo_font_western = QFontComboBox()
        self.combo_font_western.setCurrentFont(QFont(self.reader_font_western))
        self.combo_font_western.currentFontChanged.connect(self.on_western_font_changed)
        self.combo_font_western.setFixedWidth(170)
        self.combo_font_western.setStyleSheet(font_combo_style)

        self.combo_font_chinese = QFontComboBox()
        self.combo_font_chinese.setCurrentFont(QFont(self.reader_font_chinese))
        self.combo_font_chinese.currentFontChanged.connect(self.on_chinese_font_changed)
        self.combo_font_chinese.setFixedWidth(120)
        self.combo_font_chinese.setStyleSheet(font_combo_style)

        self.btn_bold = QPushButton("B")
        self.btn_bold.setToolTip("加粗文本")
        self.btn_bold.setFixedWidth(36)
        self.btn_bold.setCheckable(True)
        self.btn_bold.clicked.connect(self.toggle_text_bold)
        self._style_small_btn(self.btn_bold)

        reader_toolbar.setSpacing(4)
        reader_toolbar.addWidget(self.btn_prev)
        reader_toolbar.addWidget(self.btn_next)
        reader_toolbar.addSpacing(6)
        reader_toolbar.addWidget(self.btn_font_dec)
        reader_toolbar.addWidget(self.btn_font_inc)
        reader_toolbar.addSpacing(6)
        reader_toolbar.addWidget(self.combo_theme)
        reader_toolbar.addSpacing(6)
        reader_toolbar.addWidget(self.btn_pick_bg)
        reader_toolbar.addWidget(self.btn_pick_fg)
        reader_toolbar.addWidget(self.btn_reset_color)
        reader_toolbar.addSpacing(6)
        reader_toolbar.addWidget(self.combo_font_western)
        reader_toolbar.addWidget(self.combo_font_chinese)
        reader_toolbar.addWidget(self.btn_bold)
        reader_toolbar.addStretch()

        right_layout.addLayout(reader_toolbar)

        self.txt_reader.setPlaceholderText("将 EPUB / TXT / PDF 文件拖拽到此处，或点击左侧「打开文件」按钮")

        self.view_stack = QStackedWidget()
        if self.epub_viewer is not None:
            self.view_stack.addWidget(self.epub_viewer)
        self.view_stack.addWidget(self.txt_reader)
        self.pdf_viewer = None
        try:
            self.pdf_viewer = pdf_viewer.PdfViewerWidget()
            self.pdf_viewer.pageChanged.connect(self.on_pdf_page_changed)
            self.view_stack.addWidget(self.pdf_viewer)
        except Exception:
            self.pdf_viewer = None
        right_layout.addWidget(self.view_stack)

        # v1.6.2：后台解析期间切换到加载占位页，仅阅读区显示转圈
        self.loading_page = LoadingWidget()
        self.view_stack.addWidget(self.loading_page)

        self.lbl_chapter_title = QLabel("")
        self.lbl_chapter_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_chapter_title.setStyleSheet("padding: 4px; color: #858585; font-size: 12px;")
        right_layout.addWidget(self.lbl_chapter_title)

        self.splitter.addWidget(right_panel)
        self.splitter.setSizes([300, 240, 740])

        self.create_menu_bar()
        self.statusBar().showMessage("就绪")
        self.statusBar().setStyleSheet("color: #858585; background-color: #1e1e1e; border-top: 1px solid #3e3e42;")
        self.setAcceptDrops(True)
        self._setup_arrow_shortcuts()

        # 设置窗口图标
        icon_path = _get_icon_path()
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

    def _style_action_btn(self, btn: QPushButton):
        btn.setStyleSheet("""
            QPushButton {
                background-color: #0e639c;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 7px 14px;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #1177bb;
            }
            QPushButton:pressed {
                background-color: #094771;
            }
        """)

    def _style_nav_btn(self, btn: QPushButton):
        btn.setStyleSheet("""
            QPushButton {
                background-color: #3c3c3c;
                color: #cccccc;
                border: 1px solid #3e3e42;
                border-radius: 4px;
                padding: 6px 14px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
                border: 1px solid #a78bfa;
            }
            QPushButton:pressed {
                background-color: #37373d;
            }
            QPushButton:disabled {
                background-color: #2d2d30;
                color: #555555;
                border: 1px solid #3e3e42;
            }
        """)

    def _style_small_btn(self, btn: QPushButton):
        btn.setStyleSheet("""
            QPushButton {
                background-color: #3c3c3c;
                color: #cccccc;
                border: 1px solid #3e3e42;
                border-radius: 4px;
                padding: 5px 10px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
                border: 1px solid #a78bfa;
            }
            QPushButton:pressed {
                background-color: #37373d;
            }
            QPushButton:checked {
                background-color: #a78bfa;
                color: #1e1e1e;
                border: 1px solid #a78bfa;
                font-weight: bold;
            }
        """)

    # ------------------------------------------------------------------
    # 书库排序 & 键盘导航（v1.5.0）
    # ------------------------------------------------------------------
    def on_library_header_clicked(self, col: int):
        """点击栏标题排序；再次点击同一栏切换正序/倒序。"""
        if self._sort_col == col:
            self._sort_order = (Qt.SortOrder.DescendingOrder
                                if self._sort_order == Qt.SortOrder.AscendingOrder
                                else Qt.SortOrder.AscendingOrder)
        else:
            self._sort_col = col
            self._sort_order = Qt.SortOrder.AscendingOrder
        self._sort_library_items()
        names = {0: "文件名", 1: "书名", 2: "作者", 3: "修改日期", 4: "大小"}
        order = "正序" if self._sort_order == Qt.SortOrder.AscendingOrder else "倒序"
        self.statusBar().showMessage(f"书库已按「{names.get(col, str(col))}」{order}排序")

    def _apply_library_sort(self):
        """扫描/恢复书库后沿用用户已选择的排序方式（默认文件名正序）。"""
        self._sort_library_items()

    def _sort_library_items(self):
        """按 _sort_col 手工排序并同步表头指示（高亮 + ▲/▼）。

        QTreeWidget.sortItems 只按列文本做字典序比较，「大小」栏无法正确
        比较（如 9 MB > 10 MB 会排错），因此取出顶层项按 key 排序后重新
        插入；原始字节数存在大小列的 UserRole 中。
        """
        if self._sort_col < 0:
            return
        col = self._sort_col
        count = self.tree_library.topLevelItemCount()
        items = [self.tree_library.takeTopLevelItem(0) for _ in range(count)]
        if col == 4:
            key = lambda it: (it.data(4, Qt.ItemDataRole.UserRole) or 0)
        else:
            key = lambda it: (it.text(col) or "").lower()
        items.sort(key=key,
                   reverse=(self._sort_order == Qt.SortOrder.DescendingOrder))
        for it in items:
            self.tree_library.addTopLevelItem(it)
        header = self.tree_library.header()
        if isinstance(header, SortableHeaderView):
            header.set_sort_indicator(
                col, self._sort_order == Qt.SortOrder.AscendingOrder)

    def _setup_arrow_shortcuts(self):
        """←→ 上一章/下一章（PDF 为上一页/下一页），↑↓ 上下滚动一页。"""
        def _sc(key, slot):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(lambda: self._arrow_guard() and slot())
        _sc(Qt.Key.Key_Left, self.prev_chapter)
        _sc(Qt.Key.Key_Right, self.next_chapter)
        _sc(Qt.Key.Key_Up, lambda: self._scroll_by_page(-1))
        _sc(Qt.Key.Key_Down, lambda: self._scroll_by_page(1))

    def _arrow_guard(self) -> bool:
        """输入框/下拉框获得焦点或后台解析进行中时，方向键不触发翻页。"""
        if self._is_loading:
            return False
        w = QApplication.focusWidget()
        if w is None:
            return True
        if isinstance(w, (QLineEdit, QComboBox, QFontComboBox)):
            return False
        parent = w.parentWidget()
        if parent is not None and isinstance(parent, (QLineEdit, QComboBox, QFontComboBox)):
            return False
        return True

    def _scroll_by_page(self, direction: int):
        sb = None
        if self._is_pdf_active():
            sb = self.pdf_viewer.pdf_view.verticalScrollBar()
        else:
            viewer = self._active_text_viewer()
            if isinstance(viewer, TxtReaderWidget):
                sb = viewer.verticalScrollBar()
            elif viewer is not None:
                # Qt WebEngine：用键盘翻页高度滚动当前章节
                try:
                    step = max(1, int(viewer.height() * 0.9))
                    viewer.page().runJavaScript(
                        f"window.scrollBy(0, {direction * step});")
                except Exception:
                    pass
                return
        if sb is not None:
            sb.setValue(sb.value() + direction * sb.pageStep())

    def create_menu_bar(self):
        menubar = self.menuBar()
        menubar.setStyleSheet("""
            QMenuBar {
                background-color: #1e1e1e;
                color: #cccccc;
                border-bottom: 1px solid #3e3e42;
            }
            QMenuBar::item:selected {
                background-color: #37373d;
                color: #ffffff;
            }
            QMenu {
                background-color: #252526;
                color: #cccccc;
                border: 1px solid #3e3e42;
            }
            QMenu::item:selected {
                background-color: #094771;
                color: #ffffff;
            }
        """)

        file_menu = menubar.addMenu("文件")
        add_folder_action = QAction("添加目录…", self)
        add_folder_action.setShortcut(QKeySequence("Ctrl+Shift+O"))
        add_folder_action.setToolTip("扫描并关联新文件夹，书籍追加到书库")
        add_folder_action.triggered.connect(self.add_folder_association)
        file_menu.addAction(add_folder_action)

        remove_folder_action = QAction("管理关联目录…", self)
        remove_folder_action.setToolTip("移除已关联目录及其书籍记录")
        remove_folder_action.triggered.connect(self.remove_folder_association)
        file_menu.addAction(remove_folder_action)

        rescan_action = QAction("重新扫描全部关联目录", self)
        rescan_action.setShortcut(QKeySequence("F5"))
        rescan_action.triggered.connect(self.rescan_all_folders)
        file_menu.addAction(rescan_action)

        open_file_action = QAction("打开文件", self)
        open_file_action.setShortcut(QKeySequence("Ctrl+O"))
        open_file_action.triggered.connect(self.open_file_dialog)
        file_menu.addAction(open_file_action)

        file_menu.addSeparator()

        # 格式筛选移入菜单
        format_menu = file_menu.addMenu("格式筛选")
        self.action_filter_epub = QAction("EPUB", self)
        self.action_filter_epub.setCheckable(True)
        self.action_filter_epub.setChecked(self.filter_epub)
        self.action_filter_epub.triggered.connect(self.on_filter_epub_changed)
        format_menu.addAction(self.action_filter_epub)

        self.action_filter_pdf = QAction("PDF", self)
        self.action_filter_pdf.setCheckable(True)
        self.action_filter_pdf.setChecked(self.filter_pdf)
        self.action_filter_pdf.triggered.connect(self.on_filter_pdf_changed)
        format_menu.addAction(self.action_filter_pdf)

        self.action_filter_txt = QAction("TXT", self)
        self.action_filter_txt.setCheckable(True)
        self.action_filter_txt.setChecked(self.filter_txt)
        self.action_filter_txt.triggered.connect(self.on_filter_txt_changed)
        format_menu.addAction(self.action_filter_txt)

        file_menu.addSeparator()

        choose_db_action = QAction("数据库位置...", self)
        choose_db_action.setToolTip("选择或迁移 SQLite 书库目录")
        choose_db_action.triggered.connect(self.choose_database_dir)
        file_menu.addAction(choose_db_action)

        file_menu.addSeparator()

        exit_action = QAction("退出", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        read_menu = menubar.addMenu("选项")
        self.toggle_left_action = QAction("显示文件面板", self)
        self.toggle_left_action.setCheckable(True)
        self.toggle_left_action.setChecked(True)
        self.toggle_left_action.setShortcut(QKeySequence("Ctrl+L"))
        self.toggle_left_action.triggered.connect(self.toggle_left_panel)
        read_menu.addAction(self.toggle_left_action)

        self.toggle_toc_action = QAction("显示目录面板", self)
        self.toggle_toc_action.setCheckable(True)
        self.toggle_toc_action.setChecked(True)
        self.toggle_toc_action.setShortcut(QKeySequence("Ctrl+T"))
        self.toggle_toc_action.triggered.connect(self.toggle_toc_panel)
        read_menu.addAction(self.toggle_toc_action)

        read_menu.addSeparator()

        prev_action = QAction("上一章", self)
        prev_action.setShortcut(QKeySequence("Ctrl+Left"))
        prev_action.triggered.connect(self.prev_chapter)
        read_menu.addAction(prev_action)

        next_action = QAction("下一章", self)
        next_action.setShortcut(QKeySequence("Ctrl+Right"))
        next_action.triggered.connect(self.next_chapter)
        read_menu.addAction(next_action)

        read_menu.addSeparator()

        inc_font_action = QAction("增大字体", self)
        inc_font_action.setShortcut(QKeySequence("Ctrl+="))
        inc_font_action.triggered.connect(lambda: self.change_font_size(1))
        read_menu.addAction(inc_font_action)

        dec_font_action = QAction("减小字体", self)
        dec_font_action.setShortcut(QKeySequence("Ctrl+-"))
        dec_font_action.triggered.connect(lambda: self.change_font_size(-1))
        read_menu.addAction(dec_font_action)

    def load_settings(self):
        defaults = {
            "font_size": 18,
            "theme": "mien",
            "custom_bg": "",
            "custom_fg": "",
            "custom_font_western": "Palatino Linotype",
            "custom_font_chinese": "华文仿宋",
            "text_bold": False,
            "toc_panel_visible": True,
            "filter_epub": True,
            "filter_pdf": True,
            "filter_txt": True,
            "database_dir": "",
            "library_folders": [],
        }

        loaded = None
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception:
                loaded = None

        # 若 JSON 不存在，尝试从旧版 QSettings 迁移一次
        if loaded is None:
            migrated = _migrate_qsettings_to_json(self.config_path)
            if migrated is not None:
                loaded = migrated
                try:
                    with open(self.config_path, "w", encoding="utf-8") as f:
                        json.dump(loaded, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

        if not isinstance(loaded, dict):
            loaded = {}
        self.settings = {**defaults, **loaded}

        try:
            self.reader_font_size = int(self.settings.get("font_size", defaults["font_size"]))
        except (TypeError, ValueError):
            self.reader_font_size = defaults["font_size"]

        self.reader_theme = self.settings.get("theme", defaults["theme"])
        if not isinstance(self.reader_theme, str):
            self.reader_theme = defaults["theme"]

        self.reader_bg = self.settings.get("custom_bg", defaults["custom_bg"])
        if not isinstance(self.reader_bg, str):
            self.reader_bg = defaults["custom_bg"]

        self.reader_fg = self.settings.get("custom_fg", defaults["custom_fg"])
        if not isinstance(self.reader_fg, str):
            self.reader_fg = defaults["custom_fg"]

        self.reader_font_western = self.settings.get("custom_font_western", defaults["custom_font_western"])
        if not isinstance(self.reader_font_western, str):
            self.reader_font_western = defaults["custom_font_western"]

        self.reader_font_chinese = self.settings.get("custom_font_chinese", defaults["custom_font_chinese"])
        if not isinstance(self.reader_font_chinese, str):
            self.reader_font_chinese = defaults["custom_font_chinese"]

        val = self.settings.get("text_bold", defaults["text_bold"])
        if isinstance(val, str):
            self.reader_text_bold = val.lower() in ("true", "1", "yes", "on")
        else:
            self.reader_text_bold = bool(val)

        val_toc = self.settings.get("toc_panel_visible", defaults["toc_panel_visible"])
        if isinstance(val_toc, str):
            self.toc_panel_visible = val_toc.lower() in ("true", "1", "yes", "on")
        else:
            self.toc_panel_visible = bool(val_toc)

        val_epub = self.settings.get("filter_epub", defaults["filter_epub"])
        self.filter_epub = val_epub.lower() in ("true", "1", "yes", "on") if isinstance(val_epub, str) else bool(val_epub)
        val_pdf = self.settings.get("filter_pdf", defaults["filter_pdf"])
        self.filter_pdf = val_pdf.lower() in ("true", "1", "yes", "on") if isinstance(val_pdf, str) else bool(val_pdf)
        val_txt = self.settings.get("filter_txt", defaults["filter_txt"])
        self.filter_txt = val_txt.lower() in ("true", "1", "yes", "on") if isinstance(val_txt, str) else bool(val_txt)

        val_folders = self.settings.get("library_folders", defaults["library_folders"])
        if isinstance(val_folders, list):
            self.library_folders = [str(p) for p in val_folders if str(p).strip()]
        else:
            self.library_folders = []

    def save_settings(self):
        self.settings["font_size"] = self.txt_reader.font_size
        self.settings["theme"] = self.reader_theme
        self.settings["custom_bg"] = self.txt_reader.custom_bg or ""
        self.settings["custom_fg"] = self.txt_reader.custom_fg or ""
        self.settings["custom_font_western"] = self.txt_reader.custom_font_western
        self.settings["custom_font_chinese"] = self.txt_reader.custom_font_chinese
        self.settings["text_bold"] = bool(self.txt_reader.text_bold_enabled)
        self.settings["toc_panel_visible"] = bool(self.mid_panel.isVisible())
        self.settings["filter_epub"] = bool(self.filter_epub)
        self.settings["filter_pdf"] = bool(self.filter_pdf)
        self.settings["filter_txt"] = bool(self.filter_txt)
        self.settings["database_dir"] = os.path.abspath(self.database_dir)

        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def apply_theme(self):
        theme_map = {"light": 1, "dark": 0, "sepia": 2, "mien": 3}
        idx = theme_map.get(self.reader_theme, 3)
        # v1.6.3.0 修复：setCurrentIndex 会同步触发 on_theme_changed——后者把
        # reader_bg/reader_fg 清空并 save_settings 写回配置，导致主题非默认
        # 「黑色」（索引 0）的用户每次重启后自定义颜色被静默抹掉。此处仅恢复
        # 下拉框显示位置，必须屏蔽信号。
        self.combo_theme.blockSignals(True)
        try:
            self.combo_theme.setCurrentIndex(idx)
        finally:
            self.combo_theme.blockSignals(False)
        self._update_text_viewer_styles()

        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#cccccc"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#252526"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#2d2d30"))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#252526"))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#cccccc"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#cccccc"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#3c3c3c"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#cccccc"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#094771"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        self.setPalette(palette)

        self.combo_font_western.blockSignals(True)
        self.combo_font_chinese.blockSignals(True)
        self.btn_bold.blockSignals(True)
        try:
            self.combo_font_western.setCurrentFont(QFont(self.reader_font_western))
            self.combo_font_chinese.setCurrentFont(QFont(self.reader_font_chinese))
            self.btn_bold.setChecked(self.reader_text_bold)
        finally:
            self.combo_font_western.blockSignals(False)
            self.combo_font_chinese.blockSignals(False)
            self.btn_bold.blockSignals(False)

        self.toggle_toc_action.setChecked(self.toc_panel_visible)
        if not self.toc_panel_visible:
            if self.mid_panel.isVisible():
                self._mid_panel_width = self.splitter.sizes()[1]
            self.mid_panel.setVisible(False)

        # 同步菜单中的格式筛选状态
        if hasattr(self, 'action_filter_epub'):
            self.action_filter_epub.setChecked(self.filter_epub)
        if hasattr(self, 'action_filter_pdf'):
            self.action_filter_pdf.setChecked(self.filter_pdf)
        if hasattr(self, 'action_filter_txt'):
            self.action_filter_txt.setChecked(self.filter_txt)

    def refresh_reader_display(self):
        if self._is_pdf_active():
            return  # PDF 由浏览器式阅读器自行管理渲染
        if not self.current_book or self.current_chapter_index < 0:
            return
        if self.current_chapter_index >= len(self.current_book.chapters):
            return

        viewer = self._active_text_viewer()
        if viewer is None:
            return
        chapter = self.current_book.chapters[self.current_chapter_index]
        scroll_ratio = viewer.scroll_ratio()

        if self.current_format == "epub":
            viewer.set_book_images(self.current_book.images)
            viewer.current_chapter_file = chapter.file_name
            viewer.set_chapter_content(chapter.content)
        else:
            viewer.current_chapter_file = chapter.file_name
            viewer.set_text_content(chapter.content)

        viewer.set_scroll_ratio(scroll_ratio)

    def on_theme_changed(self, index):
        themes = ["dark", "light", "sepia", "mien"]
        self.reader_theme = themes[index]
        self.reader_bg = ""
        self.reader_fg = ""
        self._update_text_viewer_styles()
        self.refresh_reader_display()
        self.save_settings()

    def pick_background_color(self):
        color = QColorDialog.getColor(QColor(self.reader_bg or "#252526"), self, "选择阅读背景色")
        if color.isValid():
            self.reader_bg = color.name()
            self._update_text_viewer_styles()
            self.refresh_reader_display()
            self.save_settings()

    def pick_text_color(self):
        color = QColorDialog.getColor(QColor(self.reader_fg or "#cccccc"), self, "选择阅读文字颜色")
        if color.isValid():
            self.reader_fg = color.name()
            self._update_text_viewer_styles()
            self.refresh_reader_display()
            self.save_settings()

    def reset_colors(self):
        self.reader_bg = ""
        self.reader_fg = ""
        self._update_text_viewer_styles()
        self.refresh_reader_display()
        self.save_settings()

    def on_western_font_changed(self, font: QFont):
        family = font.family()
        if not family or family == self.reader_font_western:
            return
        self.reader_font_western = family
        self._update_text_viewer_styles()
        self.refresh_reader_display()
        self.save_settings()

    def on_chinese_font_changed(self, font: QFont):
        family = font.family()
        if not family or family == self.reader_font_chinese:
            return
        self.reader_font_chinese = family
        self._update_text_viewer_styles()
        self.refresh_reader_display()
        self.save_settings()

    def toggle_text_bold(self, checked):
        self.reader_text_bold = checked
        self._update_text_viewer_styles()
        self.refresh_reader_display()
        self.save_settings()

    def change_font_size(self, delta: int):
        if self._is_pdf_active():
            self.pdf_viewer.zoom_by(1.15 if delta > 0 else 1 / 1.15)
            return
        viewer = self._active_text_viewer()
        if viewer is None:
            return
        new_size = viewer.font_size + delta
        if 10 <= new_size <= 32:
            self.reader_font_size = new_size
            self._update_text_viewer_styles()
            self.refresh_reader_display()
            self.save_settings()

    def toggle_left_panel(self, checked):
        if checked:
            self.left_panel.setVisible(True)
            if hasattr(self, '_left_panel_width') and self._left_panel_width > 0:
                sizes = self.splitter.sizes()
                total = sum(sizes)
                mid = sizes[1]
                new_left = self._left_panel_width
                right = total - new_left - mid
                if right < 200:
                    right = 200
                    new_left = max(200, total - mid - right)
                self.splitter.setSizes([new_left, mid, right])
        else:
            self._left_panel_width = self.splitter.sizes()[0]
            self.left_panel.setVisible(False)
        self.toggle_left_action.setChecked(checked)

    def toggle_toc_panel(self, checked):
        if checked:
            self.mid_panel.setVisible(True)
            if hasattr(self, '_mid_panel_width') and self._mid_panel_width > 0:
                sizes = self.splitter.sizes()
                total = sum(sizes)
                left = sizes[0]
                new_mid = self._mid_panel_width
                right = total - left - new_mid
                if right < 200:
                    right = 200
                    new_mid = max(160, total - left - right)
                self.splitter.setSizes([left, new_mid, right])
        else:
            self._mid_panel_width = self.splitter.sizes()[1]
            self.mid_panel.setVisible(False)
        self.toggle_toc_action.setChecked(checked)

    def on_filter_epub_changed(self, checked):
        self.filter_epub = checked
        self.save_settings()
        self.filter_library()

    def on_filter_pdf_changed(self, checked):
        self.filter_pdf = checked
        self.save_settings()
        self.filter_library()

    def on_filter_txt_changed(self, checked):
        self.filter_txt = checked
        self.save_settings()
        self.filter_library()

    def filter_library(self, text: str = None):
        if text is None:
            text = self.edit_search.text()
        text = text.lower()

        show_epub = self.filter_epub
        show_pdf = self.filter_pdf
        show_txt = self.filter_txt

        for i in range(self.tree_library.topLevelItemCount()):
            item = self.tree_library.topLevelItem(i)
            file_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
            ext = file_path.lower().split('.')[-1] if '.' in file_path else ''

            file_name = item.text(0).lower()  # v1.6.2.1：文件名位于第 0 列，仍参与筛选
            title = item.text(1).lower()
            author = item.text(2).lower()
            matches_text = (text in title or text in author or text in file_name)
            matches_format = ((ext == 'epub' and show_epub)
                              or (ext == 'pdf' and show_pdf)
                              or (ext == 'txt' and show_txt))

            item.setHidden(not matches_text or not matches_format)

    def _start_scan(self, folders, clear_first: bool = False):
        """启动扫描；clear_first=False 时书籍追加到现有书库（v1.6.2）。"""
        if clear_first:
            self.tree_library.clear()
            self.library.clear()

        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop()
            self.scan_thread.wait()

        formats = set()
        if self.filter_epub:
            formats.add("epub")
        if self.filter_pdf:
            formats.add("pdf")
        if self.filter_txt:
            formats.add("txt")
        if not formats:
            self.lbl_status.setText("请至少选择一种格式")
            self.statusBar().showMessage("请至少选择一种格式")
            return

        self.scan_thread = ScanThread(folders, formats)
        self.scan_thread.file_found.connect(self.on_file_found)
        self.scan_thread.scan_finished.connect(self.on_scan_finished)
        self.scan_thread.scan_error.connect(self.on_scan_error)
        self.scan_thread.start()

    def add_folder_association(self):
        """扫描并关联新目录：书籍追加到书库，可累积多个目录（v1.6.2）。"""
        folder = QFileDialog.getExistingDirectory(self, "选择要扫描并关联的文件夹")
        if not folder:
            return
        norm = os.path.normpath(os.path.abspath(folder))
        known = [os.path.normpath(os.path.abspath(f)) for f in self.library_folders]
        if norm not in known:
            self.library_folders.append(norm)
            self.settings["library_folders"] = self.library_folders
            self.save_settings()
        self.lbl_status.setText(f"正在扫描: {norm}...")
        self.statusBar().showMessage("正在扫描目录...")
        self._start_scan([norm])

    def rescan_all_folders(self):
        """重新扫描所有已关联目录（文件变动后刷新书库）。"""
        if not self.library_folders:
            QMessageBox.information(self, "重新扫描", "尚未关联任何目录，请先用「➕ 添加目录」。")
            return
        self.lbl_status.setText("正在重新扫描全部关联目录...")
        self.statusBar().showMessage("正在重新扫描全部关联目录...")
        self._start_scan(list(self.library_folders))

    def remove_folder_association(self):
        """取消目录关联：同时从书库移除该目录下的书籍（不删磁盘文件）。"""
        if not self.library_folders:
            QMessageBox.information(self, "取消关联", "当前没有已关联的目录。")
            return
        folder, ok = QInputDialog.getItem(
            self, "管理关联目录", "选择要移除的目录：",
            self.library_folders, 0, False)
        if not ok or not folder:
            return
        reply = QMessageBox.question(
            self, "确认移除",
            f"移除关联目录：\n{folder}\n\n该目录中的书籍将从书库移除（不会删除磁盘文件）。")
        if reply != QMessageBox.StandardButton.Yes:
            return
        norm = os.path.normpath(os.path.abspath(folder))
        self.library_folders = [
            f for f in self.library_folders
            if os.path.normpath(os.path.abspath(f)) != norm]
        self.settings["library_folders"] = self.library_folders
        self.save_settings()

        prefix = norm + os.sep
        removed = 0
        for i in range(self.tree_library.topLevelItemCount() - 1, -1, -1):
            item = self.tree_library.topLevelItem(i)
            p = item.data(0, Qt.ItemDataRole.UserRole) or ""
            if p == norm or p.startswith(prefix):
                self.library.pop(p, None)
                try:
                    self.db.delete_book(p)
                except Exception:
                    pass
                self.tree_library.takeTopLevelItem(i)
                removed += 1
        self.statusBar().showMessage(f"已移除关联目录：{norm}（同时移除 {removed} 本书）")
        self.lbl_status.setText(f"已取消关联：{norm}（移除 {removed} 本）")

    def on_file_found(self, file_path: str, title: str, author: str):
        fmt = file_path.lower().split('.')[-1] if '.' in file_path else ''
        key = lyuer_db.normalize_path(file_path)
        self.library[key] = (title, author)
        try:
            self.db.upsert_book(key, title, author, fmt)
        except Exception:
            # 数据库失败不应阻断阅读；保留 UI 项即可。
            pass
        self._ensure_library_item(key, title, author, fmt)
        self._apply_library_sort()

    def on_scan_finished(self, count: int):
        self.lbl_status.setText(f"扫描完成，共找到 {count} 本书")
        self.statusBar().showMessage(f"扫描完成，共 {count} 本书")

    def on_scan_error(self, error_msg: str):
        self.statusBar().showMessage(f"扫描错误: {error_msg}")

    def open_file_dialog(self):
        filters = []
        if self.filter_epub:
            filters.append("EPUB 文件 (*.epub)")
        if self.filter_pdf:
            filters.append("PDF 文件 (*.pdf)")
        if self.filter_txt:
            filters.append("TXT 文件 (*.txt)")
        if not filters:
            QMessageBox.information(self, "提示", "请至少勾选一种文件格式")
            return

        file_filter = ";;".join(filters + ["所有文件 (*)"])
        file_path, _ = QFileDialog.getOpenFileName(
            self, "打开文件", "", file_filter)
        if file_path:
            self.open_book_file(file_path)

    def open_book_file(self, file_path: str):
        """打开书籍（v1.6.2）：解析移入后台线程，阅读区显示转圈占位。"""
        ext = file_path.lower().split('.')[-1] if '.' in file_path else ''
        if ext not in ('epub', 'txt', 'pdf'):
            QMessageBox.warning(self, "提示", f"不支持的文件格式: {ext or os.path.basename(file_path)}")
            return
        if self._is_loading:
            self.statusBar().showMessage("正在加载其他书籍，请稍候…")
            return
        if not os.path.exists(file_path):
            QMessageBox.warning(self, "提示", f"文件不存在：\n{file_path}")
            return
        # 引擎可用性快速失败，不启动线程
        if ext == 'epub' and (self.epub_viewer is None or not epub_viewer.is_epub_available()):
            QMessageBox.critical(self, "打开失败",
                                 "Qt WebEngine EPUB 渲染引擎不可用。请安装：pip install PySide6 PySide6-WebEngine")
            return
        if ext == 'pdf' and (self.pdf_viewer is None or not pdf_viewer.is_pdf_available()):
            QMessageBox.critical(self, "打开失败",
                                 "QtPdf 渲染引擎不可用，无法打开 PDF。请升级 PySide6：pip install -U PySide6")
            return

        self.statusBar().showMessage(f"正在加载: {os.path.basename(file_path)}...")
        self._is_loading = True
        self._pending_open_path = file_path
        self._prev_view = self.view_stack.currentWidget()
        # 解析超过 300ms 才切换加载页，避免快速打开时页面闪烁
        QTimer.singleShot(300, self._show_loading_page)

        self._load_worker = BookLoadWorker(file_path)
        self._load_worker.loaded.connect(self._on_book_loaded)
        self._load_worker.failed.connect(self._on_book_load_failed)
        self._load_worker.start()

    def _show_loading_page(self):
        if self._is_loading:
            self.loading_page.set_message(
                f"正在加载：{os.path.basename(self._pending_open_path)}…\n"
                f"大文件解析可能需要数秒，窗口其余面板可正常操作")
            self.view_stack.setCurrentWidget(self.loading_page)

    def _on_book_loaded(self, book, ext: str):
        file_path = self._pending_open_path
        self._is_loading = False
        try:
            self._finish_open_book(book, ext, file_path)
        finally:
            if self._load_worker is not None:
                self._load_worker.deleteLater()
            self._load_worker = None

    def _on_book_load_failed(self, error_msg: str):
        file_path = self._pending_open_path
        self._is_loading = False
        if self._prev_view is not None:
            self.view_stack.setCurrentWidget(self._prev_view)
        QMessageBox.critical(
            self, "打开失败",
            f"无法打开文件:\n{file_path}\n\n错误: {error_msg}\n\n"
            f"提示: 若文件损坏或编码异常，可尝试用其他工具重新转换。")
        self.statusBar().showMessage("打开失败")
        if self._load_worker is not None:
            self._load_worker.deleteLater()
        self._load_worker = None

    def _finish_open_book(self, book, ext: str, file_path: str):
        """解析完成后的主线程装配：切换阅读器、填充章节、恢复阅读进度。"""
        prev_format = self.current_format
        try:
            if ext == 'epub':
                if self.pdf_viewer is not None:
                    self.pdf_viewer.shutdown()
                self.epub_viewer.shutdown()
                self.current_book = book
                self.epub_viewer.set_book_images(self.current_book.images)
                self.epub_viewer.set_chapters(self.current_book.chapters)
                self.view_stack.setCurrentWidget(self.epub_viewer)
            elif ext == 'txt':
                if self.pdf_viewer is not None:
                    self.pdf_viewer.shutdown()
                if self.epub_viewer is not None:
                    self.epub_viewer.shutdown()
                self.current_book = book
                self.txt_reader.clear()
                self.view_stack.setCurrentWidget(self.txt_reader)
            elif ext == 'pdf':
                if self.epub_viewer is not None:
                    self.epub_viewer.shutdown()
                self.current_book = book
                # v1.4.1：阅读器结构对齐 PDF Maker 的 previewr.py
                self.view_stack.setCurrentWidget(self.pdf_viewer)
                self.pdf_viewer.load_document(file_path)

            self.current_format = ext
            self._update_format_ui()

            self._current_book_path = lyuer_db.normalize_path(file_path)
            try:
                rec = self.db.get_book_by_path(self._current_book_path)
                if rec is None:
                    self.db.upsert_book(self._current_book_path, self.current_book.title,
                                        self.current_book.author, ext)
                else:
                    self.db.touch_opened(self._current_book_path)
                    # 元数据优先以本次解析结果刷新；阅读进度字段保持不变。
                    self.db.upsert_book(self._current_book_path, self.current_book.title,
                                        self.current_book.author, ext,
                                        extra={"meta": rec.meta})
            except Exception:
                pass

            start_index = 0
            rec = self.db.get_book_by_path(self._current_book_path) if self._current_book_path else None
            if rec and rec.reading_chapter < len(self.current_book.chapters):
                start_index = rec.reading_chapter

            self.lbl_book_title.setText(f"《{self.current_book.title}》\n作者: {self.current_book.author}")
            self.setWindowTitle(
                f"{os.path.basename(file_path)} - 《{self.current_book.title}》 - {self.current_book.author}")

            self.list_chapters.clear()
            for i, chapter in enumerate(self.current_book.chapters):
                item = QListWidgetItem(f"{i+1}. {chapter.title}")
                item.setData(Qt.ItemDataRole.UserRole, i)
                self.list_chapters.addItem(item)

            if self.current_book.chapters:
                self.load_chapter(start_index, restore_position=True)
                self.statusBar().showMessage(f"已打开: {self.current_book.title}，共 {len(self.current_book.chapters)} 章")
            else:
                viewer = self._active_text_viewer()
                if viewer is not None:
                    viewer.clear()
                    if isinstance(viewer, TxtReaderWidget):
                        viewer.setPlaceholderText("此书没有可读取的章节")
                self.statusBar().showMessage("此书没有可读取的章节")

            self._ensure_library_item(self._current_book_path or file_path,
                                      self.current_book.title,
                                      self.current_book.author, ext)

            self.update_nav_buttons()
        except Exception as e:
            self.current_format = prev_format
            if self._prev_view is not None:
                self.view_stack.setCurrentWidget(self._prev_view)
            QMessageBox.critical(self, "打开失败", f"无法打开文件:\n{file_path}\n\n错误: {str(e)}\n\n提示: 若文件损坏或编码异常，可尝试用其他工具重新转换。")
            self.statusBar().showMessage("打开失败")

    def load_chapter(self, index: int, restore_position: bool = False):
        if self._is_loading:
            return
        if not self.current_book or index < 0 or index >= len(self.current_book.chapters):
            return
        chapter = self.current_book.chapters[index]
        self.current_chapter_index = index

        if self._is_pdf_active():
            # PDF：章节 = 页码区间，跳转即可（v1.4.0 浏览器式阅读器）
            self.pdf_viewer.go_to_page(chapter.page_start)
            self.lbl_chapter_title.setText(
                f"第 {index + 1} 项 / 共 {len(self.current_book.chapters)} 项 | {chapter.title}")
            if index < self.list_chapters.count():
                self.list_chapters.blockSignals(True)
                self.list_chapters.setCurrentRow(index)
                self.list_chapters.blockSignals(False)
            if restore_position and self.db and self._current_book_path:
                rec = self.db.get_book_by_path(self._current_book_path)
                if rec and rec.reading_chapter == index:
                    self.pdf_viewer.set_scroll_ratio(rec.reading_scroll)
            self._save_reading_position()
            self.update_nav_buttons()
            return

        viewer = self._active_text_viewer()
        if viewer is None:
            return
        viewer.current_chapter_file = chapter.file_name
        if self.current_format == "epub":
            viewer.set_book_images(self.current_book.images)
            viewer.set_chapter_content(chapter.content)
        else:
            viewer.set_text_content(chapter.content)

        self.lbl_chapter_title.setText(
            f"第 {index + 1} 章 / 共 {len(self.current_book.chapters)} 章 | {chapter.title}")
        if index < self.list_chapters.count():
            self.list_chapters.blockSignals(True)
            self.list_chapters.setCurrentRow(index)
            self.list_chapters.blockSignals(False)

        target_ratio = 0.0
        if restore_position and self.db and self._current_book_path:
            rec = self.db.get_book_by_path(self._current_book_path)
            if rec and rec.reading_chapter == index:
                target_ratio = rec.reading_scroll
        viewer.set_scroll_ratio(target_ratio)
        self._save_reading_position()
        self.update_nav_buttons()

    def on_pdf_page_changed(self, page: int, total: int):
        """PDF 阅读器滚动换页：同步章节面板与阅读进度。"""
        if not self.current_book or self.current_format != 'pdf':
            return
        ch_idx = 0
        for i, ch in enumerate(self.current_book.chapters):
            if ch.page_start <= page:
                ch_idx = i
            else:
                break
        if ch_idx != self.current_chapter_index:
            self.current_chapter_index = ch_idx
            if ch_idx < self.list_chapters.count():
                self.list_chapters.blockSignals(True)
                self.list_chapters.setCurrentRow(ch_idx)
                self.list_chapters.blockSignals(False)
            chapter = self.current_book.chapters[ch_idx]
            self.lbl_chapter_title.setText(
                f"第 {ch_idx + 1} 项 / 共 {len(self.current_book.chapters)} 项 | {chapter.title}")
        self.update_nav_buttons()
        self._save_reading_position()

    def _on_epub_navigate(self, index: int, anchor: str):
        """EPUB 正文/目录页链接跳转（来自 Qt WebEngine 导航拦截）。"""
        if not self.current_book or index < 0 or index >= len(self.current_book.chapters):
            return
        if index == self.current_chapter_index:
            if anchor and self.epub_viewer is not None:
                self.epub_viewer.scroll_to_anchor(anchor)
            return
        self._save_reading_position()
        self.load_chapter(index)
        if anchor and self.epub_viewer is not None:
            self.epub_viewer.scroll_to_anchor(anchor)

    def on_chapter_clicked(self, item: QListWidgetItem):
        if self._is_loading:
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is None:
            return
        self.load_chapter(int(index))

    def on_library_item_clicked(self, item: QTreeWidgetItem, column: int):
        file_path = item.data(0, Qt.ItemDataRole.UserRole)
        if file_path and os.path.exists(file_path):
            self.open_book_file(file_path)

    def _open_file_location(self, file_path: str):
        if not file_path or not os.path.exists(file_path):
            return
        folder = os.path.dirname(os.path.abspath(file_path))
        system = platform.system()
        try:
            if system == 'Windows':
                os.startfile(folder)
            elif system == 'Darwin':
                subprocess.Popen(['open', folder])
            else:
                subprocess.Popen(['xdg-open', folder])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", f"无法打开文件夹:\n{folder}\n\n{str(e)}")

    def show_library_context_menu(self, position):
        item = self.tree_library.itemAt(position)
        if not item:
            return
        file_path = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #252526;
                color: #cccccc;
                border: 1px solid #3e3e42;
            }
            QMenu::item:selected {
                background-color: #094771;
                color: #ffffff;
            }
        """)
        open_action = menu.addAction("打开")
        open_loc_action = menu.addAction("打开文件所在位置")
        copy_name_action = menu.addAction("复制文件名")
        copy_path_action = menu.addAction("复制完整路径")
        remove_action = menu.addAction("从书库移除")

        action = menu.exec(self.tree_library.viewport().mapToGlobal(position))
        if action == open_action:
            self.on_library_item_clicked(item, 0)
        elif action == open_loc_action:
            self._open_file_location(file_path)
        elif action == copy_name_action:
            QApplication.clipboard().setText(os.path.basename(file_path))
        elif action == copy_path_action:
            QApplication.clipboard().setText(file_path)
        elif action == remove_action:
            key = lyuer_db.normalize_path(file_path)
            self.library.pop(key, None)
            try:
                self.db.delete_book(key)
            except Exception:
                pass
            self.tree_library.takeTopLevelItem(self.tree_library.indexOfTopLevelItem(item))

    def _update_format_ui(self):
        """按当前格式切换工具栏：PDF 下禁用字体/主题相关控件。"""
        is_pdf = self._is_pdf_active()
        for w in (self.btn_font_dec, self.btn_font_inc,
                  self.combo_theme,
                  self.btn_pick_bg, self.btn_pick_fg, self.btn_reset_color,
                  self.combo_font_western, self.combo_font_chinese,
                  self.btn_bold):
            w.setEnabled(not is_pdf)
        self.update_nav_buttons()

    def prev_chapter(self):
        if self._is_loading:
            return
        if self._is_pdf_active():
            self.pdf_viewer.go_to_page(self.pdf_viewer.current_page() - 1)
            return
        if self.current_chapter_index > 0:
            self._save_reading_position()
            self.load_chapter(self.current_chapter_index - 1)

    def next_chapter(self):
        if self._is_loading:
            return
        if self._is_pdf_active():
            self.pdf_viewer.go_to_page(self.pdf_viewer.current_page() + 1)
            return
        if self.current_book and self.current_chapter_index < len(self.current_book.chapters) - 1:
            self._save_reading_position()
            self.load_chapter(self.current_chapter_index + 1)

    def update_nav_buttons(self):
        if self._is_pdf_active():
            # PDF 模式：上一页 / 下一页
            self.btn_prev.setText("◀ 上一页")
            self.btn_next.setText("下一页 ▶")
            total = self.pdf_viewer.page_count()
            cur = self.pdf_viewer.current_page()
            self.btn_prev.setEnabled(total > 0 and cur > 0)
            self.btn_next.setEnabled(total > 0 and cur < total - 1)
            return
        self.btn_prev.setText("◀ 上一章")
        self.btn_next.setText("下一章 ▶")
        has_book = self.current_book is not None and len(self.current_book.chapters) > 0
        self.btn_prev.setEnabled(has_book and self.current_chapter_index > 0)
        self.btn_next.setEnabled(has_book and self.current_chapter_index < len(self.current_book.chapters) - 1)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls:
                path = urls[0].toLocalFile().lower()
                if path.endswith(('.epub', '.pdf', '.txt')):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            if file_path.lower().endswith(('.epub', '.pdf', '.txt')):
                event.acceptProposedAction()
                try:
                    self.open_book_file(file_path)
                except Exception:
                    pass

    def closeEvent(self, event):
        self._save_reading_position()
        self.save_settings()
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.stop()
            self.scan_thread.wait()
        if self._load_worker is not None and self._load_worker.isRunning():
            self._load_worker.wait(5000)
            if self._load_worker.isRunning():
                # v1.6.3.0：超大文件解析卡死等极端情况的退出兜底，
                # 避免关闭窗口被后台线程永久阻塞
                self._load_worker.terminate()
                self._load_worker.wait(2000)
        if getattr(self, "pdf_viewer", None):
            self.pdf_viewer.shutdown()
        if getattr(self, "epub_viewer", None):
            self.epub_viewer.shutdown()
        if getattr(self, "db", None):
            try:
                self.db.close()
            except Exception:
                pass
        event.accept()


# =============================================================================
# 入口
# =============================================================================
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MienArchReader")
    app.setStyle("Fusion")

    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)

    # 设置应用级图标（影响任务栏/EXE 图标）
    icon_path = _get_icon_path()
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    window = MainWindow()

    if len(sys.argv) > 1:
        dropped_path = sys.argv[1]
        if dropped_path.lower().endswith(('.epub', '.pdf', '.txt')) and os.path.exists(dropped_path):
            window.open_book_file(dropped_path)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()