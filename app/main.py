"""字幕断句校验台 — FastAPI 入口。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from .segmenter import analyze
from .subtitles import (
    LANGUAGE_NAMES, Cue, parse_subtitle, write_vtt,
)
from .terminology import (
    TYPE_LABELS, TYPE_SEVERITY, apply_replacements, make_term, review_terms,
)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="字幕断句校验台", version="1.0.0", lifespan=lifespan)


def _issue_dict(iss) -> dict:
    return {
        "cue_seq": iss.index,
        "kind": iss.kind,
        "severity": iss.severity,
        "language": iss.language,
        "language_name": LANGUAGE_NAMES.get(iss.language, iss.language),
        "reason": iss.reason,
        "segment": iss.segment,
        "start": round(iss.start, 3),
        "end": round(iss.end, 3),
        "redistributable": iss.redistributable,
    }


def _result_payload(sid: int, filename: str, fmt: str, params: dict,
                    result) -> dict:
    return {
        "id": sid,
        "filename": filename,
        "format": fmt,
        "params": params,
        "stats": result.stats,
        "cues": [
            {
                "seq": i,
                "start": round(c.start, 3),
                "end": round(c.end, 3),
                "duration": round(c.duration, 3),
                "length": len(c.text),
                "cps": round(len(c.text) / c.duration, 1)
                if c.duration > 0 else None,
                "text": c.text,
                "sources": [s + 1 for s in spans],
                "redistributed": len(spans) > 1,
            }
            for i, (c, spans) in enumerate(zip(result.cues, result.spans))
        ],
        "issues": [_issue_dict(i) for i in result.issues],
        "language_names": LANGUAGE_NAMES,
    }


# --- 术语审校辅助 ---

def _load_glossary() -> list:
    return [
        make_term(row["language"], row["canonical"],
                  row["allowed_list"], row["forbidden_list"],
                  row.get("note") or "", term_id=row["id"])
        for row in db.list_glossary_terms()
    ]


def _term_row_payload(row: dict) -> dict:
    return {
        "id": row["id"],
        "language": row["language"],
        "canonical": row["canonical"],
        "allowed": row["allowed_list"],
        "forbidden": row["forbidden_list"],
        "note": row.get("note") or "",
        "created_at": row.get("created_at"),
    }


def _term_review_payload(sid: int, findings: list[dict],
                         counts: dict | None = None) -> dict:
    counts = counts or db.get_decision_counts(sid)
    by_type: dict[str, int] = {}
    for f in findings:
        by_type[f["issue_type"]] = by_type.get(f["issue_type"], 0) + 1
    return {
        "submission_id": sid,
        "findings": findings,
        "counts": counts,
        "type_labels": TYPE_LABELS,
        "type_severity": TYPE_SEVERITY,
    }


def _original_cues_for(sid: int) -> list[Cue]:
    return [Cue(start=r["start_sec"], end=r["end_sec"], text=r["text"])
            for r in db.get_original_cues(sid)]


@app.post("/api/analyze")
async def api_analyze(
    file: UploadFile = File(...),
    max_chars: int = Form(18),
    min_dwell: float = Form(0.8),
    max_cps: float = Form(12.0),
):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = raw.decode("latin-1")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400,
                                detail="无法识别文件编码，请使用 UTF-8")

    name = file.filename or "subtitles"
    fmt = "srt" if name.lower().endswith(".srt") else "vtt"

    try:
        cues = parse_subtitle(content)
        result = analyze(cues, max_chars=max_chars, min_dwell=min_dwell,
                         max_cps=max_cps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    notes = {}
    flagged = {iss.index for iss in result.issues}
    for seq in sorted(flagged):
        for iss in result.issues:
            if iss.index == seq:
                notes.setdefault(seq, []).append(
                    f"[校验问题/{iss.kind}] {iss.reason}")
    revised_vtt = write_vtt(result.cues, notes=notes)
    sid = db.save_submission(name, fmt, max_chars, min_dwell, max_cps,
                             result, revised_vtt)

    # 术语审校作用于上传的原始 cue（保留原始时间轴），与断句修订互不影响
    db.save_original_cues(sid, cues)
    terms = _load_glossary()
    review = review_terms(cues, terms)
    db.save_term_findings(sid, review["findings"])

    payload = _result_payload(sid, name, fmt,
                              {"max_chars": max_chars, "min_dwell": min_dwell,
                               "max_cps": max_cps}, result)
    payload["term_review"] = _term_review_payload(
        sid, db.get_term_findings(sid))
    return payload


@app.get("/api/submissions")
async def api_submissions():
    items = db.list_submissions()
    for it in items:
        it["stats"] = json.loads(it.get("stats_json") or "{}")
        it.pop("stats_json", None)
    return {"items": items, "language_names": LANGUAGE_NAMES}


@app.get("/api/submissions/{sid}")
async def api_get_submission(sid: int, language: str | None = None):
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    sub = record["submission"]
    issues = record["issues"]
    if language:
        issues = [i for i in issues if i["language"] == language]
    payload = {
        "id": sub["id"],
        "filename": sub["filename"],
        "format": sub["format"],
        "params": {
            "max_chars": sub["max_chars"],
            "min_dwell": sub["min_dwell"],
            "max_cps": sub["max_cps"],
        },
        "stats": json.loads(sub["stats_json"] or "{}"),
        "cues": [
            {
                "seq": c["seq"],
                "start": round(c["start_sec"], 3),
                "end": round(c["end_sec"], 3),
                "duration": round(c["end_sec"] - c["start_sec"], 3),
                "length": len(c["text"]),
                "cps": round(len(c["text"])
                             / max(1e-9, c["end_sec"] - c["start_sec"]), 1),
                "text": c["text"],
                "sources": [s + 1 for s in json.loads(c["source_indices"])],
                "redistributed": len(json.loads(c["source_indices"])) > 1,
            }
            for c in record["cues"]
        ],
        "issues": [
            {
                "cue_seq": i["cue_seq"],
                "kind": i["kind"],
                "severity": i["severity"],
                "language": i["language"],
                "language_name": LANGUAGE_NAMES.get(i["language"],
                                                    i["language"]),
                "reason": i["reason"],
                "segment": i["segment"],
                "start": round(i["start_sec"], 3),
                "end": round(i["end_sec"], 3),
                "redistributable": bool(i["redistributable"]),
            }
            for i in issues
        ],
        "language_names": LANGUAGE_NAMES,
        "term_review": _term_review_payload(
            sid, db.get_term_findings(sid)),
    }
    return payload


@app.get("/api/submissions/{sid}/export")
async def api_export(sid: int, language: str | None = None):
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    vtt = record["revised_vtt"]
    base = record["submission"]["filename"].rsplit(".", 1)[0]
    filename = f"{base}.revised.vtt"
    return Response(
        content=vtt,
        media_type="text/vtt; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename}"'},
    )


# --- 术语表管理 ---

class TermIn(BaseModel):
    language: str = "any"
    canonical: str = Field(min_length=1)
    allowed: list[str] | str = []
    forbidden: list[str] | str = []
    note: str = ""


class TermsImport(BaseModel):
    terms: list[TermIn]
    replace_all: bool = False


def _coerce_term(body: TermIn) -> dict:
    try:
        term = make_term(body.language, body.canonical,
                         body.allowed, body.forbidden, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "language": term.language,
        "canonical": term.canonical,
        "allowed": "\n".join(term.allowed),
        "forbidden": "\n".join(term.forbidden),
        "note": term.note,
    }


@app.get("/api/glossary")
async def api_list_terms(language: str | None = None):
    rows = db.list_glossary_terms(language)
    return {"items": [_term_row_payload(r) for r in rows],
            "language_names": LANGUAGE_NAMES}


@app.post("/api/glossary")
async def api_create_term(body: TermIn):
    data = _coerce_term(body)
    tid = db.create_glossary_term(**data)
    return _term_row_payload(db.get_glossary_term(tid))


@app.put("/api/glossary/{tid}")
async def api_update_term(tid: int, body: TermIn):
    if not db.get_glossary_term(tid):
        raise HTTPException(status_code=404, detail="术语不存在")
    data = _coerce_term(body)
    db.update_glossary_term(tid, **data)
    return _term_row_payload(db.get_glossary_term(tid))


@app.delete("/api/glossary/{tid}")
async def api_delete_term(tid: int):
    if not db.delete_glossary_term(tid):
        raise HTTPException(status_code=404, detail="术语不存在")
    return {"deleted": tid}


@app.post("/api/glossary/import")
async def api_import_terms(body: TermsImport):
    items = [_coerce_term(t) for t in body.terms]
    n = db.bulk_replace_glossary_terms(items, body.replace_all)
    return {"imported": n,
            "items": [_term_row_payload(r)
                      for r in db.list_glossary_terms()]}


@app.get("/api/glossary/export")
async def api_export_glossary():
    rows = db.list_glossary_terms()
    return {"terms": [
        {k: v for k, v in _term_row_payload(r).items()
         if k != "created_at"}
        for r in rows
    ]}


# --- 术语审校结果与决策 ---

@app.get("/api/submissions/{sid}/terms")
async def api_get_terms(sid: int, decision: str | None = None,
                        language: str | None = None):
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    findings = db.get_term_findings(sid, decision=decision,
                                    language=language)
    return _term_review_payload(sid, findings)


class DecisionIn(BaseModel):
    decision: str
    replacement: str | None = None


@app.put("/api/submissions/{sid}/terms/{fid}")
async def api_set_decision(sid: int, fid: int, body: DecisionIn):
    finding = db.get_term_finding(fid)
    if finding is None or finding["submission_id"] != sid:
        raise HTTPException(status_code=404, detail="疑似项不存在")
    if body.decision not in ("pending", "accepted", "rejected"):
        raise HTTPException(status_code=400, detail="decision 取值非法")
    replacement = body.replacement
    if body.decision == "accepted" and replacement is not None \
            and replacement == finding["surface"]:
        replacement = finding["canonical"]
    db.set_finding_decision(fid, body.decision, replacement)
    return _term_review_payload(sid, db.get_term_findings(sid))


class BulkDecisionIn(BaseModel):
    decision: str
    finding_ids: list[int] | None = None


@app.post("/api/submissions/{sid}/terms/decisions")
async def api_bulk_decision(sid: int, body: BulkDecisionIn):
    if db.get_artifact(sid) is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    if body.decision not in ("pending", "accepted", "rejected"):
        raise HTTPException(status_code=400, detail="decision 取值非法")
    n = db.bulk_set_decisions(sid, body.decision, body.finding_ids)
    return {"updated": n, **_term_review_payload(sid, db.get_term_findings(sid))}


@app.get("/api/submissions/{sid}/terms/export")
async def api_export_terms_vtt(sid: int):
    """根据已采纳决策在原始字幕上批量替换，导出修订 WebVTT。"""
    if db.get_artifact(sid) is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    cues = _original_cues_for(sid)
    accepted = [f for f in db.get_term_findings(sid)
                if f["decision"] == "accepted"]
    revised = apply_replacements(cues, [
        {"cue_seq": f["cue_seq"], "begin_offset": f["begin_offset"],
         "end_offset": f["end_offset"], "replacement": f["replacement"]}
        for f in accepted
    ])
    notes: dict[int, list[str]] = {}
    for f in accepted:
        notes.setdefault(f["cue_seq"], []).append(
            f"[术语审校] {f['surface']} → {f['replacement']}")
    vtt = write_vtt(revised, notes=notes)
    sub = db.get_artifact(sid)["submission"]
    base = sub["filename"].rsplit(".", 1)[0]
    return Response(
        content=vtt,
        media_type="text/vtt; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{base}.terms.vtt"'},
    )


@app.get("/api/submissions/{sid}/terms/report")
async def api_term_report(sid: int):
    """导出 Markdown 审校报告。"""
    record = db.get_artifact(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    sub = record["submission"]
    findings = db.get_term_findings(sid)
    counts = db.get_decision_counts(sid)
    report = _build_report(sub, findings, counts,
                           len(db.get_original_cues(sid)))
    base = sub["filename"].rsplit(".", 1)[0]
    return PlainTextResponse(
        content=report,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{base}.term-report.md"'},
    )


def _build_report(sub: dict, findings: list[dict],
                  counts: dict, cue_count: int) -> str:
    lines = [
        "# 术语审校报告",
        "",
        f"- 字幕文件：{sub['filename']}",
        f"- 任务编号：{sub['id']}",
        f"- 原始 cue 数：{cue_count}",
        f"- 疑似项总数：{len(findings)}",
        f"- 待确认：{counts['pending']} ｜ 已采纳：{counts['accepted']} "
        f"｜ 已忽略：{counts['rejected']}",
        "",
        "## 按类型统计",
        "",
    ]
    by_type: dict[str, int] = {}
    for f in findings:
        by_type[f["issue_type"]] = by_type.get(f["issue_type"], 0) + 1
    if by_type:
        for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
            lines.append(f"- {TYPE_LABELS.get(t, t)}：{n}")
    else:
        lines.append("- 无疑似项")
    lines += ["", "## 疑似项明细", ""]

    if not findings:
        lines.append("无。")
        return "\n".join(lines) + "\n"

    decision_label = {"pending": "待确认", "accepted": "已采纳",
                      "rejected": "已忽略"}
    for i, f in enumerate(findings, 1):
        lang = LANGUAGE_NAMES.get(f["language"], f["language"])
        lines += [
            f"### {i}. {f['canonical']} —— "
            f"{TYPE_LABELS.get(f['issue_type'], f['issue_type'])}",
            "",
            f"- 决策：{decision_label.get(f['decision'], f['decision'])}",
            f"- 语言：{lang}",
            f"- 来源 cue：{f['cue_seq'] + 1}"
            f"（{f['start']:.3f} → {f['end']:.3f}）",
            f"- 原表面形式：`{f['surface']}`",
            f"- 建议替换：`{f['replacement']}`",
            f"- 上下文：…{f['context_before']}"
            f"**{f['surface']}**{f['context_after']}…",
            f"- 说明：{f.get('reason') or ''}",
        ]
        if f["cross_cue"] and f.get("forms"):
            forms = "、".join(
                f"`{x['surface']}`（{x['count']} 次，cue "
                f"{', '.join(str(s + 1) for s in x['cue_seqs'])}）"
                for x in f["forms"]
            )
            lines.append(f"- 跨 cue 形式：{forms}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


@app.get("/api/health")
async def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
