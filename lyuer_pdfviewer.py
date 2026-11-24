#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lyuer PDF Viewer Module v2.1.2
版权所有 (c) MienArch | 面包图书馆 | 2026
许可证：GNU GPL v3.0 or later
==============================

v2.1.2 (Lyuer v1.6.3.0)：修复过期延迟任务串扰新文档——
go_to_page / set_scroll_ratio 的 QTimer 延迟闭包此前未绑定文档标识，
定时器触发前若用户已打开另一份 PDF，旧文档遗留的滚动任务会作用到
新文档（误翻到旧页码/旧滚动比例）；现闭包捕获装载时路径并在触发时
校验，不匹配即丢弃。



PyMuPDF 仅保留元数据/目录解析（供主程序扫描线程与章节面板使用），
不再承担渲染工作。主程序通过 ``import lyuer_pdfviewer`` 调用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import (
    Qt, Signal, QTimer, QEvent, QPointF, QRectF, QSizeF, QModelIndex,
    QElapsedTimer,
)
from PySide6.QtGui import (
    QColor, QPalette, QPolygonF, QPainter, QGuiApplication, QKeySequence,
    QAction, QShortcut,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QToolButton,
    QStackedWidget, QSplitter, QTreeView, QAbstractItemView, QMenu, QApplication,
)

# --- QtPdf 渲染引擎（PySide6 官方发行版自带 QtPdf / QtPdfWidgets）---
try:
    from PySide6.QtPdf import QPdfDocument, QPdfBookmarkModel
    from PySide6.QtPdfWidgets import QPdfView
    _QTPDF_AVAILABLE = True
except ImportError:
    QPdfDocument = QPdfBookmarkModel = QPdfView = None
    _QTPDF_AVAILABLE = False

# --- PyMuPDF 仅用于元数据/目录解析（扫描线程、章节面板）---
try:
    import pymupdf as fitz  # noqa: N813
except ImportError:
    try:
        import fitz  # noqa: N813  兼容 PyMuPDF < 2.0
    except ImportError:
        fitz = None


def is_pdf_available() -> bool:
    """QtPdf 渲染引擎是否可用（供主程序打开 PDF 前检查）。"""
    return _QTPDF_AVAILABLE


def is_parser_available() -> bool:
    """PyMuPDF（元数据/目录解析）是否可用（供扫描线程检查）。"""
    return fitz is not None


# =============================================================================
# 数据模型（与主程序的 EpubChapter/EpubBookInfo 保持鸭子类型兼容）
# =============================================================================
@dataclass
class PdfChapter:
    id: str
    title: str
    order: int
    page_start: int = 0   # 0-based 起始页
    page_end: int = 0     # 0-based 结束页（不含）


@dataclass
class PdfBookInfo:
    title: str
    author: str
    cover_path: Optional[str] = None
    chapters: List[PdfChapter] = field(default_factory=list)
    images: dict = field(default_factory=dict)


# =============================================================================
# PDF 元数据 / 目录解析（PyMuPDF，不渲染；供主程序扫描与章节面板使用）
# =============================================================================
class PdfParser:
    """解析 PDF 元数据与书签目录；渲染工作由 PdfViewerWidget 完成。"""

    @staticmethod
    def _require_fitz():
        if fitz is None:
            raise ImportError(
                "未安装 PyMuPDF，无法解析 PDF 元数据。请运行: pip install PyMuPDF")

    @staticmethod
    def parse_pdf(file_path: str) -> PdfBookInfo:
        PdfParser._require_fitz()
        doc = fitz.open(file_path)
        try:
            meta = doc.metadata or {}
            title = (meta.get("title") or "").strip() or "未知标题"
            author = (meta.get("author") or "").strip() or "未知作者"

            chapters: List[PdfChapter] = []
            toc = doc.get_toc()  # [[level, title, page], ...]

            if toc:
                for i, (level, title, page) in enumerate(toc):
                    if not title or not title.strip():
                        title = f"章节 {i + 1}"
                    start_page = max(0, page - 1)
                    end_page = len(doc)
                    for j in range(i + 1, len(toc)):
                        if toc[j][0] <= level:
                            end_page = toc[j][2] - 1
                            break
                    chapters.append(PdfChapter(
                        id=f"pdf_ch_{i}",
                        title=title.strip(),
                        order=i,
                        page_start=start_page,
                        page_end=end_page,
                    ))
            else:
                # 无书签时按页浏览，整本书一个“章节”，由阅读器内部按页定位
                chapters.append(PdfChapter(
                    id="pdf_all_pages",
                    title=f"全文（{len(doc)} 页）",
                    order=0,
                    page_start=0,
                    page_end=len(doc),
                ))

            cover_path = None
            images: Dict[str, bytes] = {}
            if len(doc) > 0:
                try:
                    pix = doc.load_page(0).get_pixmap(matrix=fitz.Matrix(1.2, 1.2))
                    cover_path = "pdf_cover.png"
                    images[cover_path] = pix.tobytes("png")
                except Exception:
                    pass

            return PdfBookInfo(title=title, author=author,
                               cover_path=cover_path,
                               chapters=chapters, images=images)
        finally:
            doc.close()

    @staticmethod
    def read_metadata(file_path: str) -> Tuple[str, str]:
        PdfParser._require_fitz()
        doc = fitz.open(file_path)
        try:
            meta = doc.metadata or {}
            title = (meta.get("title") or "").strip() or "未知标题"
            author = (meta.get("author") or "").strip() or "未知作者"
            return title, author
        finally:
            doc.close()


