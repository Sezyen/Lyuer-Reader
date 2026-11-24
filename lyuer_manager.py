#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MienArch Lyuer Manager v1.1.1 — 书库管理器
版权所有 (c) MienArch | 面包图书馆 | 2026
许可证：GNU GPL v3.0 or later
==========================================================

v1.1.1：
- 修复窗口激活自动同步漏检字段：_sync_from_db 的书库签名此前仅含
  path/missing/title/author/cover，评分、丛书、出版社、标签、简介、
  文件大小等字段被外部（主程序/其他入口）修改时激活窗口不会刷新；
  签名补齐上述字段后任何元数据变化都能触发重载。



依赖：PySide6（仅 QtWidgets/Gui/Core，无需 WebEngine）、lyuer_database；
可选 PyMuPDF（PDF 元数据与封面）。
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import posixpath
import random
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote
from xml.etree import ElementTree as ET

try:
    import lyuer_database as lyuer_db
except ImportError:
    print("无法导入 lyuer_database.py，请确认它与本脚本位于同一目录。", file=sys.stderr)
    raise

_MISSING = []
try:
    from PySide6.QtCore import Qt, QSize, QTimer, QRect, QPoint, QEvent
    from PySide6.QtGui import (
        QFont, QFontMetrics, QIcon, QPixmap, QPainter, QColor, QPen,
        QLinearGradient, QAction, QKeySequence, QShortcut,
    )
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QSplitter, QTreeWidget, QTreeWidgetItem, QListWidget, QListWidgetItem,
        QLineEdit, QPushButton, QLabel, QComboBox, QFileDialog, QMessageBox,
        QMenu, QDialog, QDialogButtonBox, QFormLayout, QSpinBox, QDoubleSpinBox,
        QTextEdit, QAbstractItemView, QInputDialog,
    )
except ImportError:
    _MISSING.append("PySide6")

try:
    import pymupdf as fitz  # noqa: N813
except ImportError:
    try:
        import fitz  # noqa: N813
    except ImportError:
        fitz = None

if _MISSING:
    print("缺少依赖: " + ", ".join(_MISSING), file=sys.stderr)
    print("请运行: pip install " + " ".join(_MISSING), file=sys.stderr)
    sys.exit(1)


# =============================================================================
# 配置路径
# =============================================================================
def _get_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_config_path() -> str:
    d = os.path.join(_get_app_dir(), ".config")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "LyuerManager_config.json")


def _get_resource_dir() -> str:
    """资源目录：开发运行为脚本目录；PyInstaller onedir 为 exe 同级 _internal。"""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        for cand in ("_internal", "internal"):
            full = os.path.join(exe_dir, cand)
            if os.path.isdir(full):
                return full
        return getattr(sys, "_MEIPASS", exe_dir)
    return os.path.dirname(os.path.abspath(__file__))


def _get_icon_path() -> str:
    """图标路径：优先 _internal/Lyuer.png，兼容 lyuer.png / 旧名 Lyuer.ico。"""
    for name in ("Lyuer.png", "lyuer.png", "Lyuer.ico", "lyuer.ico"):
        full = os.path.join(_get_resource_dir(), name)
        if os.path.exists(full):
            return full
    return ""


# =============================================================================
# 轻量元数据 / 封面提取（不依赖 QtWebEngine）
# =============================================================================
class _EpubLite:
    """EPUB 轻量读取：标题/作者/封面字节（zip + container.xml + OPF）。"""

    @staticmethod
    def read(file_path: str) -> Tuple[str, str, Optional[bytes]]:
        title, author, cover = "未知标题", "未知作者", None
        with zipfile.ZipFile(file_path) as zf:
            names = zf.namelist()
            lower = {n.lower(): n for n in names}

            container = next((lower[k] for k in lower
                              if k.endswith("meta-inf/container.xml")), None)
            opf_path = None
            if container:
                try:
                    root = ET.fromstring(zf.read(container))
                    for el in root.iter():
                        if el.tag.endswith("rootfile"):
                            fp = el.get("full-path")
                            if fp:
                                prefix = container[:container.lower().index("meta-inf/")]
                                cand = prefix + fp
                                opf_path = cand if cand in names else lower.get(cand.lower())
                                break
                except Exception:
                    opf_path = None
            if opf_path is None:
                cands = [n for n in names if n.lower().endswith(".opf")]
                if not cands:
                    raise Exception("找不到 OPF 包文件")
                opf_path = sorted(cands, key=len)[0]

            opf = ET.fromstring(zf.read(opf_path))
            opf_dir = posixpath.dirname(opf_path)
            ns = {
                "dc": "http://purl.org/dc/elements/1.1/",
                "opf": "http://www.idpf.org/2007/opf",
            }

            t = opf.find(".//dc:title", ns)
            if t is not None and t.text and t.text.strip():
                title = t.text.strip()
            creators = [c.text.strip() for c in opf.findall(".//dc:creator", ns)
                        if c.text and c.text.strip()]
            if creators:
                author = " / ".join(creators)

            # 封面定位：
            # 1) manifest item properties="cover-image"
            # 2) <meta name="cover" content="id"> 指向的 item
            cover_href = None
            cover_id = None
            for el in opf.iter():
                tag = el.tag.split("}")[-1]
                if tag == "meta" and (el.get("name") or "").lower() == "cover":
                    cover_id = el.get("content")
            for el in opf.iter():
                tag = el.tag.split("}")[-1]
                if tag != "item":
                    continue
                props = (el.get("properties") or "").split()
                if "cover-image" in props or (cover_id and el.get("id") == cover_id):
                    cover_href = el.get("href")
                    if cover_href:
                        break
            if cover_href is None:
                # 兜底：manifest 中文件名含 cover 的图片
                for el in opf.iter():
                    tag = el.tag.split("}")[-1]
                    if tag == "item" and (el.get("media-type") or "").startswith("image/"):
                        if "cover" in posixpath.basename(el.get("href") or "").lower():
                            cover_href = el.get("href")
                            break
            if cover_href:
                full = posixpath.normpath(
                    posixpath.join(opf_dir, unquote(cover_href).replace("\\", "/")))
                if full in names:
                    cover = zf.read(full)
                elif full.lower() in lower:
                    cover = zf.read(lower[full.lower()])
        return title, author, cover

    @staticmethod
    def read_metadata(file_path: str) -> Tuple[str, str]:
        t, a, _ = _EpubLite.read(file_path)
        return t, a


