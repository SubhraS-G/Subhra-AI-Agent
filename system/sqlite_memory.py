"""SQLite-backed memory store.

Tier-1 finisher from ROADMAP.md. Same public interface as the file-based
`system.memory.MemoryStore`, plus:

- full-text search across all notes and preferences (FTS5 when available)
- cross-links between notes (note_links) and correction events (correction_log)
- imports existing memory/*.md notes so nothing is lost

The default database lives at memory/memory.db (gitignore it).
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3
import threading
from pathlib import Path

from system import paths
from system.logging_utils import log
from system.memory import CONFIDENCE_ORDER, PROMOTION_EVIDENCE, Preference, MemoryStore as FileMemoryStore

_MEMORY_CLASSES = ("permanent", "projects", "workflows", "temporary")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_class TEXT NOT NULL,
    name        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(memory_class, name)
);

CREATE TABLE IF NOT EXISTS preferences (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pref_id        TEXT NOT NULL UNIQUE,
    rule           TEXT NOT NULL,
    scope          TEXT NOT NULL DEFAULT 'general',
    confidence     TEXT NOT NULL DEFAULT 'low',
    evidence_count INTEGER NOT NULL DEFAULT 1,
    body           TEXT NOT NULL DEFAULT '',
    last_updated   TEXT NOT NULL,
    location       TEXT NOT NULL DEFAULT 'workflows'
);

CREATE TABLE IF NOT EXISTS correction_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pref_id     TEXT NOT NULL,
    evidence_no INTEGER NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_links (
    src_class TEXT NOT NULL,
    src_name  TEXT NOT NULL,
    dst_class TEXT NOT NULL,
    dst_name  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(src_class, src_name, dst_class, dst_name)
);
"""


