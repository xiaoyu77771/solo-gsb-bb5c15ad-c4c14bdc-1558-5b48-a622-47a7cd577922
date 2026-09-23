"""端到端冒烟测试：

    DB_PATH=/tmp/test.db python3 tests/smoke_test.py

需要 fastapi / httpx（见 requirements.txt）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

VTT = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:01.300\n这是一个非常非常长的句子根本来不及读完\n\n"
    "00:00:01.300 --> 00:00:01.500\n后面紧接着另一句内容同样也是非常拥挤\n"
)
SRT = (
    "1\n00:00:01,000 --> 00:00:01,200\n"
    "This is the longest sentence and you will not have time\n\n"
    "2\n00:00:01,200 --> 00:00:01,400\n另一句中文也同样非常拥挤根本读不完呢\n"
)

# 术语审校专用字幕：时长充裕，专注检测一致性
TERM_VTT = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:04.000\nConnect to the WI-FI network now.\n\n"
    "00:00:04.000 --> 00:00:07.000\nThe new wi fi works great.\n\n"
    "00:00:07.000 --> 00:00:10.000\nWiFi is allowed but WLAN is forbidden.\n\n"
    "00:00:10.000 --> 00:00:13.000\nCheck our websyte for ＨＤＭＩ cables.\n\n"
    "00:00:13.000 --> 00:00:16.000\n云瑞同步功能即将上线\n"
)


def main() -> None:
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json() == {"status": "ok"}

        data = client.post(
            "/api/analyze",
            files={"file": ("demo.vtt", VTT, "text/vtt")},
            data={"max_chars": "10", "min_dwell": "0.8", "max_cps": "12"},
        ).json()
        assert all(len(c["text"]) <= 10 for c in data["cues"])
        assert data["stats"]["moved_boundaries"] >= 0
        assert data["stats"]["split_cues"] == 2
        assert {i["language"] for i in data["issues"]} == {"zh"}
        sid = data["id"]
        # 无术语表时也应返回空审校结果
        assert data["term_review"]["counts"]["pending"] == 0

        mixed = client.post(
            "/api/analyze",
            files={"file": ("demo.srt", SRT, "text/plain")},
            data={"max_chars": "12", "min_dwell": "1", "max_cps": "8"},
        ).json()
        assert {"zh", "en"} <= {i["language"] for i in mixed["issues"]}

        # 重叠 cue：切点应落在重叠区间内，统计为一次边界重分配
        overlap_vtt = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.000\n第一句内容比较长需要拆开显示才行的啊\n\n"
            "00:00:02.000 --> 00:00:04.000\n第二句内容也是比较长需要拆开显示啊\n"
        )
        ov = client.post(
            "/api/analyze",
            files={"file": ("ov.vtt", overlap_vtt, "text/vtt")},
            data={"max_chars": "10", "min_dwell": "0.8", "max_cps": "12"},
        ).json()
        assert ov["stats"]["moved_boundaries"] == 1
        for a, b in zip(ov["cues"], ov["cues"][1:]):
            if a["sources"] != b["sources"]:
                assert 2.0 <= a["end"] <= 3.0

        bad = client.post(
            "/api/analyze",
            files={"file": ("x.vtt", b"not a subtitle", "text/vtt")},
            data={"max_chars": "10", "min_dwell": "0.8", "max_cps": "12"},
        )
        assert bad.status_code == 400

        # ---- 术语表 CRUD ------------------------------------------------
        t_wifi = client.post("/api/glossary", json={
            "language": "en", "canonical": "Wi-Fi",
            "allowed": "WiFi", "forbidden": "WLAN",
            "note": "品牌写法",
        }).json()
        t_hdmi = client.post("/api/glossary", json={
            "language": "any", "canonical": "HDMI",
        }).json()
        t_web = client.post("/api/glossary", json={
            "language": "en", "canonical": "website",
        }).json()
        t_sync = client.post("/api/glossary", json={
            "language": "zh", "canonical": "云端同步",
        }).json()
        assert len(client.get("/api/glossary").json()["items"]) == 4

        # 规范词不能为空 / 变体不能与规范词相同
        assert client.post("/api/glossary",
                           json={"canonical": " "}).status_code == 422
        assert client.post("/api/glossary", json={
            "canonical": "USB", "allowed": ["USB"],
        }).status_code == 400

        upd = client.put(f"/api/glossary/{t_web['id']}", json={
            "language": "en", "canonical": "website",
            "allowed": ["web site"], "forbidden": "",
        })
        assert upd.status_code == 200
        assert upd.json()["allowed"] == ["web site"]

        # ---- 上传并执行术语审校 ----------------------------------------
        td = client.post(
            "/api/analyze",
            files={"file": ("terms.vtt", TERM_VTT, "text/vtt")},
            data={"max_chars": "40", "min_dwell": "0.5", "max_cps": "30"},
        ).json()
        tsid = td["id"]
        tr = td["term_review"]
        findings = tr["findings"]
        by_type = {}
        for f in findings:
            by_type[f["issue_type"]] = by_type.get(f["issue_type"], 0) + 1
        assert by_type.get("case_mismatch") == 1       # WI-FI
        assert by_type.get("punctuation_mismatch") >= 1  # wi fi
        assert by_type.get("forbidden") == 1           # WLAN
        assert by_type.get("width_mismatch") == 1       # ＨＤＭＩ
        fuzzy_surfaces = {f["surface"] for f in findings
                          if f["issue_type"] == "fuzzy"}
        assert "websyte" in fuzzy_surfaces             # 近似拼写
        assert "云瑞同步" in fuzzy_surfaces            # CJK 近似
        # WiFi 与规范词跨 cue 混用 → 变体混用提示
        assert by_type.get("variant_mixed") == 1
        # 每条疑似项都带上下文、来源 cue 与建议替换
        for f in findings:
            assert f["cue_seq"] >= 0
            assert f["suggestion"] == f["canonical"]
            assert "surface" not in f or f["surface"]

        # 按类型 / 状态过滤
        only_fuzzy = client.get(
            f"/api/submissions/{tsid}/terms?decision=pending").json()
        assert all(f["decision"] == "pending"
                   for f in only_fuzzy["findings"])

        # ---- 逐项决策 ---------------------------------------------------
        forbidden = next(f for f in findings
                         if f["issue_type"] == "forbidden")
        fuzzy_f = next(f for f in findings
                       if f["surface"] == "websyte")
        ignored = next(f for f in findings
                       if f["issue_type"] == "variant_mixed")

        r = client.put(
            f"/api/submissions/{tsid}/terms/{forbidden['id']}",
            json={"decision": "accepted"})
        assert r.status_code == 200
        assert r.json()["counts"]["accepted"] == 1
        r = client.put(
            f"/api/submissions/{tsid}/terms/{fuzzy_f['id']}",
            json={"decision": "accepted", "replacement": "website"})
        assert r.json()["counts"]["accepted"] == 2
        r = client.put(
            f"/api/submissions/{tsid}/terms/{ignored['id']}",
            json={"decision": "rejected"})
        assert r.json()["counts"]["rejected"] == 1

        # 非法决策被拒绝；不存在的疑似项 404
        assert client.put(
            f"/api/submissions/{tsid}/terms/{forbidden['id']}",
            json={"decision": "wat"}).status_code == 400
        assert client.put(
            f"/api/submissions/{sid}/terms/{forbidden['id']}",
            json={"decision": "accepted"}).status_code == 404

        # 批量：把剩余 pending 全部忽略
        bulk = client.post(
            f"/api/submissions/{tsid}/terms/decisions",
            json={"decision": "rejected"}).json()
        assert bulk["counts"]["pending"] == 0
        assert bulk["counts"]["accepted"] == 2
        assert bulk["counts"]["rejected"] == len(findings) - 2

        # ---- 导出术语修订 WebVTT ---------------------------------------
        tvtt = client.get(f"/api/submissions/{tsid}/terms/export")
        assert tvtt.status_code == 200
        assert tvtt.text.startswith("WEBVTT\n")
        assert "WLAN" not in tvtt.text
        assert "website" in tvtt.text
        assert "Wi-Fi" in tvtt.text           # WLAN 已替换为 Wi-Fi
        assert "[术语审校]" in tvtt.text      # NOTE 说明
        # 时间轴保持原样（原始 cue 未被断句）
        assert "00:00:01.000 --> 00:00:04.000" in tvtt.text

        # ---- 导出 Markdown 审校报告 ------------------------------------
        report = client.get(f"/api/submissions/{tsid}/terms/report")
        assert report.status_code == 200
        assert "text/markdown" in report.headers["content-type"]
        assert "# 术语审校报告" in report.text
        assert "Wi-Fi" in report.text
        assert "已采纳" in report.text

        # ---- 术语表批量导入 / 删除 --------------------------------------
        imp = client.post("/api/glossary/import", json={
            "terms": [
                {"language": "de", "canonical": "USB-C",
                 "forbidden": ["USB C"]},
            ],
        }).json()
        assert imp["imported"] == 1
        assert client.delete(f"/api/glossary/{t_sync['id']}").status_code == 200
        assert client.delete("/api/glossary/99999").status_code == 404
        exported = client.get("/api/glossary/export").json()
        assert any(t["canonical"] == "USB-C" for t in exported["terms"])

        # ---- 历史与单文件分析保持兼容 -----------------------------------
        assert len(client.get("/api/submissions").json()["items"]) == 4
        detail = client.get(f"/api/submissions/{sid}").json()
        assert len(detail["cues"]) == len(data["cues"])
        assert "term_review" in detail
        zh = client.get(f"/api/submissions/{sid}?language=zh").json()
        assert all(i["language"] == "zh" for i in zh["issues"])

        export = client.get(f"/api/submissions/{sid}/export")
        assert export.status_code == 200
        assert export.text.startswith("WEBVTT\n")
        assert export.text.count("-->") == len(data["cues"])

    print("SMOKE_TESTS_OK")


if __name__ == "__main__":
    main()
