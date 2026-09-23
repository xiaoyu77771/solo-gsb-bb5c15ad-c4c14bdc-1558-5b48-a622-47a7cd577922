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

-- 术语审校：按语言区分的术语表（全局复用）
CREATE TABLE IF NOT EXISTS glossary_terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    language    TEXT NOT NULL DEFAULT 'any',
    canonical   TEXT NOT NULL,
    allowed     TEXT NOT NULL DEFAULT '',
    forbidden   TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 每次上传的原始 cue（断句引擎的修订 cue 存于 cues 表；术语审校作用于原文）
CREATE TABLE IF NOT EXISTS original_cues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    start_sec       REAL NOT NULL,
    end_sec         REAL NOT NULL,
    text            TEXT NOT NULL
);

-- 术语审校疑似项与逐项决策
CREATE TABLE IF NOT EXISTS term_findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    term_id         INTEGER REFERENCES glossary_terms(id) ON DELETE SET NULL,
    cue_seq         INTEGER NOT NULL,
    begin_offset    INTEGER NOT NULL,
    end_offset      INTEGER NOT NULL,
    canonical       TEXT NOT NULL,
    surface         TEXT NOT NULL,
    suggestion      TEXT NOT NULL,
    replacement     TEXT NOT NULL,
    issue_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT 'any',
    cross_cue       INTEGER NOT NULL DEFAULT 0,
    context_before  TEXT NOT NULL DEFAULT '',
    context_after   TEXT NOT NULL DEFAULT '',
    payload_json    TEXT NOT NULL DEFAULT '{}',
    decision        TEXT NOT NULL DEFAULT 'pending',
    decided_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_orig_cues_sub ON original_cues(submission_id);
CREATE INDEX IF NOT EXISTS idx_tf_submission ON term_findings(submission_id);
CREATE INDEX IF NOT EXISTS idx_tf_decision ON term_findings(submission_id, decision);
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
            """SELECT s.id, s.filename, s.format, s.max_chars, s.min_dwell,
                      s.max_cps, s.issue_count, s.stats_json, s.created_at,
                      SUM(CASE WHEN f.decision = 'pending' THEN 1 ELSE 0 END)
                          AS term_pending,
                      SUM(CASE WHEN f.decision = 'accepted' THEN 1 ELSE 0 END)
                          AS term_accepted,
                      SUM(CASE WHEN f.decision = 'rejected' THEN 1 ELSE 0 END)
                          AS term_rejected,
                      COUNT(f.id) AS term_total
               FROM submissions s
               LEFT JOIN term_findings f ON f.submission_id = s.id
               GROUP BY s.id
               ORDER BY s.id DESC LIMIT ?""",
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


# --- 原始 cue（术语审校作用于上传原文，时间轴保持不变） ---

