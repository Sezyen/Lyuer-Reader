#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lyuer EPUB Viewer Module v1.0.4
版权所有 (c) MienArch | 面包图书馆 | 2026
许可证：GNU GPL v3.0 or later
================================

v1.0.4 (Lyuer v1.6.3.0)：修复章内锚点/滚动位置恢复「失败一次即静默丢失」——
_scroll_anchor_now / _scroll_ratio_now 的异常分支此前只回填 pending 字段
却不重启恢复定时器，runJavaScript 偶发失败（页面尚未就绪等）后再无重试；
现异常时重启 _restore_timer 继续按 40ms 轮询重试，直至成功或新章节装载。



Qt WebEngine 要求 PySide6 安装 WebEngine 组件：

    pip install PySide6 PySide6-WebEngine
"""

from __future__ import annotations

import base64
import json
import mimetypes
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

import warnings

from lxml import etree
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from PySide6.QtCore import Qt, QUrl, QTimer, Signal, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

# Qt WebEngine 要求共享 OpenGL Context；必须在 QApplication 创建前设置。
# 本模块由主程序在创建 QApplication 之前导入。
try:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
except Exception:
    pass



# =============================================================================
# EPUB 页面导航拦截
# =============================================================================
# Qt WebEngine 会尝试按 base URL 解析正文内链接；about:blank 不是层级 URL，
# 相对 href（目录页、交叉引用）无法解析。这里使用一个专用的保留域名作为
# 层级 base，点击链接时由 acceptNavigationRequest 统一拦截，绝不对其发起
# 真实网络请求。
EPUB_BASE_URL = "https://lyuer.invalid/lyuer-epub/"
# 链接按 EPUB_BASE_URL 解析后 path 自带此前缀（如 "/lyuer-epub/OEBPS/ch1.xhtml"），
# 匹配章节文件前必须剥离（v1.0.1）
EPUB_BASE_PATH = "/lyuer-epub"


class _EpubWebPage(QWebEnginePage):
    """拦截 EPUB 正文内的链接点击（目录页条目、章节交叉引用）。

    所有链接导航都被取消并转为 linkActivated(path, anchor) 信号，
    由 EpubViewerWidget 解析目标章节；其余导航（setHtml 装载）保持默认。
    """

    linkActivated = Signal(str, str)  # 目标文件（包内相对路径，可为空）, 锚点

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        try:
            if (is_main_frame
                    and nav_type
                    == QWebEnginePage.NavigationType.NavigationTypeLinkClicked):
                path = url.path() or ""
                fragment = url.fragment() or ""
                # v1.0.3 关键修复：绝不能在导航请求尚未返回时同步跳转章节。
                # 链接槽函数会调用 set_chapter_content() -> setHtml() 替换文档，
                # 在 acceptNavigationRequest 执行期间重入文档装载会触发 Qt
                # WebEngine 内部断言，进程以「追踪或断点陷阱（核心已转储）」
                # 闪退。延迟到事件循环下一轮、导航彻底结束后再发信号。
                QTimer.singleShot(
                    0, lambda p=path, f=fragment: self.linkActivated.emit(p, f))
                return False
        except Exception:
            pass
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


def is_epub_available() -> bool:
    """Qt WebEngine EPUB 渲染引擎是否可用（供主程序打开 EPUB 前检查）。"""
    return True


# =============================================================================
# 数据模型
# =============================================================================
@dataclass
class EpubChapter:
    id: str
    title: str
    content: str
    order: int
    file_name: str = ""
    anchor: str = ""
    page_start: int = -1   # PDF 专用：懒加载页码起始
    page_end: int = -1     # PDF 专用：懒加载页码结束


@dataclass
class EpubBookInfo:
    title: str
    author: str
    cover_path: Optional[str] = None
    chapters: List[EpubChapter] = field(default_factory=list)
    images: dict = field(default_factory=dict)


# =============================================================================
# EPUB 解析器（v2.4 重写）
# =============================================================================
class EpubParser:
    NAV_BASENAMES = {
        'toc.ncx', 'nav.xhtml', 'nav.html', 'nav.htm',
        'toc.xhtml', 'toc.html', 'toc.htm',
    }
    FRONT_BASENAMES = {
        'cover.xhtml', 'cover.html', 'cover.htm',
        'titlepage.xhtml', 'titlepage.html',
        'copyright.xhtml', 'copyright.html',
        'colophon.xhtml', 'colophon.html',
    }

    _ENC_RE = re.compile(
        r'<\?xml[^>]*encoding\s*=\s*["\']([\w\-]+)["\']'
        r'|<meta[^>]*charset\s*=\s*["\']?\s*([\w\-]+)',
        re.IGNORECASE,
    )

    @staticmethod
    def parse_epub(file_path: str) -> EpubBookInfo:
        last_err = None
        try:
            info = EpubParser._parse_via_zip(file_path)
            if info.chapters:
                return info
        except Exception as e:
            last_err = e
        try:
            info = EpubParser._parse_via_ebooklib(file_path)
            if info.chapters:
                return info
            if not info.chapters and last_err is not None:
                raise last_err
            return info
        except Exception as e:
            if last_err is not None:
                raise Exception(f"zip 解析失败: {last_err}; ebooklib 兜底失败: {e}")
            raise

    @staticmethod
    def read_metadata(file_path: str) -> Tuple[str, str]:
        try:
            with zipfile.ZipFile(file_path) as zf:
                opf_path, opf = EpubParser._read_container_and_opf(zf)
                title, author = EpubParser._opf_metadata(opf)
                return title, author
        except Exception:
            pass
        try:
            from ebooklib import epub
            book = epub.read_epub(file_path)
            title = book.get_metadata('DC', 'title')
            author = book.get_metadata('DC', 'creator')
            return (title[0][0].strip() if title and title[0][0] else "未知标题",
                    " / ".join(a[0].strip() for a in author if a and a[0]) or "未知作者")
        except Exception as e:
            raise e

    @staticmethod
    def _norm(name: str) -> str:
        return unquote(name or "").replace('\\', '/').lstrip('/')

    @staticmethod
    def _is_nav_doc(name: str, media_type: str = "", properties: str = "") -> bool:
        if 'nav' in (properties or '').split():
            return True
        if media_type == 'application/x-dtbncx+xml':
            return True
        return posixpath.basename(name).lower() in EpubParser.NAV_BASENAMES

    @staticmethod
    def _decode_bytes(raw) -> str:
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
        m = EpubParser._ENC_RE.search(head)
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
    def _body_html(content: str) -> str:
        soup = BeautifulSoup(content, 'lxml')
        body = soup.find('body')
        return body.decode_contents() if body else content

    @staticmethod
    def _has_content(html: str) -> bool:
        soup = BeautifulSoup(html, 'lxml')
        if soup.find(['img', 'image', 'svg', 'video', 'audio']):
            return True
        return bool(soup.get_text(strip=True))

    @staticmethod
    def _extract_text_title(content: str) -> str:
        soup = BeautifulSoup(content, 'lxml')
        for tag in ('h1', 'h2', 'h3'):
            t = soup.find(tag)
            if t and t.get_text(strip=True):
                return t.get_text(strip=True)
        t = soup.find('title')
        if t and t.get_text(strip=True):
            return t.get_text(strip=True)
        return ""

    @staticmethod
    def _resolve_href(docs: Dict[str, bytes], href: str) -> Optional[str]:
        if not href:
            return None
        href = EpubParser._norm(href).split('#')[0]
        if not href:
            return None
        if href in docs:
            return href
        base = posixpath.basename(href).lower()
        for name in docs:
            if posixpath.basename(name).lower() == base:
                return name
        for name in docs:
            if name.lower().endswith('/' + href.lower()) or href.lower().endswith('/' + name.lower()):
                return name
        return None

    @staticmethod
    def _locate_offsets(body, elements: list, html: str) -> list:
        offsets = []
        cursor = 0
        for elem in elements:
            frag = str(elem)
            pos = html.find(frag, cursor)
            if pos == -1:
                pos = html.find(frag)
            if pos == -1:
                continue
            offsets.append((pos, elem))
            cursor = pos + 1
        offsets.sort(key=lambda x: x[0])
        deduped = []
        for pos, elem in offsets:
            if deduped and deduped[-1][0] == pos:
                continue
            deduped.append((pos, elem))
        return deduped

    @staticmethod
    def _split_by_anchors(html_content: str, file_name: str, anchors: list) -> list:
        soup = BeautifulSoup(html_content, 'lxml')
        body = soup.find('body')
        if not body:
            title = anchors[0][1] if anchors else "章节"
            return [EpubChapter(id=file_name, title=title, content=html_content,
                                order=anchors[0][2] if anchors else 0,
                                file_name=file_name)]

        html = body.decode_contents()
        sorted_anchors = sorted(anchors, key=lambda x: x[2])

        elem_list = []
        elem_info = {}
        for anchor, title, order in sorted_anchors:
            if not anchor:
                continue
            elem = body.find(id=anchor) or body.find(attrs={'name': anchor})
            if elem is None or id(elem) in elem_info:
                continue
            elem_list.append(elem)
            elem_info[id(elem)] = (anchor, title, order)

        offsets = EpubParser._locate_offsets(body, elem_list, html)

        if not offsets:
            return [EpubChapter(id=file_name, title=sorted_anchors[0][1],
                                content=html, order=sorted_anchors[0][2],
                                file_name=file_name)]

        result = []
        lead_html = html[:offsets[0][0]]
        located_anchors = {elem_info[id(e)][0] for e in elem_list}
        first_unlocated = next(
            (a for a in sorted_anchors if not a[0] or a[0] not in located_anchors),
            None)
        if first_unlocated is not None and EpubParser._has_content(lead_html):
            result.append(EpubChapter(
                id=file_name, title=first_unlocated[1], content=lead_html,
                order=first_unlocated[2], file_name=file_name))
            lead_html = None

        for i, (pos, elem) in enumerate(offsets):
            anchor, title, order = elem_info[id(elem)]
            end = offsets[i + 1][0] if i + 1 < len(offsets) else len(html)
            chunk = html[pos:end]
            if i == 0 and lead_html:
                chunk = lead_html + chunk
            result.append(EpubChapter(
                id=f"{file_name}#{anchor}", title=title, content=chunk,
                order=order, file_name=file_name, anchor=anchor))

        return result

    @staticmethod
    def _split_by_headings(html_content: str, file_name: str, base_id: str) -> list:
        soup = BeautifulSoup(html_content, 'lxml')
        body = soup.find('body')
        if not body:
            return []

        headings = body.find_all(['h1', 'h2', 'h3'])
        if len(headings) <= 1:
            return []

        html = body.decode_contents()
        offsets = EpubParser._locate_offsets(body, headings, html)
        if len(offsets) <= 1:
            return []

        result = []
        for i, (pos, elem) in enumerate(offsets):
            title = elem.get_text(strip=True) or f"章节 {i + 1}"
            end = offsets[i + 1][0] if i + 1 < len(offsets) else len(html)
            result.append(EpubChapter(
                id=f"{base_id}#{i}", title=title, content=html[pos:end],
                order=i, file_name=file_name))
        return result

    @staticmethod
    def _assemble_chapters(docs: Dict[str, bytes],
                           doc_meta: Dict[str, Tuple[str, str]],
                           spine_names: List[str],
                           toc_entries: List[Tuple[str, str]]) -> List[EpubChapter]:
        file_anchors: Dict[str, list] = {}
        for order, (href, t) in enumerate(toc_entries):
            file_part, _, anchor = (href or "").partition('#')
            real = EpubParser._resolve_href(docs, file_part)
            if not real:
                continue
            mt, props = doc_meta.get(real, ('', ''))
            if EpubParser._is_nav_doc(real, mt, props):
                continue
            file_anchors.setdefault(real, []).append(
                (EpubParser._norm(anchor), (t or "").strip() or "无标题", order))

        chapters: List[EpubChapter] = []
        used = set()
        seq = 0

        def add_file(name: str):
            nonlocal seq
            raw = docs.get(name)
            if raw is None:
                return
            content = EpubParser._decode_bytes(raw)
            anchors = file_anchors.get(name)
            if anchors:
                splits = EpubParser._split_by_anchors(content, name, anchors)
            else:
                title = EpubParser._extract_text_title(content) or f"章节 {seq + 1}"
                splits = [EpubChapter(id=name, title=title,
                                      content=EpubParser._body_html(content),
                                      order=seq, file_name=name)]
            for ch in splits:
                ch.order = seq
                seq += 1
                chapters.append(ch)
            used.add(name)

        for name in spine_names:
            if name in used or name not in docs:
                continue
            mt, props = doc_meta.get(name, ('', ''))
            if EpubParser._is_nav_doc(name, mt, props):
                continue
            if name not in file_anchors and \
                    posixpath.basename(name).lower() in EpubParser.FRONT_BASENAMES \
                    and len(spine_names) > 1:
                continue
            add_file(name)

        for name in file_anchors:
            if name not in used:
                add_file(name)

        chapters = [ch for ch in chapters if EpubParser._has_content(ch.content)]

        if not chapters:
            for name in sorted(docs):
                mt, props = doc_meta.get(name, ('', ''))
                if EpubParser._is_nav_doc(name, mt, props):
                    continue
                if posixpath.basename(name).lower() in EpubParser.FRONT_BASENAMES:
                    continue
                add_file(name)
            chapters = [ch for ch in chapters if EpubParser._has_content(ch.content)]

        if 0 < len(chapters) <= 2:
            final_chapters = []
            for ch in chapters:
                if len(ch.content) > 3000:
                    split = EpubParser._split_by_headings(ch.content, ch.file_name, ch.id)
                    final_chapters.extend(split if len(split) > 1 else [ch])
                else:
                    final_chapters.append(ch)
            chapters = final_chapters

        for i, ch in enumerate(chapters):
            ch.order = i
        return chapters

    @staticmethod
    def _xml_recover(raw: bytes):
        parser = etree.XMLParser(recover=True, resolve_entities=False,
                                 no_network=True, huge_tree=True)
        return etree.fromstring(raw, parser=parser)

    @staticmethod
    def _read_container_and_opf(zf: zipfile.ZipFile):
        names = zf.namelist()
        lower_map = {n.lower(): n for n in names}

        container = None
        for cand_low, real in lower_map.items():
            if cand_low.endswith('meta-inf/container.xml'):
                container = real
                break

        opf_path = None
        if container:
            try:
                root = EpubParser._xml_recover(zf.read(container))
                rootfile = root.find('.//{*}rootfile')
                if rootfile is not None:
                    full_path = rootfile.get('full-path')
                    if full_path:
                        prefix = container[:container.lower().index('meta-inf/')]
                        for cand in (prefix + full_path, full_path):
                            if cand in lower_map.get(cand.lower(), cand) and cand in names:
                                opf_path = cand
                                break
                        if opf_path is None and full_path.lower() in lower_map:
                            opf_path = lower_map[full_path.lower()]
            except Exception:
                opf_path = None

        if opf_path is None:
            opfs = [n for n in names if n.lower().endswith('.opf')]
            if not opfs:
                raise Exception("找不到 OPF 包文件（非标准 EPUB 或文件损坏）")
            opf_path = sorted(opfs, key=len)[0]

        opf = EpubParser._xml_recover(zf.read(opf_path))
        return opf_path, opf

    @staticmethod
    def _opf_metadata(opf) -> Tuple[str, str]:
        DC = '{http://purl.org/dc/elements/1.1/}'
        title = "未知标题"
        t = opf.find(f'.//{DC}title')
        if t is not None and t.text and t.text.strip():
            title = t.text.strip()
        creators = [c.text.strip() for c in opf.findall(f'.//{DC}creator')
                    if c.text and c.text.strip()]
        author = " / ".join(creators) if creators else "未知作者"
        return title, author

    @staticmethod
    def _parse_ncx(raw: bytes) -> List[Tuple[str, str]]:
        entries = []
        try:
            root = EpubParser._xml_recover(raw)
        except Exception:
            return entries
        for np in root.iter('{*}navPoint'):
            label = np.find('.//{*}navLabel/{*}text')
            content = np.find('{*}content')
            src = content.get('src') if content is not None else None
            text = ''.join(label.itertext()).strip() if label is not None else ""
            if src:
                entries.append((src, text or "无标题"))
        return entries

    @staticmethod
    def _parse_nav(raw: bytes) -> List[Tuple[str, str]]:
        entries = []
        try:
            soup = BeautifulSoup(EpubParser._decode_bytes(raw), 'lxml')
        except Exception:
            return entries
        nav = None
        for tag in soup.find_all('nav'):
            epub_type = tag.get('epub:type') or tag.get('epub\\:type') or ''
            if 'toc' in epub_type:
                nav = tag
                break
        if nav is None:
            navs = soup.find_all('nav')
            nav = navs[0] if navs else None
        if nav is None:
            return entries
        for a in nav.find_all('a'):
            href = a.get('href')
            text = a.get_text(strip=True)
            if href:
                entries.append((href, text or "无标题"))
        return entries

    @staticmethod
    def _parse_via_zip(file_path: str) -> EpubBookInfo:
        with zipfile.ZipFile(file_path) as zf:
            opf_path, opf = EpubParser._read_container_and_opf(zf)
            opf_dir = posixpath.dirname(opf_path)
            title, author = EpubParser._opf_metadata(opf)

            manifest = {}
            for it in opf.iter('{*}item'):
                iid = it.get('id')
                href = it.get('href')
                if not iid or not href:
                    continue
                full = posixpath.normpath(
                    posixpath.join(opf_dir, EpubParser._norm(href)))
                manifest[iid] = (full, (it.get('media-type') or '').lower(),
                                 it.get('properties') or '')

            spine_ids, spine_ids_nolinear = [], []
            for ir in opf.iter('{*}itemref'):
                idref = ir.get('idref')
                if not idref:
                    continue
                if (ir.get('linear') or 'yes').strip().lower() == 'no':
                    spine_ids_nolinear.append(idref)
                else:
                    spine_ids.append(idref)
            spine_names = [manifest[i][0] for i in spine_ids + spine_ids_nolinear
                           if i in manifest]

            ncx_raw = None
            ncx_dir = ''
            nav_raw = None
            for iid, (full, mt, props) in manifest.items():
                if mt == 'application/x-dtbncx+xml' and ncx_raw is None:
                    ncx_raw = EpubParser._zip_read(zf, full)
                    # NCX 内 navPoint 的 src 相对于 NCX 文件所在目录（此前漏了
                    # 这一步，NCX 位于子目录时目录条目解析失败，退化为无标题
                    # 整文件章节）
                    ncx_dir = posixpath.dirname(full)
                if 'nav' in props.split() and nav_raw is None:
                    nav_raw = EpubParser._zip_read(zf, full)
            toc_ncx = [
                (EpubParser._norm(posixpath.normpath(
                    posixpath.join(ncx_dir, h.split('#')[0])))
                 + ('#' + h.split('#', 1)[1] if '#' in h else ''), t)
                for h, t in EpubParser._parse_ncx(ncx_raw)
            ] if ncx_raw else []
            toc_nav = EpubParser._parse_nav(nav_raw) if nav_raw else []
            nav_dir = ''
            if nav_raw is not None:
                for iid, (full, mt, props) in manifest.items():
                    if 'nav' in props.split():
                        nav_dir = posixpath.dirname(full)
                        break
            toc_nav = [(EpubParser._norm(posixpath.normpath(
                            posixpath.join(nav_dir, h.split('#')[0])))
                        + ('#' + h.split('#', 1)[1] if '#' in h else ''), t)
                       for h, t in toc_nav]
            toc_entries = toc_nav if len(toc_nav) > len(toc_ncx) else toc_ncx

            docs: Dict[str, bytes] = {}
            doc_meta: Dict[str, Tuple[str, str]] = {}
            images: Dict[str, bytes] = {}
            cover_path = None
            for iid, (full, mt, props) in manifest.items():
                if 'html' in mt or mt == 'application/x-dtbook+xml':
                    raw = EpubParser._zip_read(zf, full)
                    if raw is not None:
                        docs[full] = raw
                        doc_meta[full] = (mt, props)
                elif mt.startswith('image/'):
                    raw = EpubParser._zip_read(zf, full)
                    if raw is not None:
                        images[full] = raw
                        if 'cover-image' in props.split():
                            cover_path = full
            if cover_path is None:
                for name in images:
                    if 'cover' in posixpath.basename(name).lower():
                        cover_path = name
                        break

        chapters = EpubParser._assemble_chapters(docs, doc_meta, spine_names, toc_entries)
        return EpubBookInfo(title=title, author=author, cover_path=cover_path,
                            chapters=chapters, images=images)

    @staticmethod
    def _zip_read(zf: zipfile.ZipFile, name: str) -> Optional[bytes]:
        try:
            return zf.read(name)
        except KeyError:
            low = name.lower()
            for n in zf.namelist():
                if n.lower() == low:
                    try:
                        return zf.read(n)
                    except KeyError:
                        return None
        return None

    @staticmethod
    def _parse_via_ebooklib(file_path: str) -> EpubBookInfo:
        import ebooklib
        from ebooklib import epub

        book = None
        last_err = None
        for kwargs in ({}, {'options': {'ignore_ncx': True}}):
            try:
                book = epub.read_epub(file_path, **kwargs)
                break
            except TypeError:
                try:
                    book = epub.read_epub(file_path)
                    break
                except Exception as e:
                    last_err = e
            except Exception as e:
                last_err = e
        if book is None:
            raise last_err or Exception("ebooklib 无法打开文件")

        meta_title = book.get_metadata('DC', 'title')
        title = meta_title[0][0].strip() if meta_title and meta_title[0][0] else "未知标题"
        meta_creator = book.get_metadata('DC', 'creator')
        author = " / ".join(a[0].strip() for a in meta_creator if a and a[0]) or "未知作者"

        docs: Dict[str, bytes] = {}
        doc_meta: Dict[str, Tuple[str, str]] = {}
        images: Dict[str, bytes] = {}
        cover_path = None
        for item in book.get_items():
            name = EpubParser._norm(item.get_name())
            try:
                itype = item.get_type()
            except Exception:
                itype = None
            mt = (getattr(item, 'media_type', '') or '').lower()
            props_list = getattr(item, 'properties', None) or []
            props = ' '.join(props_list) if isinstance(props_list, (list, tuple)) else str(props_list)
            if itype == ebooklib.ITEM_DOCUMENT or 'html' in mt:
                try:
                    docs[name] = item.get_content()
                    doc_meta[name] = (mt, props)
                except Exception:
                    pass
            elif itype == ebooklib.ITEM_IMAGE or mt.startswith('image/'):
                try:
                    images[name] = item.get_content()
                    if 'cover' in posixpath.basename(name).lower() and not cover_path:
                        cover_path = name
                except Exception:
                    pass

        spine_names = []
        try:
            raw_spine = book.spine or []
        except Exception:
            raw_spine = []
        for entry in raw_spine:
            idref = entry[0] if isinstance(entry, (tuple, list)) else entry
            item = book.get_item_with_id(idref)
            if item is not None:
                spine_names.append(EpubParser._norm(item.get_name()))

        toc_entries: List[Tuple[str, str]] = []

        def flatten(toc_list):
            for entry in toc_list or []:
                try:
                    if isinstance(entry, tuple) and len(entry) == 2:
                        section, children = entry
                        href = getattr(section, 'href', None)
                        t = getattr(section, 'title', None) or "无标题"
                        if href:
                            toc_entries.append((href, t))
                        flatten(children)
                    elif hasattr(entry, 'href'):
                        if entry.href:
                            toc_entries.append(
                                (entry.href, getattr(entry, 'title', None) or "无标题"))
                except Exception:
                    continue

        try:
            flatten(book.toc)
        except Exception:
            pass

        chapters = EpubParser._assemble_chapters(docs, doc_meta, spine_names, toc_entries)
        return EpubBookInfo(title=title, author=author, cover_path=cover_path,
                            chapters=chapters, images=images)

# =============================================================================
# Qt WebEngine EPUB 阅读器
# =============================================================================
class EpubViewerWidget(QWebEngineView):
    """Qt WebEngine EPUB 阅读器。

    与主程序旧 ReaderWidget 保持核心属性/方法兼容：
    ``font_size / theme / custom_bg / custom_fg / custom_font_* /
    text_bold_enabled / images / current_chapter_file /
    set_chapter_content() / scroll_ratio() / set_scroll_ratio() / clear()``
    """

    chapterTurnRequested = Signal(int)  # 预留：触屏左右滑动翻章（-1 上一章 / +1 下一章）
    navigateToChapter = Signal(int, str)  # 正文/目录页链接跳转：章节索引, 锚点
    fontZoomRequested = Signal(int)  # v1.0.2：触屏捏合映射为字号调整（+1 放大 / -1 缩小）

    def __init__(self, parent=None):
        super().__init__(parent)

        self.font_size = 18
        self.line_height = 1.8
        self.theme = "mien"
        self.custom_bg: Optional[str] = None
        self.custom_fg: Optional[str] = None
        self.custom_font_western = "Palatino Linotype"
        self.custom_font_chinese = "华文仿宋"
        self.text_bold_enabled = False

        self.images: Dict[str, bytes] = {}
        self.current_chapter_file = ""
        self.chapters: List[EpubChapter] = []   # 本书章节表：链接 -> 章节索引映射
        self._pending_anchor = ""               # 待恢复的章内锚点（跨章跳转后滚动用）

        self._page = _EpubWebPage(self)
        self.setPage(self._page)
        self._page.linkActivated.connect(self._on_link_activated)

        settings = self.settings()
        # 必须启用 JavaScript：Qt 规定 JS 禁用时 runJavaScript 不执行，
        # 而章内滚动位置恢复与锚点跳转都依赖它。书内脚本在无网络访问、
        # 无本地文件访问（下方均已禁用）的隔离页面中运行，风险可控。
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.HyperlinkAuditingEnabled, False)
        try:
            settings.setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, False)
        except Exception:
            pass

        # v1.0.2：触屏捏合适配——接管 PinchGesture 映射为字号步进；同时尽量
        # 关闭 WebEngine 内置页面缩放，避免手势进行中弹跳、结束时乱跳
        # （PinchGesturesEnabled 为 Qt 6.5+ 属性，旧版本无此属性时忽略即可）。
        try:
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.PinchGesturesEnabled, False)
        except Exception:
            pass
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.grabGesture(Qt.GestureType.PinchGesture)
        self._pinch_active = False
        self._pinch_acc = 1.0

        self._pending_scroll_ratio: Optional[float] = None
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.setInterval(40)
        self._restore_timer.timeout.connect(self._restore_pending_scroll)

        self.loadFinished.connect(self._on_load_finished)
        self.update_style()

    # ------------------------------------------------------------------
    # 触屏捏合（v1.0.2）
    # ------------------------------------------------------------------
    def event(self, ev):
        """接管捏合手势：缩放不再交给 WebEngine 页面缩放（会乱跳），
        转为阅读字号步进请求，由主程序统一调整（与 TXT/PDF 行为一致）。"""
        if ev.type() == QEvent.Type.Gesture:
            gesture = ev.gesture(Qt.GestureType.PinchGesture)
            if gesture is not None:
                self._handle_pinch(gesture)
                ev.accept()
                return True
        return super().event(ev)

    def _handle_pinch(self, gesture) -> None:
        state = gesture.state()
        if state == Qt.GestureState.GestureStarted:
            self._pinch_active = True
            self._pinch_acc = 1.0
        elif state in (Qt.GestureState.GestureFinished,
                       Qt.GestureState.GestureCanceled):
            # 手势结束/取消时复位累计倍率，避免下一次手势继承残余倍率
            self._pinch_active = False
            self._pinch_acc = 1.0
        if state == Qt.GestureState.GestureCanceled:
            return
        self._pinch_acc *= gesture.scaleFactor()
        if self._pinch_acc >= 1.1:
            self._pinch_acc = 1.0
            self.fontZoomRequested.emit(1)
        elif self._pinch_acc <= 0.9:
            self._pinch_acc = 1.0
            self.fontZoomRequested.emit(-1)

    # ------------------------------------------------------------------
    # 主题 / 阅读偏好
    # ------------------------------------------------------------------
    def get_theme_colors(self):
        themes = {
            "light": {"bg": "#fafafa", "fg": "#333333", "link": "#0066cc"},
            "dark":  {"bg": "#1e1e1e", "fg": "#d4d4d4", "link": "#4da6ff"},
            "sepia": {"bg": "#f4ecd8", "fg": "#5b4636", "link": "#8b4513"},
            "mien":  {"bg": "#252526", "fg": "#cccccc", "link": "#a78bfa"},
        }
        t = themes.get(self.theme, themes["mien"])
        bg = self.custom_bg or t["bg"]
        fg = self.custom_fg or t["fg"]
        link = t["link"]
        return bg, fg, link

    def update_style(self):
        bg, _fg, _link = self.get_theme_colors()
        try:
            self.page().setBackgroundColor(QColor(bg))
        except Exception:
            pass

        settings = self.settings()
        try:
            settings.setFontFamily(
                QWebEngineSettings.FontFamily.StandardFont, self.custom_font_western)
            settings.setFontFamily(
                QWebEngineSettings.FontFamily.SerifFont, self.custom_font_western)
        except Exception:
            pass

        # 说明：正文样式由 set_chapter_content 按当前主题全量重建，
        # 无需对活动页面做增量刷新（Qt 侧仅更新页面底色与默认字体族）。

    # ------------------------------------------------------------------
    # 图片资源
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_resource_path(path: str) -> str:
        return unquote(path or "").replace("\\", "/").split("?")[0].split("#")[0]

    def _candidate_paths(self, path: str) -> List[str]:
        path = self._normalize_resource_path(path).replace("\\", "/").lstrip("/")
        candidates: List[str] = []
        if not path:
            return candidates

        candidates.append(path)
        if self.current_chapter_file:
            base_dir = posixpath.dirname(self.current_chapter_file.replace("\\", "/"))
            try:
                joined = posixpath.normpath(posixpath.join(base_dir, path)).replace("\\", "/")
                if joined and joined != ".":
                    candidates.append(joined.lstrip("/"))
            except Exception:
                pass

        for prefix in ("OEBPS", "OPS", "content", "Text", "text"):
            candidates.append(f"{prefix}/{path}")
            candidates.append(f"{prefix}{path if path.startswith('/') else '/' + path}")

        out: List[str] = []
        seen = set()
        for cand in candidates:
            cand = cand.replace("\\", "/").lstrip("/")
            if cand and cand not in seen:
                out.append(cand)
                seen.add(cand)
        return out

    def _find_image(self, path: str) -> Optional[bytes]:
        candidates = self._candidate_paths(path)
        for cand in candidates:
            if cand in self.images:
                return self.images[cand]

        lowered = [c.lower() for c in candidates]
        for name, data in self.images.items():
            if name.lower() in lowered or name.lower().lstrip("/") in lowered:
                return data

        target = posixpath.basename(candidates[0]).lower() if candidates else ""
        if target:
            for name, data in self.images.items():
                if posixpath.basename(name).lower() == target:
                    return data
        return None

    @staticmethod
    def _data_uri(path: str, data: bytes) -> str:
        ext = posixpath.basename(path).rsplit(".", 1)[-1].lower() if "." in posixpath.basename(path) else ""
        mime = mimetypes.types_map.get(f".{ext}") if ext else None
        if not mime:
            mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _preprocess_html_images(self, html_content: str) -> str:
        """把 EPUB 内相对图片引用转换为 data URI，供 Qt WebEngine 安全加载。"""
        if not self.images or not html_content:
            return html_content

        def replace_src(match):
            quote_char = match.group(1)
            src = match.group(2).strip()
            if (not src or src.startswith(("data:", "http://", "https://", "#"))
                    or src.startswith("{{")):
                return match.group(0)
            data = self._find_image(src)
            if data is None:
                return match.group(0)
            return f"src={quote_char}{self._data_uri(src, data)}{quote_char}"

        html_content = re.sub(
            r"\bsrc\s*=\s*(['\"])([^'\"]+)\1",
            replace_src,
            html_content,
            flags=re.IGNORECASE,
        )

        # 兼容 SVG <image href="...">。
        def replace_href(match):
            quote_char = match.group(1)
            src = match.group(2).strip()
            if src.startswith(("data:", "http://", "https://", "#")):
                return match.group(0)
            data = self._find_image(src)
            if data is None:
                return match.group(0)
            return f"href={quote_char}{self._data_uri(src, data)}{quote_char}"

        html_content = re.sub(
            r"\bhref\s*=\s*(['\"])([^'\"]+)\1",
            lambda m: replace_href(m) if "<image" in m.string[max(0, m.start()-20):m.start()].lower() else m.group(0),
            html_content,
            flags=re.IGNORECASE,
        )

        # 兼容 CSS url(...)。
        def replace_css_url(match):
            prefix, quote_char, src, suffix = match.groups()
            if src.startswith(("data:", "http://", "https://", "#")):
                return match.group(0)
            data = self._find_image(src)
            if data is None:
                return match.group(0)
            if quote_char:
                return f"{prefix}{quote_char}{self._data_uri(src, data)}{quote_char}{suffix}"
            return f"{prefix}{self._data_uri(src, data)}{suffix}"

        html_content = re.sub(
            r"(url\(\s*)(['\"]?)([^'\"]+)(\2\s*\))",
            replace_css_url,
            html_content,
            flags=re.IGNORECASE,
        )
        return html_content

    # ------------------------------------------------------------------
    # 章节装载 / 滚动位置
    # ------------------------------------------------------------------
    def set_chapter_content(self, html_content: str):
        self._pending_anchor = ""
        bg, fg, link = self.get_theme_colors()
        html_content = self._preprocess_html_images(html_content or "")

        is_light = self.theme in ("light", "sepia")
        blockquote_color = "#666666" if is_light else "#999999"
        code_bg = "#e8e8e8" if is_light else "#1e1e1e"
        base_weight = 600 if self.text_bold_enabled else 400
        bold_weight = 800 if self.text_bold_enabled else 700
        font_western = self.custom_font_western.replace('"', "")
        font_chinese = self.custom_font_chinese.replace('"', "")

        wrapped = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
:root {{
    --lyuer-bg: {bg};
    --lyuer-fg: {fg};
    --lyuer-link: {link};
}}
html, body {{
    margin: 0;
    padding: 0;
    background: var(--lyuer-bg);
    color: var(--lyuer-fg);
}}
body {{
    box-sizing: border-box;
    max-width: 56em;
    margin: 0 auto;
    padding: 2.5rem 3rem 4rem;
    font-family: "{font_western}", "{font_chinese}", serif;
    font-size: {self.font_size}px;
    line-height: {self.line_height};
    font-weight: {base_weight};
    overflow-wrap: break-word;
}}
h1 {{ font-size: {self.font_size * 1.4}px; line-height: 1.3; margin: 1.2em 0 0.8em; font-weight: {bold_weight}; }}
h2 {{ font-size: {self.font_size * 1.25}px; line-height: 1.35; margin: 1.2em 0 0.7em; font-weight: {bold_weight}; }}
h3 {{ font-size: {self.font_size * 1.15}px; line-height: 1.4; margin: 1.2em 0 0.6em; font-weight: {bold_weight}; }}
p {{ margin: 0.8em 0; text-align: justify; }}
img, svg, video {{ max-width: 100%; height: auto; border-radius: 4px; }}
a {{ color: var(--lyuer-link); text-decoration: none; }}
blockquote {{ border-left: 3px solid var(--lyuer-link); margin-left: 0; padding-left: 1em; color: {blockquote_color}; }}
code {{ background: {code_bg}; padding: 2px 6px; border-radius: 4px; font-family: Consolas, monospace; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
strong, b {{ font-weight: {bold_weight}; }}
table {{ border-collapse: collapse; max-width: 100%; }}
td, th {{ border: 1px solid rgba(128,128,128,0.25); padding: 0.35em 0.6em; }}
</style>
</head>
<body>
{html_content}
</body>
</html>"""
        # v1.0.1：base 追加当前章节文件路径——固定 base 会让子目录章节里的
        # 相对链接（目录页条目、交叉引用）丢失目录层级，解析到包外而匹配失败
        base = QUrl(EPUB_BASE_URL)
        if self.current_chapter_file:
            base = QUrl(EPUB_BASE_URL + self.current_chapter_file.replace("\\", "/"))
        self.setHtml(wrapped, base)

    def _on_load_finished(self, ok: bool):
        if not ok:
            return
        if self._pending_scroll_ratio is not None:
            self._restore_timer.start()

    def _restore_pending_scroll(self):
        if self._page_is_loading():
            # setHtml 为异步装载：脚本必须等 loadFinished 之后执行，
            # 期间定时器轮询重试（修正原先 isLoading 短暂为 False 导致
            # 脚本打在旧文档上、恢复静默失败的竞态）
            self._restore_timer.start()
            return
        anchor, ratio = self._pending_anchor, self._pending_scroll_ratio
        self._pending_anchor, self._pending_scroll_ratio = "", None
        if anchor:
            self._scroll_anchor_now(anchor)
        elif ratio is not None:
            self._scroll_ratio_now(ratio)

    def scroll_ratio(self) -> float:
        page = self.page()
        if page is None:
            return 0.0
        try:
            doc_h = float(page.contentsSize().height())
            view_h = float(self.height())
            max_scroll = max(0.0, doc_h - view_h)
            if max_scroll <= 0:
                return 0.0
            ratio = float(page.scrollPosition().y()) / max_scroll
            return max(0.0, min(1.0, ratio))
        except Exception:
            return 0.0

    def _page_is_loading(self) -> bool:
        try:
            page = self.page()
            return bool(page is not None and page.isLoading())
        except Exception:
            return False

    def set_chapters(self, chapters):
        """登记本书章节表，用于把正文/目录页链接映射为章节索引。"""
        self.chapters = list(chapters or [])

    # ------------------------------------------------------------------
    # 正文链接跳转（目录页 / 章节交叉引用）
    # ------------------------------------------------------------------
    def _on_link_activated(self, path: str, anchor: str):
        path = self._normalize_resource_path(path)
        anchor = unquote(anchor or "").strip()
        # v1.0.1 关键修复：链接按 EPUB_BASE_URL 解析后自带 "/lyuer-epub"
        # 前缀，此前未剥离，目录/交叉引用永远匹配不到章节（点击无反应）
        if path.lower().startswith(EPUB_BASE_PATH.lower()):
            path = path[len(EPUB_BASE_PATH):]
        path = path.lstrip("/")
        if not path:
            # 纯片段链接（如 <a href="#sec1">）：当前章节内滚动
            if anchor:
                self.scroll_to_anchor(anchor)
            return

        # 先按包内绝对路径匹配，再按相对当前章节文件解析
        candidates = [path]
        if self.current_chapter_file:
            base_dir = posixpath.dirname(self.current_chapter_file.replace("\\", "/"))
            try:
                joined = posixpath.normpath(posixpath.join(base_dir, path)).replace("\\", "/")
                if joined and joined != ".":
                    candidates.append(joined.lstrip("/"))
            except Exception:
                pass

        def _norm_name(s: str) -> str:
            return (s or "").replace("\\", "/").lstrip("/")

        def _file_match(ch_file: str, cand: str) -> bool:
            return ch_file == cand or ch_file.lower() == cand.lower()

        # v1.0.3 关键修复：此前「带锚点但锚点未精确匹配」时 fallback 永不赋值，
        # 点击被静默忽略（书内目录点击无效的根因之一）。现改为「文件匹配即记
        # fallback」，锚点比较增加大小写不敏感回退。
        target, fallback = -1, -1
        for i, ch in enumerate(self.chapters):
            ch_file = _norm_name(ch.file_name)
            if not any(_file_match(ch_file, cand) for cand in candidates):
                continue
            if fallback < 0:
                fallback = i
            if anchor and ((ch.anchor or "") == anchor
                           or (ch.anchor or "").lower() == anchor.lower()):
                target = i
                break
        if target < 0:
            target = fallback
        if target >= 0:
            self._pending_anchor = ""
            self.navigateToChapter.emit(target, anchor)
            return

        # v1.0.3：路径未命中任何章节时，再按「锚点命中当前章」原地滚动
        # （某些书的目录页把锚点写在路径里而章节文件按整文件切分）
        if anchor and self.chapters:
            cur = _norm_name(self.current_chapter_file)
            for ch in self.chapters:
                if (_norm_name(ch.file_name) == cur
                        and (ch.anchor or "").lower() == anchor.lower()):
                    self.scroll_to_anchor(anchor)
                    return
        # 找不到对应章节（链接指向书外资源等）：忽略，停留在当前章节

    def scroll_to_anchor(self, anchor: str):
        """滚动到当前文档内的指定锚点。"""
        anchor = (anchor or "").strip().lstrip("#")
        if not anchor:
            return
        self._pending_anchor = anchor
        self._restore_timer.start()

    def _scroll_anchor_now(self, anchor: str):
        script = (
            f"var el = document.getElementById({json.dumps(anchor)}) "
            f"|| document.getElementsByName({json.dumps(anchor)})[0];"
            "if (el) { el.scrollIntoView(); window.scrollBy(0, -12); }"
        )
        try:
            self.page().runJavaScript(script)
        except Exception:
            # v1.0.4：回填 pending 后必须重启恢复定时器，否则本次恢复被静默
            # 丢弃，锚点滚动/进度恢复再无重试机会
            self._pending_anchor = anchor
            self._restore_timer.start()

    # ------------------------------------------------------------------
    # 滚动位置（统一走 pending + 定时器，规避 setHtml 异步加载竞态）
    # ------------------------------------------------------------------
    def set_scroll_ratio(self, ratio: float):
        self._pending_scroll_ratio = max(0.0, min(1.0, float(ratio)))
        self._restore_timer.start()

    def _scroll_ratio_now(self, ratio: float):
        script = (
            "var maxScroll = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);"
            f"window.scrollTo(0, maxScroll * {ratio!r});"
        )
        try:
            self.page().runJavaScript(script)
        except Exception:
            # v1.0.4：同 _scroll_anchor_now——异常时重启定时器重试
            self._pending_scroll_ratio = ratio
            self._restore_timer.start()

    def set_book_images(self, images: Dict[str, bytes]):
        self.images = images or {}

    def clear(self):
        self._pending_scroll_ratio = None
        self._pending_anchor = ""
        self.setHtml("<html><body></body></html>", QUrl(EPUB_BASE_URL))

    def shutdown(self):
        """主程序切换书籍/退出时调用。"""
        self.clear()