# =============================================================================
# 书签 Role 解析（兼容不同 PySide6 版本的枚举命名）
# =============================================================================
def _resolve_bookmark_role(name, fallback_name=None):
    if QPdfBookmarkModel is None:
        return None
    role_cls = getattr(QPdfBookmarkModel, "Role", None)
    if role_cls is not None:
        if hasattr(role_cls, name):
            return getattr(role_cls, name)
        if fallback_name and hasattr(role_cls, fallback_name):
            return getattr(role_cls, fallback_name)
    if hasattr(QPdfBookmarkModel, name):
        return getattr(QPdfBookmarkModel, name)
    if fallback_name and hasattr(QPdfBookmarkModel, fallback_name):
        return getattr(QPdfBookmarkModel, fallback_name)
    return None


BM_ROLE_PAGE = _resolve_bookmark_role("Page", "PageRole")
BM_ROLE_LOCATION = _resolve_bookmark_role("Location", "LocationRole")
BM_ROLE_ZOOM = _resolve_bookmark_role("Zoom", "ZoomRole")


if _QTPDF_AVAILABLE:

    # =============================================================================
    # 可选区视图（结构移植自 PDF Maker 的 previewr.py）
    # =============================================================================
    class SelectablePdfView(QPdfView):
        """QPdfView 扩展：文本选取高亮 + 缩放请求信号 + 布局几何缓存。"""

        selectionChanged = Signal()
        viewportResized = Signal()
        zoomRequested = Signal(float)   # Ctrl+滚轮 / 捏合缩放的倍率
        pageTurnRequested = Signal(int)  # 触屏左右滑动翻页（-1 上一页 / +1 下一页）

        def __init__(self, parent=None):
            super().__init__(parent)
            self._layout = {}
            self._sel_bounds = {}
            self._sel_text = ""
            self._anchor = None
            self._cursor_doc = None
            self._cursor_vp = None
            self._highlight = QColor(51, 153, 255, 90)

            self._auto_timer = QTimer(self)
            self._auto_timer.setInterval(30)
            self._auto_timer.timeout.connect(self._auto_scroll_step)

            self.viewport().setMouseTracking(True)
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)

            # v2.1.0：触屏手势（捏合缩放 / 上下滑动 / 左右翻页）
            self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
            self.viewport().grabGesture(Qt.GestureType.PinchGesture)
            self._touch_start = None
            self._touch_last = None
            # v2.1.1：手势状态跟踪，避免捏合/多指触点跳变引发滚动乱跳
            self._touch_scroll_total = 0.0   # 本次手势已累计滚动的像素
            self._pinch_active = False       # 捏合手势进行中（期间禁用单指滚动）
            self._touch_elapsed = QElapsedTimer()

            for sig_name in ("zoomFactorChanged", "zoomModeChanged",
                             "pageSpacingChanged", "documentMarginsChanged"):
                try:
                    getattr(self, sig_name).connect(self._update_layout)
                except Exception:
                    pass

        def _screen_res(self):
            try:
                win = self.window()
                screen = win.screen() if win else None
                if screen is None:
                    screen = QGuiApplication.primaryScreen()
                if screen is not None:
                    try:
                        dpi = screen.logicalDotsPerInch()
                        if dpi > 0:
                            return dpi / 72.0
                    except AttributeError:
                        dpi = screen.physicalDotsPerInch()
                        if dpi > 0:
                            return dpi / 72.0
            except Exception:
                pass
            return 96.0 / 72.0

        def _update_layout(self, *args):
            self._layout.clear()
            doc = self.document()
            try:
                if (doc is None
                        or doc.status() != QPdfDocument.Status.Ready
                        or doc.pageCount() <= 0):
                    self.viewport().update()
                    return

                res = max(0.1, self._screen_res())
                view_w = max(1.0, float(self.viewport().width()))
                view_h = max(1.0, float(self.viewport().height()))
                m = self.documentMargins()
                try:
                    spacing = float(self.pageSpacing())
                except Exception:
                    spacing = 0.0
                mode = self.zoomMode()
                zf = self.zoomFactor()

                page_range = range(0, doc.pageCount())
                try:
                    if self.pageMode() == QPdfView.PageMode.SinglePage:
                        cur_p = self.pageNavigator().currentPage()
                        page_range = range(cur_p, cur_p + 1)
                except Exception:
                    pass

                y = float(m.top())
                total_w = 0.0
                items = []
                for page in page_range:
                    orig = QSizeF(doc.pagePointSize(page))
                    if orig.width() <= 0 or orig.height() <= 0:
                        continue
                    if mode == QPdfView.ZoomMode.Custom:
                        scale = zf if zf > 0 else 1.0
                        size = QSizeF(orig.width() * res * scale,
                                      orig.height() * res * scale)
                    elif mode == QPdfView.ZoomMode.FitToWidth:
                        base_w = orig.width() * res
                        avail = max(1.0, view_w - float(m.left()) - float(m.right()))
                        scale = avail / base_w if base_w > 0 else 1.0
                        size = QSizeF(base_w * scale, orig.height() * res * scale)
                    else:
                        vsize = QSizeF(max(1.0, view_w - float(m.left()) - float(m.right())),
                                       max(1.0, view_h - float(m.top()) - float(m.bottom()) - spacing))
                        base = QSizeF(orig.width() * res, orig.height() * res)
                        scaled = base.scaled(vsize, Qt.AspectRatioMode.KeepAspectRatio)
                        scale = scaled.width() / base.width() if base.width() > 0 else 1.0
                        size = scaled

                    if size.width() > total_w:
                        total_w = size.width()
                    items.append([page, QRectF(QPointF(0.0, y), size), scale])
                    y += size.height() + spacing

                total_w += float(m.left()) + float(m.right())
                for page, rect, scale in items:
                    rect.moveLeft((max(total_w, view_w) - rect.width()) / 2.0)
                    self._layout[page] = (rect, scale, res)
            except Exception:
                pass
            self.viewport().update()

        def _to_doc(self, viewport_pos):
            return QPointF(viewport_pos.x() + self.horizontalScrollBar().value(),
                           viewport_pos.y() + self.verticalScrollBar().value())

        def effective_zoom(self):
            try:
                if self.zoomMode() == QPdfView.ZoomMode.Custom:
                    z = self.zoomFactor()
                    if z > 0:
                        return z
                page = -1
                try:
                    nav = self.pageNavigator()
                    page = nav.currentPage() if nav is not None else -1
                except Exception:
                    pass
                if page in self._layout:
                    return self._layout[page][1]
                doc = self.document()
                if doc is not None and doc.status() == QPdfDocument.Status.Ready:
                    if page < 0:
                        page = 0
                    if 0 <= page < doc.pageCount():
                        res = max(0.1, self._screen_res())
                        m = self.documentMargins()
                        avail = max(1.0, float(self.viewport().width()) - float(m.left()) - float(m.right()))
                        w_pt = doc.pagePointSize(page).width()
                        if w_pt > 0 and (w_pt * res) > 0:
                            return avail / (w_pt * res)
            except Exception:
                pass
            return 1.0

        # ---------------- 文本选取 ----------------
        def selected_text(self):
            return self._sel_text

        def clear_selection(self):
            self._anchor = None
            self._reset_selection()

        def select_all(self):
            self._select_pages(None)

        def select_current_page(self):
            page = -1
            try:
                nav = self.pageNavigator()
                if nav is not None:
                    page = nav.currentPage()
            except Exception:
                page = -1
            self._select_pages([page] if page >= 0 else None)

        def _select_pages(self, page_filter):
            doc = self.document()
            try:
                ready = (doc is not None
                         and doc.status() == QPdfDocument.Status.Ready
                         and doc.pageCount() > 0)
            except Exception:
                ready = False
            if not ready:
                return
            if page_filter is None:
                pages = range(doc.pageCount())
            else:
                wanted = set(page_filter)
                pages = [p for p in range(doc.pageCount()) if p in wanted]
            self._reset_selection()
            texts = []
            for page in pages:
                try:
                    sel = doc.getAllText(page)
                except Exception:
                    continue
                if sel is None or not sel.isValid() or not sel.text():
                    continue
                self._sel_bounds[page] = [QPolygonF(poly) for poly in sel.bounds()]
                texts.append(sel.text())
            self._sel_text = "\n".join(texts)
            self.selectionChanged.emit()
            self.viewport().update()

        def _reset_selection(self):
            if self._sel_bounds or self._sel_text:
                self._sel_bounds.clear()
                self._sel_text = ""
                self.selectionChanged.emit()
                self.viewport().update()

        def _update_selection(self):
            self._sel_bounds.clear()
            self._sel_text = ""

            if self._anchor is None or self._cursor_doc is None:
                self.selectionChanged.emit()
                self.viewport().update()
                return

            rect = QRectF(self._anchor, self._cursor_doc).normalized()
            if rect.width() < 3 and rect.height() < 3:
                self.selectionChanged.emit()
                self.viewport().update()
                return

            doc = self.document()
            if doc is None or doc.status() != QPdfDocument.Status.Ready:
                self.selectionChanged.emit()
                self.viewport().update()
                return

            backward = (self._cursor_doc.y() < self._anchor.y() - 0.5 or
                        (abs(self._cursor_doc.y() - self._anchor.y()) <= 0.5 and
                         self._cursor_doc.x() < self._anchor.x()))

            pages = sorted(p for p, (r, s, _res) in self._layout.items()
                           if s > 0 and r.intersects(rect))
            texts = []
            for page in pages:
                page_rect, scale, res = self._layout[page]
                inter = rect.intersected(page_rect)
                if inter.isEmpty():
                    continue

                pt_size = QSizeF(doc.pagePointSize(page))
                pt_w, pt_h = pt_size.width(), pt_size.height()

                def to_pt(p):
                    factor = scale * res
                    if factor <= 0:
                        factor = 1.0
                    x = min(max((p.x() - page_rect.x()) / factor, 0.0), pt_w)
                    y = min(max((p.y() - page_rect.y()) / factor, 0.0), pt_h)
                    return QPointF(x, y)

                is_first = (page == pages[0])
                is_last = (page == pages[-1])
                if backward:
                    start = to_pt(self._cursor_doc if is_first else inter.topLeft())
                    end = to_pt(self._anchor if is_last else inter.bottomRight())
                else:
                    start = to_pt(self._anchor if is_first else inter.topLeft())
                    end = to_pt(self._cursor_doc if is_last else inter.bottomRight())

                sel = None
                if hasattr(doc, "getSelection"):
                    try:
                        sel = doc.getSelection(page, start, end)
                    except Exception:
                        sel = None

                if (sel is None or not sel.isValid()) and hasattr(doc, "getAllText"):
                    page_area = pt_w * pt_h
                    inter_area = inter.width() * inter.height()
                    if page_area > 0 and inter_area >= 0.8 * page_area:
                        try:
                            whole = doc.getAllText(page)
                        except Exception:
                            whole = None
                        if whole is not None and whole.isValid() and whole.text():
                            sel = whole
                    else:
                        r = QRectF(start, end).normalized()
                        for inset in (3.0, 10.0, 25.0):
                            if r.width() <= 2 * inset or r.height() <= 2 * inset:
                                break
                            s2 = QPointF(r.left() + inset, r.top() + inset)
                            e2 = QPointF(r.right() - inset, r.bottom() - inset)
                            try:
                                sel = doc.getSelection(page, s2, e2)
                            except Exception:
                                sel = None
                            if sel is not None and sel.isValid() and sel.text():
                                break

                if sel is not None and sel.isValid() and sel.text():
                    self._sel_bounds[page] = [QPolygonF(poly) for poly in sel.bounds()]
                    texts.append(sel.text())

            self._sel_text = "\n".join(texts)
            self.selectionChanged.emit()
            self.viewport().update()

        # ---------------- 事件 ----------------
        def viewportEvent(self, event):
            t = event.type()
            if t == QEvent.Type.Gesture:
                gesture = event.gesture(Qt.GestureType.PinchGesture)
                if gesture is not None:
                    state = gesture.state()
                    if state == Qt.GestureState.GestureStarted:
                        # 捏合开始：立即中止单指跟踪，防止把捏合误判为滑动
                        self._pinch_active = True
                        self._touch_start = None
                        self._touch_last = None
                    elif state in (Qt.GestureState.GestureFinished,
                                   Qt.GestureState.GestureCanceled):
                        self._pinch_active = False
                    factor = gesture.scaleFactor()
                    if (state != Qt.GestureState.GestureCanceled
                            and 0.2 < factor < 5.0):
                        self.zoomRequested.emit(factor)
                    return True
            elif t == QEvent.Type.TouchBegin:
                points = event.points()
                self._touch_elapsed.start()
                self._touch_scroll_total = 0.0
                if len(points) == 1 and not self._pinch_active:
                    self._touch_start = points[0].position()
                    self._touch_last = self._touch_start
                else:
                    # 多指（含捏合）不做单指滚动基准
                    self._touch_start = None
                    self._touch_last = None
                event.accept()
                return True
            elif t == QEvent.Type.TouchUpdate:
                points = event.points()
                if self._pinch_active or len(points) != 1:
                    # 捏合期间或触点数量变化（两指滚动/一指抬起）时，
                    # 停用滚动基准，避免触点 index 跳变造成滚动条猛跳
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
                # v2.1.1 关键修复：手势被系统识别（如捏合）时会取消触点序列，
                # 取消 ≠ 抬手——绝不触发任何翻页/滚动补偿。
                self._touch_start = None
                self._touch_last = None
                event.accept()
                return True
            elif t == QEvent.Type.TouchEnd:
                # 快滑判定：快速（<350ms）且几乎未发生滚动（<40px）的手势
                # 才按"快滑"补翻一页/翻章；慢速拖拽已由 TouchUpdate 连续滚动，
                # 不再额外跳页（否则拖到哪都再跳一下）。
                if (self._touch_start is not None and self._touch_last is not None
                        and self._touch_elapsed.isValid()
                        and self._touch_elapsed.elapsed() < 350
                        and self._touch_scroll_total < 40):
                    dx = self._touch_last.x() - self._touch_start.x()
                    dy = self._touch_last.y() - self._touch_start.y()
                    vw = max(1.0, float(self.viewport().width()))
                    vh = max(1.0, float(self.viewport().height()))
                    if abs(dx) > vw * 0.18 and abs(dx) > abs(dy) * 1.2:
                        # 左右滑动翻页：左滑下一页，右滑上一页
                        self.pageTurnRequested.emit(-1 if dx > 0 else 1)
                    elif abs(dy) > vh * 0.35:
                        # 快速上下滑动：整页滚动
                        sb = self.verticalScrollBar()
                        sb.setValue(sb.value() + (-1 if dy < 0 else 1) * sb.pageStep())
                self._touch_start = None
                self._touch_last = None
                event.accept()
                return True
            if t == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    self.setFocus(Qt.FocusReason.MouseFocusReason)
                    self._reset_selection()
                    pos = self._to_doc(event.position())
                    self._anchor = pos
                    self._cursor_doc = pos
                    self._cursor_vp = event.position()
                    self._auto_timer.start()
                    return True

            elif t == QEvent.Type.MouseMove:
                self._cursor_vp = event.position()
                if (event.buttons() & Qt.MouseButton.LeftButton
                        and self._anchor is not None):
                    self._cursor_doc = self._to_doc(event.position())
                    self._update_selection()
                    return True

            elif t == QEvent.Type.MouseButtonRelease:
                if event.button() == Qt.MouseButton.LeftButton and self._anchor is not None:
                    self._auto_timer.stop()
                    start = self._anchor
                    self._cursor_doc = self._to_doc(event.position())
                    tiny = (abs(self._cursor_doc.x() - start.x()) < 4 and
                            abs(self._cursor_doc.y() - start.y()) < 4)
                    if tiny:
                        self._anchor = None
                        self._reset_selection()
                        return super().viewportEvent(event)
                    self._update_selection()
                    self._anchor = None
                    return True

            return super().viewportEvent(event)

        def wheelEvent(self, event):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.zoomRequested.emit(
                    1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
                event.accept()
                return
            super().wheelEvent(event)

        def _auto_scroll_step(self):
            if self._cursor_vp is None or self._anchor is None:
                self._auto_timer.stop()
                return
            w = self.viewport().width()
            h = self.viewport().height()
            margin = 28
            step = 18
            dx = dy = 0
            x, y = self._cursor_vp.x(), self._cursor_vp.y()
            if x < margin:
                dx = -step
            elif x > w - margin:
                dx = step
            if y < margin:
                dy = -step
            elif y > h - margin:
                dy = step
            if dx or dy:
                hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
                hb.setValue(hb.value() + dx)
                vb.setValue(vb.value() + dy)
                self._cursor_doc = self._to_doc(self._cursor_vp)
                self._update_selection()

        def paintEvent(self, event):
            super().paintEvent(event)
            if not self._sel_bounds:
                return
            qp = QPainter(self.viewport())
            qp.setRenderHint(QPainter.RenderHint.Antialiasing)
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(self._highlight)
            qp.translate(-self.horizontalScrollBar().value(),
                         -self.verticalScrollBar().value())
            for page, polys in self._sel_bounds.items():
                item = self._layout.get(page)
                if not item:
                    continue
                page_rect, scale, res = item
                ox, oy = page_rect.x(), page_rect.y()
                factor = scale * res
                for poly in polys:
                    mapped = QPolygonF([
                        QPointF(ox + p.x() * factor, oy + p.y() * factor)
                        for p in poly
                    ])
                    qp.drawPolygon(mapped)
            qp.end()

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._update_layout()
            self.viewportResized.emit()


    # =============================================================================
    # PDF 阅读面板：工具栏 + 书签侧栏 + 连续滚动视图
    # （页面结构对齐 PDF Maker 的 previewr.py，配色对齐 Lyuer 暗色主题）
    # =============================================================================
    class PdfViewerWidget(QWidget):
        """QtPdf 阅读面板。

        - 连续滚动（MultiPage）+ 适应页宽，接近浏览器体验；
        - 书签侧栏：点击跳转、翻页自动同步高亮；
        - 工具栏缩放（按钮 / Ctrl+滚轮 / Ctrl+0 复位）；
        - ``pageChanged(current, total)`` 信号汇报当前页（0-based）；
        - 保留 ``go_to_page / current_page / scroll_ratio / set_scroll_ratio /
          set_zoom / zoom_by / page_count / shutdown`` 供主程序调用。
        """

        pageChanged = Signal(int, int)

        BTN_STYLE = """
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
            QPushButton:disabled {
                background-color: #2d2d30;
                color: #555555;
                border: 1px solid #3e3e42;
            }
        """

        def __init__(self, parent=None):
            super().__init__(parent)
            if not _QTPDF_AVAILABLE:
                raise RuntimeError(
                    "QtPdf 渲染引擎不可用（PySide6.QtPdf / QtPdfWidgets 导入失败）。"
                    "请升级 PySide6：pip install -U PySide6")

            self._current_path = ""
            self._page_count = 0
            self._last_page = -1
            self._sidebar_user_on = True

            layout = QVBoxLayout(self)
            layout.setContentsMargins(6, 6, 6, 6)
            layout.setSpacing(6)

            # ---------------- 工具栏 ----------------
            toolbar = QHBoxLayout()
            toolbar.setSpacing(6)

            self.btn_bookmarks = QPushButton("☰ 书签")
            self.btn_bookmarks.setToolTip("显示/隐藏书签面板")
            self.btn_bookmarks.setCheckable(True)
            self.btn_bookmarks.setChecked(True)
            self.btn_bookmarks.toggled.connect(self._on_sidebar_toggled)
            self.btn_bookmarks.setEnabled(False)
            self.btn_bookmarks.setStyleSheet(self.BTN_STYLE)
            toolbar.addWidget(self.btn_bookmarks)

            self.btn_zoom_out = QPushButton("－")
            self.btn_zoom_out.setToolTip("缩小（Ctrl+滚轮 / Ctrl+-）")
            self.btn_zoom_out.setFixedWidth(34)
            self.btn_zoom_out.clicked.connect(self.zoom_out)
            self.btn_zoom_out.setStyleSheet(self.BTN_STYLE)
            toolbar.addWidget(self.btn_zoom_out)

            self.btn_zoom_in = QPushButton("＋")
            self.btn_zoom_in.setToolTip("放大（Ctrl+滚轮 / Ctrl+=）")
            self.btn_zoom_in.setFixedWidth(34)
            self.btn_zoom_in.clicked.connect(self.zoom_in)
            self.btn_zoom_in.setStyleSheet(self.BTN_STYLE)
            toolbar.addWidget(self.btn_zoom_in)

            self.btn_zoom_reset = QPushButton("适应页宽")
            self.btn_zoom_reset.setToolTip("重置缩放（Ctrl+0）")
            self.btn_zoom_reset.clicked.connect(self.zoom_reset)
            self.btn_zoom_reset.setStyleSheet(self.BTN_STYLE)
            toolbar.addWidget(self.btn_zoom_reset)

            self.zoom_label = QLabel("100%")
            self.zoom_label.setStyleSheet("color: #858585;")
            self.zoom_label.setMinimumWidth(46)
            self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            toolbar.addWidget(self.zoom_label)

            self.page_label = QLabel("第 0 / 0 页")
            self.page_label.setStyleSheet("color: #858585;")
            self.page_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            toolbar.addWidget(self.page_label, 1)

            layout.addLayout(toolbar)

            # ---------------- 视图区 ----------------
            self.stack = QStackedWidget()
            self.placeholder = QLabel("尚未打开 PDF 文件")
            self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.placeholder.setWordWrap(True)
            self.placeholder.setStyleSheet("color: #858585; font-size: 14pt;")
            self.stack.addWidget(self.placeholder)

            self.pdf_doc = QPdfDocument(self)
            self.pdf_doc.statusChanged.connect(self._on_doc_status)
            self.pdf_doc.pageCountChanged.connect(self._on_doc_ready_layout)

            self.pdf_view = SelectablePdfView()
            self.pdf_view.setDocument(self.pdf_doc)
            try:
                self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
                self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            except Exception:
                pass

            vp = self.pdf_view.viewport()
            pal = vp.palette()
            pal.setColor(QPalette.ColorRole.Window, QColor("#525659"))
            vp.setPalette(pal)
            vp.setAutoFillBackground(True)

            self.stack.addWidget(self.pdf_view)
            self.stack.setCurrentWidget(self.placeholder)

            # 书签侧栏
            self.sidebar = QWidget()
            self.sidebar.setMinimumWidth(170)
            side_layout = QVBoxLayout(self.sidebar)
            side_layout.setContentsMargins(4, 4, 4, 4)
            side_layout.setSpacing(4)

            side_header = QHBoxLayout()
            side_header.setSpacing(2)
            self.sidebar_title = QLabel("书签")
            self.sidebar_title.setStyleSheet(
                "font-weight: bold; color: #cccccc;")
            side_header.addWidget(self.sidebar_title, 1)
            self.btn_sidebar_close = QToolButton()
            self.btn_sidebar_close.setText("×")
            self.btn_sidebar_close.setAutoRaise(True)
            self.btn_sidebar_close.setToolTip("隐藏书签面板")
            self.btn_sidebar_close.setStyleSheet("color: #cccccc;")
            self.btn_sidebar_close.clicked.connect(self._hide_sidebar)
            side_header.addWidget(self.btn_sidebar_close)
            side_layout.addLayout(side_header)

            self.bookmark_view = QTreeView()
            self.bookmark_view.setHeaderHidden(True)
            self.bookmark_view.setUniformRowHeights(True)
            self.bookmark_view.setAnimated(True)
            self.bookmark_view.setEditTriggers(
                QAbstractItemView.EditTrigger.NoEditTriggers)
            self._apply_bookmark_palette()

            self.bookmark_model = None
            if QPdfBookmarkModel is not None:
                self.bookmark_model = QPdfBookmarkModel(self)
                self.bookmark_model.setDocument(self.pdf_doc)
                self.bookmark_view.setModel(self.bookmark_model)
                self.bookmark_view.clicked.connect(self._on_bookmark_clicked)
                self.bookmark_view.activated.connect(self._on_bookmark_clicked)
                self.bookmark_model.modelReset.connect(self._on_outline_data_changed)
                self.bookmark_model.rowsInserted.connect(self._on_outline_data_changed)

            side_layout.addWidget(self.bookmark_view, 1)

            self.splitter = QSplitter(Qt.Orientation.Horizontal)
            self.splitter.setChildrenCollapsible(False)
            self.splitter.addWidget(self.sidebar)
            self.splitter.addWidget(self.stack)
            self.splitter.setStretchFactor(0, 0)
            self.splitter.setStretchFactor(1, 1)
            layout.addWidget(self.splitter, 1)
            self.sidebar.setVisible(False)

            # ---------------- 信号与快捷键 ----------------
            self.pdf_view.zoomRequested.connect(self.zoom_by)
            self.pdf_view.pageTurnRequested.connect(
                lambda d: self.go_to_page(self.current_page() + d))
            self.pdf_view.selectionChanged.connect(self._on_selection_changed)
            self.pdf_view.setContextMenuPolicy(
                Qt.ContextMenuPolicy.CustomContextMenu)
            self.pdf_view.customContextMenuRequested.connect(
                self._show_context_menu)

            try:
                self.pdf_view.zoomFactorChanged.connect(self._update_zoom_label)
            except Exception:
                pass
            self.pdf_view.viewportResized.connect(self._update_zoom_label)
            try:
                self.pdf_view.pageNavigator().currentPageChanged.connect(
                    lambda _p: self._sync_bookmark_to_page(self.current_page()))
            except Exception:
                pass

            self._scroll_timer = QTimer(self)
            self._scroll_timer.setSingleShot(True)
            self._scroll_timer.setInterval(50)
            self._scroll_timer.timeout.connect(self._report_page)
            self.pdf_view.verticalScrollBar().valueChanged.connect(
                lambda _v: self._scroll_timer.start())

            self._build_actions()

        # ------------------------------------------------------------------
        # 动作 / 快捷键 / 右键菜单
        # ------------------------------------------------------------------
        def _build_actions(self):
            def _add(action):
                action.setShortcutContext(
                    Qt.ShortcutContext.WidgetWithChildrenShortcut)
                self.addAction(action)
                return action

            self.copy_action = QAction("复制", self)
            self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
            self.copy_action.triggered.connect(self._copy_selection)
            self.copy_action.setEnabled(False)
            _add(self.copy_action)

            self.select_page_action = QAction("选中当前页", self)
            self.select_page_action.setShortcut(QKeySequence("Ctrl+Shift+A"))
            self.select_page_action.triggered.connect(
                self.pdf_view.select_current_page)
            _add(self.select_page_action)

            self.select_all_action = QAction("全选", self)
            self.select_all_action.setShortcut(QKeySequence.StandardKey.SelectAll)
            self.select_all_action.triggered.connect(self.pdf_view.select_all)
            _add(self.select_all_action)

            def _sc(key, slot):
                s = QShortcut(QKeySequence(key), self)
                s.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                s.activated.connect(slot)

            _sc(Qt.Key.Key_PageDown,
                lambda: self.go_to_page(self.current_page() + 1))
            _sc(Qt.Key.Key_Space,
                lambda: self.go_to_page(self.current_page() + 1))
            _sc(Qt.Key.Key_PageUp,
                lambda: self.go_to_page(self.current_page() - 1))
            _sc("Ctrl+0", self.zoom_reset)
            _sc("Ctrl+=", self.zoom_in)
            _sc("Ctrl+-", self.zoom_out)
            _sc(Qt.Key.Key_Escape, self.pdf_view.clear_selection)

        def _on_selection_changed(self):
            self.copy_action.setEnabled(
                bool(self.pdf_view.selected_text().strip()))

        def _copy_selection(self):
            text = self.pdf_view.selected_text()
            if text:
                QGuiApplication.clipboard().setText(text)

        def _show_context_menu(self, pos):
            self._on_selection_changed()
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
            menu.addAction(self.copy_action)
            menu.addSeparator()
            menu.addAction(self.select_page_action)
            menu.addAction(self.select_all_action)
            menu.exec(self.pdf_view.mapToGlobal(pos))

        # ------------------------------------------------------------------
        # 文档装载
        # ------------------------------------------------------------------
        def load_document(self, file_path: str):
            file_path = os.path.normpath(os.path.abspath(file_path))
            if not os.path.exists(file_path):
                raise FileNotFoundError(file_path)

            self._current_path = ""
            self._last_page = -1
            self._page_count = 0
            self.page_label.setText("第 0 / 0 页")

            self.stack.setCurrentWidget(self.placeholder)
            self.placeholder.setText("正在加载 PDF…")
            self.btn_bookmarks.setEnabled(False)
            self.sidebar.setVisible(False)
            self.pdf_view.clear_selection()

            # 控件禁用必须发生在 load() 之前：load 会同步派发 statusChanged，
            # 若之后再禁用会覆盖 Ready 分支的启用结果（previewr v1.17.9 记录的
            # 同类时序竞态，打印按钮永久灰色即由此而来）。
            self.pdf_doc.load(file_path)

            self.stack.setCurrentWidget(self.pdf_view)
            self._current_path = file_path

        def shutdown(self):
            """主程序切换书籍/退出时调用；QtPdf 文档随下一次 load 覆盖。"""
            try:
                self.pdf_view.clear_selection()
            except Exception:
                pass
            self.sidebar.setVisible(False)

        def _on_doc_status(self, status):
            if status == QPdfDocument.Status.Error:
                self.stack.setCurrentWidget(self.placeholder)
                self.placeholder.setText(
                    "PDF 加载失败，文件可能已损坏或受密码保护。")
                self.btn_bookmarks.setEnabled(False)
                self.sidebar.setVisible(False)
                self._page_count = 0
                self._last_page = -1
                self.page_label.setText("第 0 / 0 页")
            elif status == QPdfDocument.Status.Ready:
                self.stack.setCurrentWidget(self.pdf_view)
                self.pdf_view.clear_selection()
                self.pdf_view._update_layout()
                self._page_count = self.pdf_doc.pageCount()
                self._update_zoom_label()
                self._report_page(force=True)
                self._refresh_outline_ui()
            else:
                self.btn_bookmarks.setEnabled(False)
                self.sidebar.setVisible(False)

        def _on_doc_ready_layout(self, *args):
            self._page_count = self.pdf_doc.pageCount()
            try:
                self.pdf_view._update_layout()
            except Exception:
                pass
            self._update_zoom_label()
            QTimer.singleShot(0, self._refresh_outline_ui)

        # ------------------------------------------------------------------
        # 页面定位 / 进度
        # ------------------------------------------------------------------
        def page_count(self) -> int:
            return self._page_count

        def current_page(self) -> int:
            """视口顶部所在的页码（0-based）。"""
            if self._page_count <= 0:
                return 0
            top = self.pdf_view.verticalScrollBar().value()
            for page in sorted(self.pdf_view._layout):
                rect = self.pdf_view._layout[page][0]
                if top < rect.y() + rect.height():
                    return page
            return self._page_count - 1

        def scroll_ratio(self) -> float:
            sb = self.pdf_view.verticalScrollBar()
            if sb.maximum() <= 0:
                return 0.0
            return max(0.0, min(1.0, sb.value() / sb.maximum()))

        def set_scroll_ratio(self, ratio: float):
            # v2.1.2：闭包绑定装载时的文档路径；定时器触发前若已切换文档，
            # 旧任务不得作用于新打开的 PDF
            doc_path = self._current_path

            def _apply():
                if self._current_path != doc_path:
                    return
                sb = self.pdf_view.verticalScrollBar()
                sb.setValue(int(round(
                    max(0.0, min(1.0, ratio)) * sb.maximum())))
            QTimer.singleShot(80, _apply)

        def go_to_page(self, page_no: int):
            if self._page_count <= 0:
                return
            page_no = max(0, min(page_no, self._page_count - 1))
            try:
                nav = self.pdf_view.pageNavigator()
                if nav is not None:
                    nav.jump(page_no, QPointF())
            except Exception:
                pass

            # MultiPage 连续滚动模式下 jump 不一定驱动视口滚动条，
            # 依据布局几何手动对齐（previewr v1.17.7 同类问题）。
            # v2.1.2：闭包绑定装载时路径，防止触发时已切换文档而误滚动新文档。
            doc_path = self._current_path

            def _scroll_to():
                if self._current_path != doc_path:
                    return
                layout = self.pdf_view._layout
                if page_no in layout:
                    self.pdf_view.verticalScrollBar().setValue(
                        int(layout[page_no][0].y()))
                self._report_page(force=True)

            QTimer.singleShot(60, _scroll_to)
            self._last_page = page_no
            self.page_label.setText(f"第 {page_no + 1} / {self._page_count} 页")
            self.pageChanged.emit(page_no, self._page_count)

        def _report_page(self, force: bool = False):
            if self._page_count <= 0:
                return
            cur = self.current_page()
            if cur != self._last_page or force:
                self._last_page = cur
                self.page_label.setText(f"第 {cur + 1} / {self._page_count} 页")
                self.pageChanged.emit(cur, self._page_count)
                self._sync_bookmark_to_page(cur)

        # ------------------------------------------------------------------
        # 缩放
        # ------------------------------------------------------------------
        def _effective_zoom(self) -> float:
            return self.pdf_view.effective_zoom()

        def _update_zoom_label(self, *args):
            try:
                z = self._effective_zoom()
                self.zoom_label.setText(f"{int(round(z * 100))}%")
            except Exception:
                pass

        def zoom_in(self):
            self.zoom_by(1.2)

        def zoom_out(self):
            self.zoom_by(1 / 1.2)

        def zoom_reset(self):
            try:
                self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            except Exception:
                pass
            self._update_zoom_label()

        def set_zoom(self, zoom: float):
            self._set_zoom(zoom)

        def zoom_by(self, factor: float):
            self._set_zoom(self._effective_zoom() * factor)

        def _set_zoom(self, factor: float):
            factor = max(0.25, min(4.0, factor))
            try:
                self.pdf_view.setZoomMode(QPdfView.ZoomMode.Custom)
                self.pdf_view.setZoomFactor(factor)
            except Exception:
                pass
            self._update_zoom_label()

        # ------------------------------------------------------------------
        # 书签侧栏（结构对齐 previewr.py，配色对齐 Lyuer 暗色主题）
        # ------------------------------------------------------------------
        def _has_outline(self):
            if self.bookmark_model is None:
                return False
            try:
                return self.bookmark_model.rowCount() > 0
            except Exception:
                return False

        def _on_sidebar_toggled(self, checked):
            self._sidebar_user_on = checked
            self._apply_sidebar_state()

        def _hide_sidebar(self):
            self.btn_bookmarks.setChecked(False)

        def _apply_sidebar_state(self):
            try:
                ready = (self.pdf_doc is not None
                         and self.pdf_doc.status() == QPdfDocument.Status.Ready)
            except Exception:
                ready = False
            show = (self._sidebar_user_on and ready and self._has_outline()
                    and self.stack.currentWidget() is self.pdf_view)
            if show and not self.sidebar.isVisible():
                total = max(self.splitter.width(), 400)
                self.sidebar.setVisible(True)
                self.splitter.setSizes([230, total - 230])
            elif not show and self.sidebar.isVisible():
                self.sidebar.setVisible(False)

            if self.btn_bookmarks.isChecked() != self.sidebar.isVisible():
                self.btn_bookmarks.blockSignals(True)
                self.btn_bookmarks.setChecked(self.sidebar.isVisible())
                self.btn_bookmarks.blockSignals(False)

        def _apply_bookmark_palette(self):
            """按应用当前主题（深/浅）调整书签树配色。"""
            try:
                app = QApplication.instance()
                if app is None:
                    return
                dark = app.palette().color(
                    QPalette.ColorRole.Window).lightness() < 128
                pal = self.bookmark_view.palette()
                if dark:
                    pal.setColor(QPalette.ColorRole.Base, QColor("#2d2d2d"))
                    pal.setColor(QPalette.ColorRole.Text, QColor("#cccccc"))
                    pal.setColor(QPalette.ColorRole.Highlight, QColor("#37373d"))
                    pal.setColor(QPalette.ColorRole.HighlightedText,
                                 QColor("#ffffff"))
                else:
                    pal.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
                    pal.setColor(QPalette.ColorRole.Text, QColor("#333333"))
                    pal.setColor(QPalette.ColorRole.Highlight, QColor("#094771"))
                    pal.setColor(QPalette.ColorRole.HighlightedText,
                                 QColor("#ffffff"))
                self.bookmark_view.setPalette(pal)
            except Exception:
                pass

        def _on_outline_data_changed(self, *args):
            self._refresh_outline_ui()

        def _refresh_outline_ui(self):
            self._apply_bookmark_palette()
            try:
                ready = (self.pdf_doc is not None
                         and self.pdf_doc.status() == QPdfDocument.Status.Ready)
            except Exception:
                ready = False
            has_out = self._has_outline()
            self.btn_bookmarks.setEnabled(ready and has_out)
            if ready and has_out:
                try:
                    self.bookmark_view.expandAll()
                except Exception:
                    pass
            self._apply_sidebar_state()
            if ready and has_out:
                self._sync_bookmark_to_page(self.current_page())

        def _iter_bookmark_indices(self, parent=QModelIndex()):
            if self.bookmark_model is None:
                return
            depth = 0
            p = parent
            while p.isValid():
                depth += 1
                p = p.parent()
            if depth > 64:
                return
            try:
                rows = self.bookmark_model.rowCount(parent)
            except Exception:
                return
            for row in range(rows):
                idx = self.bookmark_model.index(row, 0, parent)
                if not idx.isValid():
                    continue
                yield idx
                yield from self._iter_bookmark_indices(idx)

        def _sync_bookmark_to_page(self, page):
            if (self.bookmark_model is None or page is None
                    or not self.sidebar.isVisible() or BM_ROLE_PAGE is None):
                return
            try:
                best, best_page = None, -1
                for idx in self._iter_bookmark_indices():
                    p = idx.data(BM_ROLE_PAGE)
                    if p is not None and 0 <= p <= page and p >= best_page:
                        best_page = p
                        best = idx
                if best is None:
                    return
                self.bookmark_view.setCurrentIndex(best)
                anc = best.parent()
                while anc.isValid():
                    self.bookmark_view.expand(anc)
                    anc = anc.parent()
                self.bookmark_view.scrollTo(best)
            except Exception:
                pass

        def _on_bookmark_clicked(self, index):
            if not index.isValid() or self.bookmark_model is None                     or BM_ROLE_PAGE is None:
                return
            try:
                page_val = index.data(BM_ROLE_PAGE)
                if page_val is None or page_val < 0:
                    return
                page = int(page_val)

                loc = None
                if BM_ROLE_LOCATION is not None:
                    loc = index.data(BM_ROLE_LOCATION)
                if not isinstance(loc, QPointF):
                    loc = QPointF()

                zoom = 0.0
                if BM_ROLE_ZOOM is not None:
                    z = index.data(BM_ROLE_ZOOM)
                    if z is not None and z > 0:
                        zoom = float(z)

                nav = self.pdf_view.pageNavigator()
                if nav is not None:
                    try:
                        if zoom > 0:
                            nav.jump(page, loc, zoom)
                        else:
                            nav.jump(page, loc)
                    except Exception:
                        try:
                            nav.jump(page, loc)
                        except Exception:
                            pass

                if page in self.pdf_view._layout:
                    page_rect, scale, res = self.pdf_view._layout[page]
                    target_y = page_rect.y()
                    if loc.y() > 0:
                        factor = scale * res
                        target_y += loc.y() * factor
                    self.pdf_view.verticalScrollBar().setValue(int(target_y))
            except Exception:
                pass

else:
    # QtPdf 不可用时提供占位类，主程序捕获异常后禁用 PDF 功能
    class PdfViewerWidget(QWidget):  # type: ignore[no-redef]
        def __init__(self, parent=None):
            super().__init__(parent)
            raise RuntimeError(
                "QtPdf 渲染引擎不可用（PySide6.QtPdf / QtPdfWidgets 导入失败）。"
                "请升级 PySide6：pip install -U PySide6")
