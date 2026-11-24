#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lyuer Database Module v1.4.1
版权所有 (c) MienArch | 面包图书馆 | 2026
许可证：GNU GPL v3.0 or later
SQLite persistent library storage for MienArch Lyuer.

v1.4.1：search_books 转义 LIKE 通配符（%/_）——此前用户搜索含 % 或 _
的文本会被当作通配符（如 "100%" 匹配一切含 "100" 的记录），现按字面
含义匹配（ESCAPE '\'）。



本模块保持 Qt-free；UI 层负责解析/渲染/对话框，本层只负责持久化。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DB_FILENAME = "lyuer_database.sqlite3"
SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_path(path: str) -> str:
    """Return a stable absolute path key for the current platform."""
    if not path:
        return ""
    p = os.path.abspath(os.path.normpath(path))
    try:
        p = os.path.normcase(p)
    except Exception:
        pass
    return p


@dataclass
class BookRecord:
    id: int
    path: str
    title: str
    author: str
    format: str
    size: int = 0
    mtime: float = 0.0
    cover_path: str = ""
    rating: int = 0
    series: str = ""
    publisher: str = ""
    identifiers: Dict[str, Any] = None  # type: ignore[assignment]
    tags: List[str] = None            # type: ignore[assignment]
    meta: Dict[str, Any] = None       # type: ignore[assignment]
    added_at: str = ""
    updated_at: str = ""
    last_opened: str = ""
    reading_chapter: int = 0
    reading_scroll: float = 0.0
    missing: bool = False
    cover: bytes = b""                # v1.4.0：封面图字节（PNG/JPEG）
    comments: str = ""                # v1.4.0：简介/备注（纯文本）

    def __post_init__(self) -> None:
        if self.identifiers is None:
            self.identifiers = {}
        if self.tags is None:
            self.tags = []
        if self.meta is None:
            self.meta = {}


