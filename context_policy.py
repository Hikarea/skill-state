"""Bounded context and explicitly requested evidence; no model or embedding calls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def byte_size(text: str) -> int:
    return len(text.encode("utf-8"))


def require_bound(text: str, limit: int, label: str) -> str:
    if byte_size(text) > limit:
        raise ValueError(f"{label} exceeds {limit} UTF-8 bytes; reduce it explicitly")
    return text


def merge_state(base: dict, patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise ValueError("state patch must be an object")
    result = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key] = merge_state(result.get(key) if isinstance(result.get(key), dict) else {}, value)
        else:
            result[key] = value
    return result


class EvidenceStore:
    """Local, content-addressed exact text. Search is literal, not semantic.

    Scope is the containing run/session directory. Text is data, never instructions.
    This archive grows on disk; only bounded read/search results enter model context.
    """

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "evidence.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, text TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, text: str) -> str:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO evidence VALUES (?, ?)", (key, text))
        return key

    def read(self, key: str, offset: int = 0, limit: int = 2000) -> dict:
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("invalid evidence id")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("offset must be nonnegative and limit must be 1..2000 characters")
        with self.connect() as db:
            row = db.execute("SELECT substr(text, ?, ?), length(text) FROM evidence WHERE id = ?",
                             (offset + 1, limit, key)).fetchone()
        if row is None:
            raise ValueError("unknown evidence id in this run/session")
        part, length = row
        return {"id": key, "offset": offset, "text": part, "total_chars": length,
                "next_offset": offset + len(part) if offset + len(part) < length else None}

    def search(self, query: str, limit: int = 5) -> list[dict]:
        if not isinstance(query, str) or not 1 <= len(query) <= 200 or type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("query must be 1..200 characters and limit 1..5")
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, substr(text, max(1, instr(text, ?) - 80), 240) "
                "FROM evidence WHERE instr(text, ?) > 0 ORDER BY rowid DESC LIMIT ?",
                (query, query, limit),
            ).fetchall()
        return [{"id": key, "excerpt": excerpt} for key, excerpt in rows]


def observation_for(text: str, *, limit: int, archive: EvidenceStore | None = None) -> str:
    if archive is None:
        return require_bound(text, limit, "observation")
    key = archive.put(text)
    # Always supply an address, even for small observations whose future relevance is unknown.
    prefix = compact({"evidence_id": key, "total_bytes": byte_size(text)}) + "\n"
    if byte_size(prefix + text) <= limit:
        return prefix + text
    suffix = "\n[Excerpt only. Use evidence_read for exact remaining text.]"
    available = limit - byte_size(prefix + suffix)
    if available < 0:
        raise ValueError("observation budget too small for evidence reference")
    excerpt = text.encode("utf-8")[:available].decode("utf-8", errors="ignore")
    return prefix + excerpt + suffix
