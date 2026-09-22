"""字幕断句校验台 — FastAPI 入口。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from . import database as db
from .segmenter import analyze
from .subtitles import LANGUAGE_NAMES, parse_subtitle, write_vtt

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

    return _result_payload(sid, name, fmt,
                           {"max_chars": max_chars, "min_dwell": min_dwell,
                            "max_cps": max_cps}, result)


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


@app.get("/api/health")
async def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