class LibraryDatabase:
    """Small SQLite library store.

    Default file layout is caller-controlled. The desktop shell normally passes:
        <script_dir>/lyuer_database/lyuer_database.sqlite3
    but the user may select another directory; the filename stays fixed so the
    main program can migrate by copying the database file if needed.
    """

    def __init__(self, database_dir: str | os.PathLike[str] | None = None):
        if database_dir is None:
            database_dir = Path.cwd() / "lyuer_database"
        self.database_dir = Path(database_dir).expanduser().resolve()
        self.database_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.database_dir / DB_FILENAME
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA user_version")
        version = int(cur.fetchone()[0])
        if version < 1:
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    format TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL DEFAULT 0,
                    mtime REAL NOT NULL DEFAULT 0,
                    cover_path TEXT NOT NULL DEFAULT '',
                    rating INTEGER NOT NULL DEFAULT 0,
                    series TEXT NOT NULL DEFAULT '',
                    publisher TEXT NOT NULL DEFAULT '',
                    identifiers TEXT NOT NULL DEFAULT '{}',
                    tags TEXT NOT NULL DEFAULT '[]',
                    meta TEXT NOT NULL DEFAULT '{}',
                    added_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    last_opened TEXT NOT NULL DEFAULT '',
                    reading_chapter INTEGER NOT NULL DEFAULT 0,
                    reading_scroll REAL NOT NULL DEFAULT 0,
                    missing INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_books_title ON books(title);
                CREATE INDEX IF NOT EXISTS idx_books_author ON books(author);
                CREATE INDEX IF NOT EXISTS idx_books_series ON books(series);
                CREATE INDEX IF NOT EXISTS idx_books_format ON books(format);
                CREATE INDEX IF NOT EXISTS idx_books_missing ON books(missing);
                """
            )
        # v1.4.0：版本 1 → 2，新增 cover / comments 列（无损迁移）
        if version < 2:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(books)")}
            if "cover" not in cols:
                self.conn.execute("ALTER TABLE books ADD COLUMN cover BLOB")
            if "comments" not in cols:
                self.conn.execute(
                    "ALTER TABLE books ADD COLUMN comments TEXT NOT NULL DEFAULT ''")
        cur.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "LibraryDatabase":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # rows
    # ------------------------------------------------------------------
    @staticmethod
    def _loads_json(value: str, fallback: Any) -> Any:
        try:
            return json.loads(value) if value else fallback
        except Exception:
            return fallback

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> BookRecord:
        return BookRecord(
            id=int(row["id"]),
            path=row["path"],
            title=row["title"] or "",
            author=row["author"] or "",
            format=row["format"] or "",
            size=int(row["size"] or 0),
            mtime=float(row["mtime"] or 0),
            cover_path=row["cover_path"] or "",
            rating=int(row["rating"] or 0),
            series=row["series"] or "",
            publisher=row["publisher"] or "",
            identifiers=LibraryDatabase._loads_json(row["identifiers"], {}),
            tags=LibraryDatabase._loads_json(row["tags"], []),
            meta=LibraryDatabase._loads_json(row["meta"], {}),
            added_at=row["added_at"] or "",
            updated_at=row["updated_at"] or "",
            last_opened=row["last_opened"] or "",
            reading_chapter=int(row["reading_chapter"] or 0),
            reading_scroll=float(row["reading_scroll"] or 0),
            missing=bool(row["missing"]),
            cover=bytes(row["cover"]) if row["cover"] else b"",
            comments=row["comments"] or "",
        )

    @staticmethod
    def _encode_extra(extra: Optional[Dict[str, Any]]) -> Dict[str, str]:
        extra = extra or {}
        encoded: Dict[str, str] = {}
        for key in ("cover_path", "series", "publisher"):
            if key in extra and extra[key] is not None:
                encoded[key] = str(extra[key])
        if "rating" in extra and extra["rating"] is not None:
            try:
                encoded["rating"] = str(max(0, min(10, int(extra["rating"]))))
            except Exception:
                pass
        if "comments" in extra and extra["comments"] is not None:
            encoded["comments"] = str(extra["comments"])
        for key in ("identifiers", "tags", "meta"):
            if key in extra and extra[key] is not None:
                encoded[key] = json.dumps(extra[key], ensure_ascii=False, sort_keys=True)
        return encoded

    # ------------------------------------------------------------------
    # book CRUD
    # ------------------------------------------------------------------
    def upsert_book(
        self,
        path: str,
        title: str,
        author: str,
        fmt: str = "",
        size: int | None = None,
        mtime: float | None = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> BookRecord:
        key = normalize_path(path)
        now = utc_now()
        try:
            st = os.stat(key)
            if size is None:
                size = int(st.st_size)
            if mtime is None:
                mtime = float(st.st_mtime)
        except OSError:
            size = int(size or 0)
            mtime = float(mtime or 0)

        data = self._encode_extra(extra)
        fields = [
            "path", "title", "author", "format", "size", "mtime",
            "added_at", "updated_at", "missing",
        ]
        values: List[Any] = [key, title or "", author or "", (fmt or "").lower(), int(size or 0), float(mtime or 0), now, now, 0]
        for f, v in data.items():
            fields.append(f)
            values.append(v)

        existing = self.get_book_by_path(key)
        if existing is None:
            placeholders = ",".join("?" for _ in fields)
            sql = f"INSERT INTO books({','.join(fields)}) VALUES({placeholders})"
            cur = self.conn.execute(sql, values)
            book_id = int(cur.lastrowid)
        else:
            assignments = ",".join(f"{f}=?" for f in fields if f not in ("path", "added_at"))
            update_values = [v for f, v in zip(fields, values) if f not in ("path", "added_at")]
            sql = f"UPDATE books SET {assignments} WHERE id=?"
            self.conn.execute(sql, update_values + [existing.id])
            book_id = existing.id
        self.conn.commit()
        return self.get_book_by_id(book_id)  # type: ignore[return-value]

    def get_book_by_id(self, book_id: int) -> Optional[BookRecord]:
        row = self.conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def get_book_by_path(self, path: str) -> Optional[BookRecord]:
        row = self.conn.execute("SELECT * FROM books WHERE path=?", (normalize_path(path),)).fetchone()
        return self._row_to_record(row) if row else None

    def all_books(self, include_missing: bool = True) -> List[BookRecord]:
        sql = "SELECT * FROM books"
        if not include_missing:
            sql += " WHERE missing=0"
        sql += " ORDER BY lower(title), lower(author), path"
        return [self._row_to_record(r) for r in self.conn.execute(sql)]

    def search_books(self, query: str, include_missing: bool = True) -> List[BookRecord]:
        # v1.4.1：转义 LIKE 通配符，保证 % / _ 按字面匹配
        like = (query.strip()
                .replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
        q = f"%{like}%"
        sql = (
            "SELECT * FROM books WHERE "
            "(title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\' "
            "OR series LIKE ? ESCAPE '\\' OR publisher LIKE ? ESCAPE '\\' "
            "OR path LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' "
            "OR identifiers LIKE ? ESCAPE '\\')"
        )
        params: List[Any] = [q, q, q, q, q, q, q]
        if not include_missing:
            sql += " AND missing=0"
        sql += " ORDER BY lower(title), lower(author), path"
        return [self._row_to_record(r) for r in self.conn.execute(sql, params)]

    def delete_book(self, path_or_id: str | int) -> bool:
        if isinstance(path_or_id, int):
            cur = self.conn.execute("DELETE FROM books WHERE id=?", (path_or_id,))
        else:
            cur = self.conn.execute("DELETE FROM books WHERE path=?", (normalize_path(path_or_id),))
        self.conn.commit()
        return cur.rowcount > 0

    def mark_missing(self, path: str, missing: bool = True) -> None:
        self.conn.execute(
            "UPDATE books SET missing=?, updated_at=? WHERE path=?",
            (1 if missing else 0, utc_now(), normalize_path(path)),
        )
        self.conn.commit()

    def set_reading_position(self, path: str, chapter: int, scroll: float) -> None:
        self.conn.execute(
            "UPDATE books SET reading_chapter=?, reading_scroll=?, updated_at=? WHERE path=?",
            (max(0, int(chapter)), max(0.0, min(1.0, float(scroll))), utc_now(), normalize_path(path)),
        )
        self.conn.commit()

    def touch_opened(self, path: str) -> None:
        self.conn.execute(
            "UPDATE books SET last_opened=?, missing=0 WHERE path=?",
            (utc_now(), normalize_path(path)),
        )
        self.conn.commit()

    def set_rating(self, path: str, rating: int) -> None:
        self.conn.execute(
            "UPDATE books SET rating=?, updated_at=? WHERE path=?",
            (max(0, min(10, int(rating))), utc_now(), normalize_path(path)),
        )
        self.conn.commit()

    def set_tags(self, path: str, tags: Iterable[str]) -> None:
        clean = sorted({str(t).strip() for t in tags if str(t).strip()})
        self.conn.execute(
            "UPDATE books SET tags=?, updated_at=? WHERE path=?",
            (json.dumps(clean, ensure_ascii=False), utc_now(), normalize_path(path)),
        )
        self.conn.commit()

    def set_series(self, path: str, series: str, series_index: Any = None) -> None:
        meta_update: Dict[str, Any] = {}
        if series_index is not None:
            try:
                meta_update["series_index"] = float(series_index)
            except Exception:
                pass
        current = self.get_book_by_path(path)
        meta = dict(current.meta) if current else {}
        meta.update(meta_update)
        self.conn.execute(
            "UPDATE books SET series=?, meta=?, updated_at=? WHERE path=?",
            (series or "", json.dumps(meta, ensure_ascii=False, sort_keys=True), utc_now(), normalize_path(path)),
        )
        self.conn.commit()

    def set_identifiers(self, path: str, identifiers: Dict[str, Any]) -> None:
        clean = {str(k).strip(): str(v).strip() for k, v in (identifiers or {}).items() if str(k).strip() and str(v).strip()}
        self.conn.execute(
            "UPDATE books SET identifiers=?, updated_at=? WHERE path=?",
            (json.dumps(clean, ensure_ascii=False, sort_keys=True), utc_now(), normalize_path(path)),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # v1.4.0：封面 / 简介 / 元数据批量更新（供 lyuer_manager）
    # ------------------------------------------------------------------
    def set_cover(self, path: str, data: Optional[bytes]) -> None:
        """设置封面图字节（PNG/JPEG）。传 None 或空字节清除封面。"""
        self.conn.execute(
            "UPDATE books SET cover=?, updated_at=? WHERE path=?",
            (sqlite3.Binary(data) if data else None, utc_now(), normalize_path(path)),
        )
        self.conn.commit()

    def set_comments(self, path: str, comments: str) -> None:
        self.conn.execute(
            "UPDATE books SET comments=?, updated_at=? WHERE path=?",
            (comments or "", utc_now(), normalize_path(path)),
        )
        self.conn.commit()

    def update_metadata(self, path: str, **fields: Any) -> None:
        """calibre 式元数据更新：仅更新提供的字段。

        允许字段：title/author/series/publisher/rating/tags/identifiers/
        comments/meta。meta 为 dict，将与现有 meta 合并（保留 reading
        进度等内部键）。
        """
        key = normalize_path(path)
        allowed = ("title", "author", "series", "publisher", "comments")
        sets: List[str] = []
        vals: List[Any] = []
        for f in allowed:
            if f in fields and fields[f] is not None:
                sets.append(f"{f}=?")
                vals.append(str(fields[f]))
        if "rating" in fields and fields["rating"] is not None:
            try:
                sets.append("rating=?")
                vals.append(max(0, min(10, int(fields["rating"]))))
            except Exception:
                pass
        if "tags" in fields and fields["tags"] is not None:
            tags = fields["tags"]
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.replace("；", ";").split(";") if t.strip()]
            clean = sorted({str(t).strip() for t in tags if str(t).strip()})
            sets.append("tags=?")
            vals.append(json.dumps(clean, ensure_ascii=False))
        if "identifiers" in fields and fields["identifiers"] is not None:
            ident = fields["identifiers"]
            clean = {str(k).strip(): str(v).strip() for k, v in ident.items() if str(k).strip() and str(v).strip()}
            sets.append("identifiers=?")
            vals.append(json.dumps(clean, ensure_ascii=False, sort_keys=True))
        if "meta" in fields and isinstance(fields["meta"], dict):
            current = self.get_book_by_path(key)
            meta = dict(current.meta) if current else {}
            meta.update(fields["meta"])
            sets.append("meta=?")
            vals.append(json.dumps(meta, ensure_ascii=False, sort_keys=True))
        if not sets:
            return
        sets.append("updated_at=?")
        vals.append(utc_now())
        vals.append(key)
        self.conn.execute(f"UPDATE books SET {','.join(sets)} WHERE path=?", vals)
        self.conn.commit()

    def stats(self) -> Dict[str, int]:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM books").fetchone()
        missing = self.conn.execute("SELECT COUNT(*) AS c FROM books WHERE missing=1").fetchone()
        return {"books": int(row["c"]), "missing": int(missing["c"])}