class SQLiteMemoryStore(FileMemoryStore):
    """Drop-in replacement for MemoryStore backed by a single SQLite file."""

    def __init__(self, db_path: Path | None = None, root: Path | None = None) -> None:
        # Keep the file-store root attribute for compatibility with tests
        # and thumbnail output routing, but do not create markdown trees.
        self.root = root
        if db_path is None:
            if root is not None:
                db_path = root / "memory.db"
            else:
                paths.ensure_directories()
                db_path = paths.MEMORY_DIR / "memory.db"
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Shared across server threads (check_same_thread=False) with a lock
        # serializing mutations; reads rely on SQLite's serialized mode.
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.executescript(_SCHEMA)
        self._ensure_fts()
        self.conn.commit()

    # ------------------------------------------------------------- internals

    def _ensure_fts(self) -> None:
        try:
            self.conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
                USING fts5(name, memory_class, content, content='notes', content_rowid='id');
                CREATE TRIGGER IF NOT EXISTS notes_fts_insert AFTER INSERT ON notes BEGIN
                    INSERT INTO notes_fts(rowid, name, memory_class, content)
                    VALUES (new.id, new.name, new.memory_class, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS notes_fts_delete AFTER DELETE ON notes BEGIN
                    INSERT INTO notes_fts(notes_fts, rowid, name, memory_class, content)
                    VALUES ('delete', old.id, old.name, old.memory_class, old.content);
                END;
                """
            )
            self.fts_enabled = True
        except sqlite3.OperationalError:
            # Older SQLite without FTS5: fall back to LIKE search.
            self.fts_enabled = False

    @classmethod
    def from_file_store(cls, file_store: FileMemoryStore, db_path: Path | None = None) -> "SQLiteMemoryStore":
        """Import a file store's notes and preferences into a new DB store."""
        store = cls(db_path=db_path, root=file_store.root)
        migrated = 0
        for memory_class in _MEMORY_CLASSES:
            for path in file_store.list_notes(memory_class):
                content = path.read_text(encoding="utf-8")
                store.write_note(memory_class, path.stem, content)
                migrated += 1
        for pref in file_store.list_preferences():
            for _ in range(pref.evidence_count - 1):
                store.record_correction(pref.rule, scope=pref.scope, note="")
            store.record_correction(pref.rule, scope=pref.scope, note=pref.body)
            migrated += 1
        log("memory_migrated", notes_and_prefs=migrated)
        return store

    # ------------------------------------------------------------------ read

    def list_notes(self, memory_class: str):
        rows = self.conn.execute(
            "SELECT name FROM notes WHERE memory_class = ? ORDER BY name", (memory_class,)
        ).fetchall()
        return [row["name"] for row in rows]

    def read_note(self, memory_class: str, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT content FROM notes WHERE memory_class = ? AND name = ?",
            (memory_class, name),
        ).fetchone()
        return row["content"] if row else None

    # ----------------------------------------------------------------- write

    def write_note(self, memory_class: str, name: str, content: str) -> str:
        now = _dt.datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO notes (memory_class, name, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(memory_class, name)
                DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at
                """,
                (memory_class, name, content.rstrip() + "\n", now, now),
            )
            self.conn.commit()
        log("memory_write", memory_class=memory_class, note=name, store="sqlite")
        return name

    def delete_temporary(self, name: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM notes WHERE memory_class = 'temporary' AND name = ?", (name,)
            )
            self.conn.commit()
        deleted = cur.rowcount > 0
        if deleted:
            log("memory_delete", memory_class="temporary", note=name, store="sqlite")
        return deleted

    def note_path(self, memory_class: str, name: str):
        raise NotImplementedError("SQLiteMemoryStore keeps notes in the database, not files")

    # -------------------------------------------------------------- searching

    def search(self, query: str, memory_class: str | None = None, limit: int = 10) -> list[dict]:
        """Search across notes AND learned preferences (rules are memory too)."""
        results: list[dict] = []

        # Preferences first — they carry the highest behavioral weight.
        like = f"%{query}%"
        pref_rows = self.conn.execute(
            "SELECT pref_id, rule, scope, body, confidence FROM preferences "
            "WHERE rule LIKE ? OR body LIKE ? ORDER BY evidence_count DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        for r in pref_rows:
            results.append({
                "memory_class": f"preferences[{r['confidence']}]",
                "name": r["pref_id"],
                "excerpt": self._excerpt(r["rule"], query),
            })

        remaining = max(0, limit - len(results))
        if remaining:
            if self.fts_enabled:
                sql = (
                    "SELECT n.memory_class, n.name, n.content, "
                    "bm25(notes_fts) AS rank FROM notes_fts f "
                    "JOIN notes n ON n.id = f.rowid WHERE notes_fts MATCH ?"
                )
                params: list = [query]
                if memory_class:
                    sql += " AND n.memory_class = ?"
                    params.append(memory_class)
                sql += " ORDER BY rank LIMIT ?"
                params.append(remaining)
                try:
                    rows = self.conn.execute(sql, params).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            else:
                sql = "SELECT memory_class, name, content FROM notes WHERE content LIKE ?"
                params: list = [like]
                if memory_class:
                    sql += " AND memory_class = ?"
                    params.append(memory_class)
                sql += " ORDER BY updated_at DESC LIMIT ?"
                params.append(remaining)
                rows = self.conn.execute(sql, params).fetchall()
            results.extend({
                "memory_class": r["memory_class"],
                "name": r["name"],
                "excerpt": self._excerpt(r["content"], query),
            }
            for r in rows)
        return results

    @staticmethod
    def _excerpt(content: str, query: str, radius: int = 80) -> str:
        lowered = content.lower()
        for term in re.split(r"\s+", query.strip()):
            idx = lowered.find(term.lower())
            if idx != -1:
                start = max(0, idx - radius)
                end = min(len(content), idx + len(term) + radius)
                prefix = "..." if start > 0 else ""
                suffix = "..." if end < len(content) else ""
                return prefix + content[start:end].strip() + suffix
        return content[: radius * 2].strip()

    # ----------------------------------------------------------------- links

    def link_notes(self, src: tuple[str, str], dst: tuple[str, str]) -> bool:
        """Cross-link two notes, e.g. link_notes(("projects","valorant"), ("workflows","thumbnails"))."""
        now = _dt.datetime.now().isoformat(timespec="seconds")
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO note_links
                (src_class, src_name, dst_class, dst_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (src[0], src[1], dst[0], dst[1], now),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def links_for(self, memory_class: str, name: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT dst_class, dst_name FROM note_links
            WHERE src_class = ? AND src_name = ?
            UNION
            SELECT src_class, dst_name FROM (
                SELECT src_class, src_name AS dst_name FROM note_links
                WHERE dst_class = ? AND dst_name = ?
            )
            """,
            (memory_class, name, memory_class, name),
        ).fetchall()
        return [{"memory_class": r[0], "name": r[1]} for r in rows]

    # ------------------------------------------------------------ preferences

    def list_preferences(self, scope: str | None = None) -> list[Preference]:
        sql = "SELECT * FROM preferences"
        params: list = []
        if scope is not None:
            sql += " WHERE scope = ?"
            params.append(scope)
        sql += " ORDER BY pref_id"
        return [
            Preference(
                id=r["pref_id"],
                rule=r["rule"],
                scope=r["scope"],
                confidence=r["confidence"],
                evidence_count=r["evidence_count"],
                last_updated=r["last_updated"],
                body=r["body"],
            )
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def record_correction(
        self, rule: str, scope: str = "general", note: str = ""
    ) -> Preference:
        """Record one correction; same learning progression as the file store."""
        slug = re.sub(r"[^a-z0-9]+", "_", rule.lower()).strip("_")[:48]
        today = _dt.date.today().isoformat()
        row = self.conn.execute(
            "SELECT * FROM preferences WHERE pref_id = ?", (slug,)
        ).fetchone()

        if row is None:
            pref = Preference(
                id=slug, rule=rule, scope=scope, confidence="low",
                evidence_count=1, last_updated=today, body=note,
            )
            location = "workflows"
            self.conn.execute(
                """
                INSERT INTO preferences
                (pref_id, rule, scope, confidence, evidence_count, body, last_updated, location)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (pref.id, pref.rule, pref.scope, pref.confidence, pref.evidence_count, pref.body, pref.last_updated, location),
            )
            evidence_no = 1
        else:
            new_count = row["evidence_count"] + 1
            confidence = self._promote(row["confidence"], new_count)
            merged_body = (row["body"] + "\n\n" + note).strip() if note else row["body"]
            location = "permanent" if confidence == "established" else "workflows"
            self.conn.execute(
                """
                UPDATE preferences SET evidence_count = ?, confidence = ?,
                       body = ?, last_updated = ?, location = ? WHERE pref_id = ?
                """,
                (new_count, confidence, merged_body, today, location, slug),
            )
            pref = Preference(
                id=row["pref_id"], rule=row["rule"], scope=row["scope"],
                confidence=confidence, evidence_count=new_count,
                last_updated=today, body=merged_body,
            )
            evidence_no = new_count

        with self._lock:
            self.conn.execute(
                "INSERT INTO correction_log (pref_id, evidence_no, note, recorded_at) VALUES (?, ?, ?, ?)",
                (pref.id, evidence_no, note, _dt.datetime.now().isoformat(timespec="seconds")),
            )
            self.conn.commit()
        log("preference_recorded", id=pref.id, confidence=pref.confidence,
            evidence=pref.evidence_count, store="sqlite")
        return pref

    def correction_history(self, pref_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT evidence_no, note, recorded_at FROM correction_log WHERE pref_id = ? ORDER BY evidence_no",
            (pref_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- status

    def stats(self) -> dict:
        notes = self.conn.execute(
            "SELECT memory_class, COUNT(*) AS n FROM notes GROUP BY memory_class"
        ).fetchall()
        prefs = self.conn.execute(
            "SELECT confidence, COUNT(*) AS n FROM preferences GROUP BY confidence"
        ).fetchall()
        return {
            "notes": {r["memory_class"]: r["n"] for r in notes},
            "preferences": {r["confidence"]: r["n"] for r in prefs},
            "links": self.conn.execute("SELECT COUNT(*) FROM note_links").fetchone()[0],
            "corrections": self.conn.execute("SELECT COUNT(*) FROM correction_log").fetchone()[0],
            "fts_enabled": self.fts_enabled,
        }

    def close(self) -> None:
        self.conn.close()