class _PdfLite:
    @staticmethod
    def read_metadata(file_path: str) -> Tuple[str, str]:
        if fitz is None:
            return os.path.splitext(os.path.basename(file_path))[0], "未知作者"
        doc = fitz.open(file_path)
        try:
            meta = doc.metadata or {}
            title = (meta.get("title") or "").strip() or "未知标题"
            author = (meta.get("author") or "").strip() or "未知作者"
            return title, author
        finally:
            doc.close()

    @staticmethod
    def read_cover(file_path: str) -> Optional[bytes]:
        if fitz is None:
            return None
        doc = fitz.open(file_path)
        try:
            if len(doc) <= 0:
                return None
            pix = doc.load_page(0).get_pixmap(matrix=fitz.Matrix(0.6, 0.6))
            return pix.tobytes("png")
        except Exception:
            return None
        finally:
            doc.close()


def _read_book_meta(file_path: str) -> Tuple[str, str]:
    ext = file_path.lower().split(".")[-1]
    if ext == "epub":
        return _EpubLite.read_metadata(file_path)
    if ext == "pdf":
        return _PdfLite.read_metadata(file_path)
    return os.path.splitext(os.path.basename(file_path))[0], "未知作者"


def _read_book_cover(file_path: str) -> Optional[bytes]:
    ext = file_path.lower().split(".")[-1]
    try:
        if ext == "epub":
            _, _, cover = _EpubLite.read(file_path)
            return cover
        if ext == "pdf":
            return _PdfLite.read_cover(file_path)
    except Exception:
        return None
    return None


# =============================================================================
# 牛皮纸占位封面
# =============================================================================
COVER_W, COVER_H = 200, 300


def _split_title_lines(text: str, fm: QFontMetrics, max_w: int) -> List[str]:
    lines: List[str] = []
    buf = ""
    for ch in text:
        if ch == "\n":
            lines.append(buf)
            buf = ""
            continue
        if fm.horizontalAdvance(buf + ch) > max_w and buf:
            lines.append(buf)
            buf = ch
        else:
            buf += ch
    if buf:
        lines.append(buf)
    return [ln for ln in lines if ln.strip()]


