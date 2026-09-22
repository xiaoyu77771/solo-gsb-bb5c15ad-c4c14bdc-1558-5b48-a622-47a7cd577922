"""SQLite 持久化层：启动时初始化，保存每次校验任务及修订结果。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/subtitles.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,
    format        TEXT NOT NULL DEFAULT 'vtt',
    max_chars     INTEGER NOT NULL,
    min_dwell     REAL NOT NULL,
    max_cps       REAL NOT NULL,
    issue_count   INTEGER NOT NULL DEFAULT 0,
    stats_json    TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    start_sec       REAL NOT NULL,
    end_sec         REAL NOT NULL,
    text            TEXT NOT NULL,
    source_indices  TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    cue_seq         INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT 'unknown',
    reason          TEXT NOT NULL,
    segment         TEXT NOT NULL,
    start_sec       REAL NOT NULL,
    end_sec         REAL NOT NULL,
    redistributable INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS artifacts (
    submission_id INTEGER PRIMARY KEY
        REFERENCES submissions(id) ON DELETE CASCADE,
    revised_vtt TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cues_submission ON cues(submission_id);
CREATE INDEX IF NOT EXISTS idx_issues_submission ON issues(submission_id);
CREATE INDEX IF NOT EXISTS idx_issues_language ON issues(submission_id, language);
"""


def init_db(path: str | None = None) -> None:
    target = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with get_conn(target) as conn:
        conn.executescript(_SCHEMA)


@contextmanager
def get_conn(path: str | None = None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_submission(filename: str, fmt: str, max_chars: int, min_dwell: float,
                    max_cps: float, result, revised_vtt: str) -> int:
    """持久化一次分析结果，返回 submission id。"""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO submissions
               (filename, format, max_chars, min_dwell, max_cps,
                issue_count, stats_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (filename, fmt, max_chars, min_dwell, max_cps,
             len(result.issues), json.dumps(result.stats, ensure_ascii=False)),
        )
        sid = cur.lastrowid

        conn.executemany(
            """INSERT INTO cues
               (submission_id, seq, start_sec, end_sec, text, source_indices)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(sid, i, c.start, c.end, c.text,
              json.dumps(spans, ensure_ascii=False))
             for i, (c, spans) in enumerate(zip(result.cues, result.spans))],
        )
        conn.executemany(
            """INSERT INTO issues
               (submission_id, cue_seq, kind, severity, language, reason,
                segment, start_sec, end_sec, redistributable)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(sid, iss.index, iss.kind, iss.severity, iss.language, iss.reason,
              iss.segment, iss.start, iss.end, int(iss.redistributable))
             for iss in result.issues],
        )
        conn.execute(
            "INSERT INTO artifacts (submission_id, revised_vtt) VALUES (?, ?)",
            (sid, revised_vtt),
        )
    return sid


def list_submissions(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, filename, format, max_chars, min_dwell, max_cps,
                      issue_count, stats_json, created_at
               FROM submissions ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_artifact(submission_id: int) -> dict | None:
    with get_conn() as conn:
        sub = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if sub is None:
            return None
        art = conn.execute(
            "SELECT revised_vtt FROM artifacts WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        cues = conn.execute(
            """SELECT seq, start_sec, end_sec, text, source_indices
               FROM cues WHERE submission_id = ? ORDER BY seq""",
            (submission_id,),
        ).fetchall()
        issues = conn.execute(
            """SELECT cue_seq, kind, severity, language, reason, segment,
                      start_sec, end_sec, redistributable
               FROM issues WHERE submission_id = ? ORDER BY cue_seq""",
            (submission_id,),
        ).fetchall()
    return {
        "submission": dict(sub),
        "revised_vtt": art["revised_vtt"] if art else "",
        "cues": [dict(r) for r in cues],
        "issues": [dict(r) for r in issues],
    }