def save_original_cues(submission_id: int, cues) -> None:
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO original_cues
               (submission_id, seq, start_sec, end_sec, text)
               VALUES (?, ?, ?, ?, ?)""",
            [(submission_id, i, c.start, c.end, c.text)
             for i, c in enumerate(cues)],
        )


def get_original_cues(submission_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT seq, start_sec, end_sec, text
               FROM original_cues WHERE submission_id = ? ORDER BY seq""",
            (submission_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- 术语表 CRUD ---

def _term_row_to_dict(r) -> dict:
    d = dict(r)
    d["allowed_list"] = [x for x in (d.pop("allowed") or "").split("\n") if x]
    d["forbidden_list"] = [
        x for x in (d.pop("forbidden") or "").split("\n") if x]
    return d


def list_glossary_terms(language: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if language:
            rows = conn.execute(
                "SELECT * FROM glossary_terms WHERE language = ? "
                "ORDER BY id", (language,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM glossary_terms ORDER BY language, id").fetchall()
    return [_term_row_to_dict(r) for r in rows]


def get_glossary_term(term_id: int) -> dict | None:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM glossary_terms WHERE id = ?",
                         (term_id,)).fetchone()
    return _term_row_to_dict(r) if r else None


def create_glossary_term(language: str, canonical: str,
                         allowed: str, forbidden: str, note: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO glossary_terms
               (language, canonical, allowed, forbidden, note)
               VALUES (?, ?, ?, ?, ?)""",
            (language, canonical, allowed, forbidden, note),
        )
        return cur.lastrowid


def update_glossary_term(term_id: int, language: str, canonical: str,
                         allowed: str, forbidden: str, note: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE glossary_terms
               SET language = ?, canonical = ?, allowed = ?,
                   forbidden = ?, note = ?
               WHERE id = ?""",
            (language, canonical, allowed, forbidden, note, term_id),
        )
        return cur.rowcount > 0


def delete_glossary_term(term_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM glossary_terms WHERE id = ?",
                           (term_id,))
        return cur.rowcount > 0


def bulk_replace_glossary_terms(items: list[dict],
                                replace_all: bool = False) -> int:
    """批量导入术语（按 语言+规范词 去重 upsert），返回写入条数。"""
    with get_conn() as conn:
        if replace_all:
            conn.execute("DELETE FROM glossary_terms")
        n = 0
        for it in items:
            existing = conn.execute(
                "SELECT id FROM glossary_terms WHERE language = ? "
                "AND canonical = ?",
                (it["language"], it["canonical"]),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE glossary_terms
                       SET allowed = ?, forbidden = ?, note = ?
                       WHERE id = ?""",
                    (it["allowed"], it["forbidden"], it["note"],
                     existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO glossary_terms
                       (language, canonical, allowed, forbidden, note)
                       VALUES (?, ?, ?, ?, ?)""",
                    (it["language"], it["canonical"], it["allowed"],
                     it["forbidden"], it["note"]),
                )
            n += 1
    return n


# --- 术语审校发现与决策 ---

def save_term_findings(submission_id: int, findings: list[dict]) -> list[int]:
    """按顺序持久化疑似项，返回各自的行 id。"""
    ids: list[int] = []
    with get_conn() as conn:
        for f in findings:
            cur = conn.execute(
                """INSERT INTO term_findings
                   (submission_id, term_id, cue_seq, begin_offset, end_offset,
                    canonical, surface, suggestion, replacement, issue_type,
                    severity, language, cross_cue, context_before,
                    context_after, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (submission_id, f.get("term_id"), f["cue_seq"],
                 f["begin_offset"], f["end_offset"], f["canonical"],
                 f["surface"], f["suggestion"], f["replacement"],
                 f["issue_type"], f["severity"], f["language"],
                 int(f.get("cross_cue", False)),
                 f.get("context_before", ""), f.get("context_after", ""),
                 json.dumps(f, ensure_ascii=False)),
            )
            ids.append(cur.lastrowid)
    return ids


def _finding_row_to_dict(r) -> dict:
    d = json.loads(r["payload_json"])
    d["id"] = r["id"]
    d["submission_id"] = r["submission_id"]
    d["term_id"] = r["term_id"]
    d["decision"] = r["decision"]
    d["replacement"] = r["replacement"]
    d["issue_type_label"] = d.get("issue_type_label") or r["issue_type"]
    return d


def get_term_findings(submission_id: int,
                      decision: str | None = None,
                      language: str | None = None) -> list[dict]:
    sql = ("SELECT * FROM term_findings WHERE submission_id = ?")
    args: list = [submission_id]
    if decision:
        sql += " AND decision = ?"
        args.append(decision)
    if language:
        sql += " AND language = ?"
        args.append(language)
    sql += " ORDER BY canonical, cue_seq, begin_offset, id"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_finding_row_to_dict(r) for r in rows]


def get_term_finding(finding_id: int) -> dict | None:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM term_findings WHERE id = ?",
                         (finding_id,)).fetchone()
    return _finding_row_to_dict(r) if r else None


def set_finding_decision(finding_id: int, decision: str,
                         replacement: str | None = None) -> bool:
    if decision not in ("pending", "accepted", "rejected"):
        raise ValueError("decision 必须是 pending / accepted / rejected")
    with get_conn() as conn:
        if replacement is None:
            cur = conn.execute(
                "UPDATE term_findings SET decision = ?, "
                "decided_at = datetime('now') WHERE id = ?",
                (decision, finding_id),
            )
        else:
            cur = conn.execute(
                "UPDATE term_findings SET decision = ?, replacement = ?, "
                "decided_at = datetime('now') WHERE id = ?",
                (decision, replacement, finding_id),
            )
        return cur.rowcount > 0


def bulk_set_decisions(submission_id: int, decision: str,
                       finding_ids: list[int] | None = None) -> int:
    """批量设置决策；仅作用于 pending 项。finding_ids 为空时作用于该任务
    全部 pending 项。"""
    with get_conn() as conn:
        sql = ("UPDATE term_findings SET decision = ?, "
               "decided_at = datetime('now') WHERE submission_id = ? "
               "AND decision = 'pending'")
        args: list = [decision, submission_id]
        if finding_ids:
            placeholders = ",".join("?" for _ in finding_ids)
            sql += f" AND id IN ({placeholders})"
            args.extend(finding_ids)
        cur = conn.execute(sql, args)
        return cur.rowcount


def get_decision_counts(submission_id: int) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT decision, COUNT(*) AS n FROM term_findings "
            "WHERE submission_id = ? GROUP BY decision",
            (submission_id,),
        ).fetchall()
    counts = {"pending": 0, "accepted": 0, "rejected": 0}
    for r in rows:
        counts[r["decision"]] = r["n"]
    return counts