def make_kraft_cover(title_text: str, sub_text: str = "",
                     w: int = COVER_W, h: int = COVER_H) -> QPixmap:
    """生成「牛皮纸」风格占位封面：暖纸色底 + 双线边框 + 居中书名。"""
    pm = QPixmap(w, h)
    pm.fill(QColor("#c7a87b"))

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # 轻微明暗渐变（模拟纸张受光）
    grad = QLinearGradient(0, 0, w, h)
    grad.setColorAt(0.0, QColor(255, 248, 235, 40))
    grad.setColorAt(0.5, QColor(0, 0, 0, 0))
    grad.setColorAt(1.0, QColor(90, 60, 20, 45))
    p.fillRect(QRect(0, 0, w, h), grad)

    # 纸纤维纹理（以书名为种子，保证同书同纹）
    seed = int(hashlib.md5((title_text or "lyuer").encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    p.setPen(QPen(QColor(120, 90, 50, 22), 1))
    for _ in range(60):
        x = rng.randint(0, w)
        y = rng.randint(0, h)
        ln = rng.randint(6, 22)
        p.drawLine(QPoint(x, y), QPoint(x + rng.randint(-3, 3), y + ln))

    # 双线边框
    p.setPen(QPen(QColor("#8b6f47"), 2))
    p.drawRect(QRect(8, 8, w - 16, h - 16))
    p.setPen(QPen(QColor("#8b6f47"), 1))
    p.drawRect(QRect(13, 13, w - 26, h - 26))

    # 顶部小标
    p.setPen(QColor("#6d5636"))
    f0 = QFont("Serif", 8)
    f0.setBold(True)
    p.setFont(f0)
    p.drawText(QRect(0, 22, w, 16), Qt.AlignmentFlag.AlignHCenter, "MIENARCH LYUER")

    # 书名：自动缩放适配，居中排布
    display = (title_text or "未命名").strip()
    avail_h = h - 110
    size = max(14, min(26, int(w / max(4.0, len(display) ** 0.55))))
    while size > 10:
        f = QFont("Serif", size)
        f.setBold(True)
        fm = QFontMetrics(f)
        lines = _split_title_lines(display, fm, w - 56)
        line_h = fm.height() + 4
        if lines and line_h * len(lines) <= avail_h:
            break
        size -= 1
    p.setFont(f)
    fm = QFontMetrics(f)
    lines = _split_title_lines(display, fm, w - 56) or [display]
    total_h = (fm.height() + 4) * len(lines)
    y = (h - total_h) // 2 + 8
    p.setPen(QColor("#4a3826"))
    for ln in lines:
        p.drawText(QRect(20, y, w - 40, fm.height() + 6),
                   Qt.AlignmentFlag.AlignHCenter, ln)
        y += fm.height() + 4

    # 底部文件名 / 副标题
    if sub_text:
        f2 = QFont("Sans Serif", 8)
        p.setFont(f2)
        p.setPen(QColor("#6d5636"))
        sub = sub_text
        fm2 = QFontMetrics(f2)
        while fm2.horizontalAdvance(sub) > w - 48 and len(sub) > 4:
            sub = sub[:-2]
        if sub != sub_text:
            sub = sub[:-1] + "…"
        p.drawText(QRect(24, h - 44, w - 48, 20),
                   Qt.AlignmentFlag.AlignHCenter, sub)

    p.end()
    return pm


# =============================================================================
# 元数据编辑对话框（calibre 式）
# =============================================================================
class MetadataDialog(QDialog):
    """单本元数据编辑。OK 后由调用方写库。"""

    def __init__(self, record: lyuer_db.BookRecord, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"编辑元数据 - {record.title or os.path.basename(record.path)}")
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(8)

        self.ed_title = QLineEdit(record.title)
        self.ed_author = QLineEdit(record.author)
        self.ed_series = QLineEdit(record.series)
        self.sp_series_index = QDoubleSpinBox()
        self.sp_series_index.setRange(0, 9999)
        self.sp_series_index.setDecimals(1)
        si = record.meta.get("series_index")
        if si is not None:
            try:
                self.sp_series_index.setValue(float(si))
            except Exception:
                pass
        self.ed_publisher = QLineEdit(record.publisher)
        self.sp_rating = QSpinBox()
        self.sp_rating.setRange(0, 10)
        self.sp_rating.setSuffix(" 星")
        self.sp_rating.setValue(record.rating)
        self.ed_tags = QLineEdit("; ".join(record.tags))
        self.ed_isbn = QLineEdit(str(record.identifiers.get("isbn", "")))
        self.ed_comments = QTextEdit()
        self.ed_comments.setPlainText(record.comments)
        self.ed_comments.setAcceptRichText(False)
        self.ed_comments.setMinimumHeight(90)
        self.lbl_file = QLabel(
            f"{os.path.basename(record.path)}\n{record.path}\n"
            f"{record.format.upper() or '—'} | {record.size / 1024 / 1024:.1f} MB")
        self.lbl_file.setWordWrap(True)
        self.lbl_file.setStyleSheet("color: #858585; font-size: 11px;")

        form.addRow("书名", self.ed_title)
        form.addRow("作者", self.ed_author)
        form.addRow("丛书", self.ed_series)
        form.addRow("丛书序号", self.sp_series_index)
        form.addRow("出版社", self.ed_publisher)
        form.addRow("评分", self.sp_rating)
        form.addRow("标签", self.ed_tags)
        form.addRow("ISBN", self.ed_isbn)
        form.addRow("简介", self.ed_comments)
        form.addRow("文件", self.lbl_file)
        lay.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def result_fields(self) -> dict:
        tags = [t.strip() for t in
                re.split(r"[;；]", self.ed_tags.text()) if t.strip()]
        return {
            "title": self.ed_title.text().strip(),
            "author": self.ed_author.text().strip(),
            "series": self.ed_series.text().strip(),
            "series_index": self.sp_series_index.value(),
            "publisher": self.ed_publisher.text().strip(),
            "rating": self.sp_rating.value(),
            "tags": tags,
            "identifiers": {"isbn": self.ed_isbn.text().strip()},
            "comments": self.ed_comments.toPlainText(),
        }


# =============================================================================
# 主窗口
# =============================================================================
class ManagerWindow(QMainWindow):
    GRID_W, GRID_H = 170, 250

    def __init__(self):
        super().__init__()
        self.setWindowTitle("MienArch Lyuer Manager v1.1.1 — 书库管理")
        self.setMinimumSize(1180, 760)

        self.config_path = _get_config_path()
        self.cfg = self._load_cfg()

        # 书库位置：默认与阅读器一致（应用目录/lyuer_database）
        db_dir = self.cfg.get("database_dir") or os.path.join(_get_app_dir(), "lyuer_database")
        self.db = lyuer_db.LibraryDatabase(db_dir)
        self.db_dir = db_dir

        self.records: List[lyuer_db.BookRecord] = []
        self.filter_cat = ""     # 当前筛选类别（"" 表示全部）
        self.filter_val = ""
        self.search_text = ""
        self._cover_cache: Dict[int, QPixmap] = {}

        self._init_ui()
        icon_path = _get_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
        self.reload_all()

    # ------------------------------------------------------------------
    def _load_cfg(self) -> dict:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self):
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # 顶部工具栏
        bar = QHBoxLayout()
        bar.setSpacing(6)

        def _btn(text, tip, slot):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            b.setStyleSheet("""
                QPushButton { background:#3c3c3c; color:#ccc; border:1px solid #3e3e42;
                              border-radius:4px; padding:6px 12px; font-size:13px; }
                QPushButton:hover { border:1px solid #a78bfa; background:#4a4a4a; }
            """)
            bar.addWidget(b)
            return b

        _btn("➕ 添加目录", "扫描目录并导入书库（含封面）", self.add_folder)
        _btn("📄 添加文件", "添加单个 EPUB / PDF / TXT 文件", self.add_files)
        _btn("🔄 刷新", "重新校验缺失状态并重载书库", self.reload_all)
        _btn("✏️ 编辑元数据", "编辑选中书籍的元数据", self.edit_selected_meta)
        self.btn_extract_cover = _btn("🖼 提取封面", "从原文件重新提取封面（右键：批量自动提取）", self.extract_selected_cover)
        self.btn_extract_cover.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_extract_cover.customContextMenuRequested.connect(self.show_extract_cover_menu)
        _btn("📖 用 Lyuer 打开", "调用 lyuer_main 打开选中书籍", self.open_selected_in_lyuer)
        _btn("⚙ 数据库位置", "选择书库数据库目录", self.choose_db_dir)

        bar.addStretch(1)

        bar.addWidget(QLabel("排序:"))
        self.combo_sort = QComboBox()
        self.combo_sort.addItems(["按书名", "按作者", "按评分", "按添加时间", "按文件大小", "按文件名"])
        self.combo_sort.currentIndexChanged.connect(self.refill_grid)
        self.combo_sort.setFixedWidth(110)
        self.combo_sort.setStyleSheet("""
            QComboBox { background:#3c3c3c; color:#ccc; border:1px solid #3e3e42;
                        border-radius:4px; padding:4px 8px; }
            QComboBox QAbstractItemView { background:#252526; color:#ccc;
                        selection-background-color:#37373d; }
        """)
        bar.addWidget(self.combo_sort)

        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索书名 / 作者 / 标签 / 路径…")
        self.ed_search.setFixedWidth(240)
        self.ed_search.textChanged.connect(self.on_search)
        self.ed_search.setStyleSheet("""
            QLineEdit { background:#3c3c3c; color:#ccc; border:1px solid #3e3e42;
                        border-radius:4px; padding:6px 10px; }
            QLineEdit:focus { border:1px solid #a78bfa; }
        """)
        bar.addWidget(self.ed_search)
        root.addLayout(bar)

        # 主区域：左筛选树 + 右封面网格
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.tree_filter = QTreeWidget()
        self.tree_filter.setHeaderLabel("筛选")
        self.tree_filter.setFixedWidth(230)
        self.tree_filter.setStyleSheet("""
            QTreeWidget { background:#1e1e1e; color:#ccc; border:1px solid #3e3e42;
                          border-radius:6px; outline:none; padding:4px; }
            QTreeWidget::item { padding:4px 2px; border-radius:4px; }
            QTreeWidget::item:selected { background:#37373d; color:#fff; }
            QTreeWidget::item:hover { background:#2a2d2e; }
        """)
        self.tree_filter.itemClicked.connect(self.on_filter_clicked)
        splitter.addWidget(self.tree_filter)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setIconSize(QSize(self.GRID_W - 20, self.GRID_H - 70))
        self.grid.setGridSize(QSize(self.GRID_W + 14, self.GRID_H + 14))
        self.grid.setSpacing(6)
        self.grid.setWordWrap(True)
        self.grid.setStyleSheet("""
            QListWidget { background:#1e1e1e; border:1px solid #3e3e42;
                          border-radius:6px; outline:none; }
            QListWidget::item { background:#252526; border:1px solid #3e3e42;
                                border-radius:6px; padding:6px; }
            QListWidget::item:hover { border:1px solid #a78bfa; }
            QListWidget::item:selected { border:1px solid #a78bfa;
                                         background:#2d2a3a; }
        """)
        self.grid.itemDoubleClicked.connect(
            lambda _it: self.open_selected_in_lyuer())
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self.show_grid_menu)
        splitter.addWidget(self.grid)

        splitter.setSizes([240, 900])
        root.addWidget(splitter, 1)

        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet("color:#858585; font-size:12px; padding:2px 4px;")
        root.addWidget(self.lbl_status)

        # 快捷键：Enter 打开 / F2 编辑元数据 / F5 刷新 / Delete 删除记录
        def _sc(key, slot):
            s = QShortcut(QKeySequence(key), self)
            s.setContext(Qt.ShortcutContext.WindowShortcut)
            s.activated.connect(slot)
        _sc(Qt.Key.Key_Return, self.open_selected_in_lyuer)
        _sc(Qt.Key.Key_Enter, self.open_selected_in_lyuer)
        _sc(Qt.Key.Key_F2, self.edit_selected_meta)
        _sc(Qt.Key.Key_F5, self.reload_all)
        _sc(Qt.Key.Key_Delete, self.delete_selected)

        self._apply_dark_palette()

    def _apply_dark_palette(self):
        from PySide6.QtGui import QPalette
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
        pal.setColor(QPalette.ColorRole.WindowText, QColor("#cccccc"))
        pal.setColor(QPalette.ColorRole.Base, QColor("#252526"))
        pal.setColor(QPalette.ColorRole.Text, QColor("#cccccc"))
        pal.setColor(QPalette.ColorRole.Button, QColor("#3c3c3c"))
        pal.setColor(QPalette.ColorRole.ButtonText, QColor("#cccccc"))
        pal.setColor(QPalette.ColorRole.Highlight, QColor("#37373d"))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#252526"))
        pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#cccccc"))
        QApplication.instance().setPalette(pal)

    # ------------------------------------------------------------------
    # 数据装载
    # ------------------------------------------------------------------
    def reload_all(self):
        """重载数据库、刷新缺失状态、重建筛选树与网格。"""
        records = self.db.all_books(include_missing=True)
        missing_n = 0
        for rec in records:
            missing = rec.missing or not os.path.exists(rec.path)
            if missing != rec.missing:
                try:
                    self.db.mark_missing(rec.path, missing)
                except Exception:
                    pass
            if missing:
                missing_n += 1
        self.records = self.db.all_books(include_missing=True)
        self._cover_cache.clear()
        self._rebuild_filter_tree()
        self.refill_grid()
        total = len(self.records)
        self.lbl_status.setText(
            f"书库共 {total} 本"
            + (f"，缺失 {missing_n} 本" if missing_n else "")
            + f" | 数据库: {self.db.db_path}")

    def _rebuild_filter_tree(self):
        self.tree_filter.clear()
        recs = self.records
        total = len(recs)

        def _cat(parent, name, counter, key=None):
            """counter: dict value->count；key 为该类别标识。"""
            head = QTreeWidgetItem([f"{name} ({sum(counter.values()) if counter else 0})"])
            head.setData(0, Qt.ItemDataRole.UserRole, (key or name, ""))
            head.setFlags(head.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            parent.addChild(head)
            for val, cnt in sorted(counter.items(),
                                   key=lambda x: (-x[1], x[0].lower())):
                if len(val) > 40:
                    val_show = val[:40] + "…"
                else:
                    val_show = val
                it = QTreeWidgetItem([f"{val_show} ({cnt})"])
                it.setToolTip(0, val)
                it.setData(0, Qt.ItemDataRole.UserRole, (key or name, val))
                head.addChild(it)
            head.setExpanded(True)

        root = QTreeWidgetItem(["全部书籍 (%d)" % total])
        root.setData(0, Qt.ItemDataRole.UserRole, ("", ""))
        self.tree_filter.addTopLevelItem(root)

        invisible = QTreeWidgetItem(self.tree_filter)
        invisible.setFlags(Qt.ItemFlag.NoItemFlags)

        authors: Dict[str, int] = {}
        series: Dict[str, int] = {}
        pubs: Dict[str, int] = {}
        fmts: Dict[str, int] = {}
        ratings: Dict[str, int] = {}
        tags: Dict[str, int] = {}
        for r in recs:
            for a in [x.strip() for x in re.split(r"[/、;；]", r.author) if x.strip()]:
                authors[a] = authors.get(a, 0) + 1
            if r.series.strip():
                series[r.series.strip()] = series.get(r.series.strip(), 0) + 1
            if r.publisher.strip():
                pubs[r.publisher.strip()] = pubs.get(r.publisher.strip(), 0) + 1
            if r.format:
                fmts[r.format.upper()] = fmts.get(r.format.upper(), 0) + 1
            rk = r.rating if 0 < r.rating <= 10 else 0
            label = f"{rk} 星" if rk else "未评分"
            ratings[label] = ratings.get(label, 0) + 1
            for t in r.tags:
                tags[t] = tags.get(t, 0) + 1

        _cat(invisible, "作者", authors, "author")
        _cat(invisible, "丛书", series, "series")
        _cat(invisible, "出版社", pubs, "publisher")
        _cat(invisible, "文件类型", fmts, "format")
        _cat(invisible, "评分", ratings, "rating")
        _cat(invisible, "标签", tags, "tags")

    # ------------------------------------------------------------------
    # 筛选 & 排序 & 搜索
    # ------------------------------------------------------------------
    def on_filter_clicked(self, item: QTreeWidgetItem, _col: int):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        self.filter_cat, self.filter_val = data
        self.refill_grid()

    def on_search(self, text: str):
        self.search_text = text.strip().lower()
        self.refill_grid()

    def _passes_filter(self, r: lyuer_db.BookRecord) -> bool:
        cat, val = self.filter_cat, self.filter_val
        if cat == "author":
            authors = [x.strip() for x in re.split(r"[/、;；]", r.author)]
            if val not in authors:
                return False
        elif cat == "series":
            if r.series.strip() != val:
                return False
        elif cat == "publisher":
            if r.publisher.strip() != val:
                return False
        elif cat == "format":
            if (r.format or "").upper() != val:
                return False
        elif cat == "rating":
            rk = r.rating if 0 < r.rating <= 10 else 0
            if (f"{rk} 星" if rk else "未评分") != val:
                return False
        elif cat == "tags":
            if val not in r.tags:
                return False

        if self.search_text:
            blob = " ".join([r.title, r.author, r.series, r.publisher,
                             r.path, " ".join(r.tags)]).lower()
            if self.search_text not in blob:
                return False
        return True

    def _sorted_records(self) -> List[lyuer_db.BookRecord]:
        recs = [r for r in self.records if self._passes_filter(r)]
        mode = self.combo_sort.currentIndex()
        if mode == 0:
            recs.sort(key=lambda r: (r.title.lower(), r.author.lower()))
        elif mode == 1:
            recs.sort(key=lambda r: (r.author.lower(), r.title.lower()))
        elif mode == 2:
            recs.sort(key=lambda r: (-r.rating, r.title.lower()))
        elif mode == 3:
            recs.sort(key=lambda r: (r.added_at, r.title.lower()))
        elif mode == 4:
            recs.sort(key=lambda r: (-r.size, r.title.lower()))
        else:  # mode == 5：按文件名排序（v1.1.0）
            recs.sort(key=lambda r: (os.path.basename(r.path).lower(),
                                     r.title.lower()))
        return recs

    # ------------------------------------------------------------------
    # 封面
    # ------------------------------------------------------------------
    def _cover_pixmap(self, rec: lyuer_db.BookRecord) -> QPixmap:
        pm = self._cover_cache.get(rec.id)
        if pm is not None:
            return pm
        if rec.cover:
            src = QPixmap()
            if src.loadFromData(rec.cover):
                pm = src.scaled(self.GRID_W - 20, self.GRID_H - 70,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
                self._cover_cache[rec.id] = pm
                return pm
        title = rec.title if rec.title != "未知标题" else \
            os.path.splitext(os.path.basename(rec.path))[0]
        pm = make_kraft_cover(title, os.path.basename(rec.path))
        self._cover_cache[rec.id] = pm
        return pm

    # ------------------------------------------------------------------
    # 网格填充
    # ------------------------------------------------------------------
    def refill_grid(self):
        self.grid.clear()
        recs = self._sorted_records()
        for rec in recs:
            stars = ("★" * rec.rating) if rec.rating > 0 else ""
            file_name = os.path.basename(rec.path)
            title = rec.title or file_name
            author = rec.author or "未知作者"
            # v1.1.0：封面下方依次为 文件名 → 书名 → 作者
            label = f"{file_name}\n{title}\n{author}"
            if stars:
                label += f"\n{stars} ({rec.rating})"
            if rec.series:
                label += f"\n〔{rec.series}〕"
            if rec.missing:
                label = "[缺失] " + label
            it = QListWidgetItem(QIcon(self._cover_pixmap(rec)), label)
            it.setData(Qt.ItemDataRole.UserRole, rec.id)
            it.setToolTip(f"{title}\n{author}\n{rec.path}\n"
                          f"评分: {rec.rating or '未评分'} | 丛书: {rec.series or '—'} | "
                          f"出版社: {rec.publisher or '—'}")
            it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            it.setSizeHint(QSize(self.GRID_W, self.GRID_H))
            self.grid.addItem(it)
        shown = len(recs)
        total = len(self.records)
        self.lbl_status.setText(
            f"显示 {shown} / {total} 本"
            + (f" | 筛选: {self.filter_cat or '全部'}"
               + (f" = {self.filter_val}" if self.filter_val else "")
               if self.filter_cat else ""))

    # ------------------------------------------------------------------
    # 当前选中
    # ------------------------------------------------------------------
    def _selected_record(self) -> Optional[lyuer_db.BookRecord]:
        item = self.grid.currentItem()
        if item is None:
            return None
        bid = item.data(Qt.ItemDataRole.UserRole)
        return self.db.get_book_by_id(int(bid)) if bid is not None else None

    # ------------------------------------------------------------------
    # 动作：扫描 / 添加
    # ------------------------------------------------------------------
    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择要导入书库的文件夹")
        if not folder:
            return
        found = 0
        errors = 0
        for root, _dirs, files in os.walk(folder):
            for name in files:
                ext = name.lower().split(".")[-1] if "." in name else ""
                if ext not in ("epub", "pdf", "txt"):
                    continue
                path = os.path.join(root, name)
                try:
                    title, author = _read_book_meta(path)
                    rec = self.db.upsert_book(path, title, author, ext)
                    if not rec.cover:
                        cover = _read_book_cover(path)
                        if cover:
                            self.db.set_cover(path, cover)
                    found += 1
                except Exception:
                    errors += 1
        self.reload_all()
        QMessageBox.information(
            self, "导入完成",
            f"已导入/更新 {found} 本" + (f"，失败 {errors} 本" if errors else ""))

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "添加书籍文件", "",
            "电子书 (*.epub *.pdf *.txt);;所有文件 (*)")
        n = 0
        for path in paths:
            ext = path.lower().split(".")[-1]
            try:
                title, author = _read_book_meta(path)
                rec = self.db.upsert_book(path, title, author, ext)
                if not rec.cover:
                    cover = _read_book_cover(path)
                    if cover:
                        self.db.set_cover(path, cover)
                n += 1
            except Exception as e:
                QMessageBox.warning(self, "添加失败", f"{path}\n{e}")
        if n:
            self.reload_all()

    # ------------------------------------------------------------------
    # 动作：元数据 / 封面 / 打开 / 删除
    # ------------------------------------------------------------------
    def edit_selected_meta(self):
        rec = self._selected_record()
        if rec is None:
            QMessageBox.information(self, "提示", "请先在网格中选择一本书。")
            return
        dlg = MetadataDialog(rec, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        f = dlg.result_fields()
        try:
            self.db.update_metadata(
                rec.path,
                title=f["title"], author=f["author"], series=f["series"],
                publisher=f["publisher"], rating=f["rating"],
                tags=f["tags"], identifiers=f["identifiers"],
                comments=f["comments"],
                meta={"series_index": f["series_index"]})
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return
        self._cover_cache.pop(rec.id, None)
        self.reload_all()

    def extract_selected_cover(self):
        rec = self._selected_record()
        if rec is None:
            return
        if not os.path.exists(rec.path):
            QMessageBox.warning(self, "提示", "文件已缺失，无法提取封面。")
            return
        cover = _read_book_cover(rec.path)
        if cover:
            self.db.set_cover(rec.path, cover)
            self._cover_cache.pop(rec.id, None)
            self.reload_all()
            self.statusBar().showMessage("封面已更新", 3000)
        else:
            QMessageBox.information(self, "提示", "未能从该文件提取封面（TXT 或无封面图）。")

    # ------------------------------------------------------------------
    # v1.1.0：批量提取封面（提取封面按钮右键）
    # ------------------------------------------------------------------
    def show_extract_cover_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background:#252526; color:#ccc; border:1px solid #3e3e42; }
            QMenu::item:selected { background:#094771; color:#fff; }
        """)
        a_all = menu.addAction("🖼 自动提取全部书籍封面")
        a_missing = menu.addAction("仅补齐缺失封面的书籍")
        act = menu.exec(self.btn_extract_cover.mapToGlobal(pos))
        if act == a_all:
            self.auto_extract_all_covers(only_missing=False)
        elif act == a_missing:
            self.auto_extract_all_covers(only_missing=True)

    def auto_extract_all_covers(self, only_missing: bool = False):
        """遍历书库批量提取封面；only_missing=True 时跳过已有封面的书。"""
        total = len(self.records)
        if total == 0:
            QMessageBox.information(self, "提示", "书库为空。")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        done = failed = skipped = 0
        try:
            for rec in self.records:
                if not os.path.exists(rec.path):
                    failed += 1
                    continue
                if only_missing and rec.cover:
                    skipped += 1
                    continue
                cover = _read_book_cover(rec.path)
                if cover:
                    try:
                        self.db.set_cover(rec.path, cover)
                        done += 1
                        continue
                    except Exception:
                        pass
                failed += 1
                QApplication.processEvents()
        finally:
            QApplication.restoreOverrideCursor()
        self._cover_cache.clear()
        self.reload_all()
        msg = f"共处理 {total} 本：成功 {done}，无封面或可提取失败 {failed}"
        if skipped:
            msg += f"，跳过已有封面 {skipped}"
        QMessageBox.information(self, "批量提取封面完成", msg)

    # ------------------------------------------------------------------
    # v1.1.0：打开方式（自选程序 / 系统默认）
    # ------------------------------------------------------------------
    def open_with_program(self, rec: "lyuer_db.BookRecord",
                          program: Optional[str] = None):
        """用用户自选软件打开书籍；program 为空时弹出选择框并记住该程序。"""
        if not os.path.exists(rec.path):
            QMessageBox.warning(self, "文件缺失", f"文件不存在：\n{rec.path}")
            return
        if program is None:
            filt = ("可执行文件 (*.exe *.bat *.cmd);;所有文件 (*)"
                    if sys.platform == "win32" else "所有文件 (*)")
            program, _ = QFileDialog.getOpenFileName(
                self, "选择用于打开该书籍的程序",
                self.cfg.get("open_with_program", ""), filt)
            if not program:
                return
            self.cfg["open_with_program"] = program
            self._save_cfg()
        try:
            if sys.platform == "win32" and program.lower().endswith((".bat", ".cmd")):
                subprocess.Popen(program, shell=True, close_fds=True)
            else:
                subprocess.Popen([program, rec.path], close_fds=True)
            self.db.touch_opened(rec.path)
            self.statusBar().showMessage(
                f"已用「{os.path.basename(program)}」打开: {rec.title}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "打开失败",
                                 f"无法用所选程序打开：\n{program}\n\n{e}")

    def open_with_default(self, rec: "lyuer_db.BookRecord"):
        """使用操作系统默认关联程序打开书籍。"""
        if not os.path.exists(rec.path):
            QMessageBox.warning(self, "文件缺失", f"文件不存在：\n{rec.path}")
            return
        import platform
        try:
            if platform.system() == "Windows":
                os.startfile(rec.path)  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", rec.path])
            else:
                subprocess.Popen(["xdg-open", rec.path])
            self.db.touch_opened(rec.path)
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    # ------------------------------------------------------------------
    # v1.1.0：窗口激活时与数据库自动同步
    # ------------------------------------------------------------------
    def event(self, ev):
        if ev.type() == QEvent.Type.WindowActivate:
            self._sync_from_db()
        return super().event(ev)

    def _sync_from_db(self):
        """轻量比对书库签名，仅在变化时重载（避免激活窗口频繁闪烁）。"""
        try:
            recs = self.db.all_books(include_missing=True)
        except Exception:
            return

        def _sig(rs):
            # v1.1.1：签名补齐评分/丛书/出版社/标签/简介/大小/修改时间——
            # 此前外部改动这些字段时签名比对一致，激活窗口漏掉刷新
            return [(r.path, r.missing, r.title, r.author, bool(r.cover),
                     r.rating, r.series, r.publisher, r.tags, r.comments,
                     r.size, r.mtime) for r in rs]

        if _sig(recs) == _sig(self.records):
            return
        self.records = recs
        self._cover_cache.clear()
        self._rebuild_filter_tree()
        self.refill_grid()

    def delete_selected(self):
        rec = self._selected_record()
        if rec is None:
            return
        reply = QMessageBox.question(
            self, "删除记录",
            f"从书库中删除记录？\n\n{rec.title}\n{rec.path}\n\n（不会删除磁盘文件）")
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_book(rec.id)
            self.reload_all()

    def _open_file_location(self, path: str):
        if not path or not os.path.exists(path):
            return
        import platform
        folder = os.path.dirname(os.path.abspath(path))
        try:
            if platform.system() == "Windows":
                os.startfile(folder)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    # ------------------------------------------------------------------
    # 调用 lyuer_main 打开
    # ------------------------------------------------------------------
    def find_lyuer_main(self) -> Optional[str]:
        """定位阅读器入口：配置项 > 同目录 lyuer_main*.py > lyuer_main*.exe。"""
        cfg_entry = self.cfg.get("lyuer_main", "")
        if cfg_entry and os.path.exists(cfg_entry):
            return cfg_entry
        base = _get_app_dir()
        pys = sorted(glob.glob(os.path.join(base, "lyuer_main*.py")))
        if pys:
            return pys[0]
        exes = sorted(glob.glob(os.path.join(base, "lyuer_main*.exe")))
        if exes:
            return exes[0]
        # PyInstaller 场景：管理器与主程序打包为同一目录下的 exe
        for pat in ("Lyuer*.exe", "lyuer*.exe"):
            cands = [p for p in glob.glob(os.path.join(base, pat))
                     if "manager" not in os.path.basename(p).lower()]
            if cands:
                return sorted(cands)[0]
        return None

    def open_selected_in_lyuer(self):
        rec = self._selected_record()
        if rec is None:
            QMessageBox.information(self, "提示", "请先在网格中选择一本书。")
            return
        if not os.path.exists(rec.path):
            QMessageBox.warning(self, "文件缺失", f"文件不存在：\n{rec.path}")
            return
        entry = self.find_lyuer_main()
        if entry is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "请指定 Lyuer 阅读器主程序 (lyuer_main.py 或 exe)", "",
                "Python/可执行 (*.py *.exe);;所有文件 (*)")
            if not path:
                return
            self.cfg["lyuer_main"] = path
            self._save_cfg()
            entry = path
        try:
            ext = entry.lower()
            if ext.endswith(".py"):
                cmd = [sys.executable, entry, rec.path]
            else:
                cmd = [entry, rec.path]
            subprocess.Popen(cmd, cwd=os.path.dirname(entry) or None,
                             close_fds=True)
            self.db.touch_opened(rec.path)
            self.statusBar().showMessage(f"已用 Lyuer 打开: {rec.title}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "启动失败", f"无法启动阅读器：\n{e}")

    # ------------------------------------------------------------------
    def choose_db_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "选择书库数据库目录", self.db_dir)
        if not folder:
            return
        new_dir = os.path.abspath(folder)
        if new_dir == os.path.abspath(self.db_dir):
            return
        self.db.close()
        self.db = lyuer_db.LibraryDatabase(new_dir)
        self.db_dir = new_dir
        self.cfg["database_dir"] = new_dir
        self._save_cfg()
        self.reload_all()

    # ------------------------------------------------------------------
    def show_grid_menu(self, pos):
        item = self.grid.itemAt(pos)
        if item is None:
            return
        self.grid.setCurrentItem(item)
        rec = self._selected_record()
        if rec is None:
            return
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background:#252526; color:#ccc; border:1px solid #3e3e42; }
            QMenu::item:selected { background:#094771; color:#fff; }
        """)
        a_open = menu.addAction("📖 用 Lyuer 打开")
        menu_open_with = menu.addMenu("打开方式")
        a_open_with = menu_open_with.addAction("选择程序…")
        prog = self.cfg.get("open_with_program", "")
        a_remembered = None
        if prog and os.path.exists(prog):
            a_remembered = menu_open_with.addAction(f"使用「{os.path.basename(prog)}」")
        a_open_default = menu_open_with.addAction("系统默认程序")
        a_meta = menu.addAction("✏️ 编辑元数据")
        a_cover = menu.addAction("🖼 重新提取封面")
        menu.addSeparator()
        a_loc = menu.addAction("打开文件所在位置")
        a_copy = menu.addAction("复制路径")
        menu.addSeparator()
        a_del = menu.addAction("🗑 从书库删除记录")
        act = menu.exec(self.grid.viewport().mapToGlobal(pos))
        if act == a_open:
            self.open_selected_in_lyuer()
        elif act == a_meta:
            self.edit_selected_meta()
        elif act == a_cover:
            self.extract_selected_cover()
        elif act == a_loc:
            self._open_file_location(rec.path)
        elif act == a_copy:
            QApplication.clipboard().setText(rec.path)
        elif act == a_open_with:
            self.open_with_program(rec, None)
        elif a_remembered is not None and act == a_remembered:
            self.open_with_program(rec, prog)
        elif act == a_open_default:
            self.open_with_default(rec)
        elif act == a_del:
            self.delete_selected()

    def closeEvent(self, event):
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
    app.setApplicationName("MienArchLyuerManager")
    app.setStyle("Fusion")
    _icon = _get_icon_path()
    if _icon:
        app.setWindowIcon(QIcon(_icon))
    app.setFont(QFont("Microsoft YaHei", 10))
    win = ManagerWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
