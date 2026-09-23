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
    "00:00:01.300 --> 00:00:01.500\n后面紧接着另一句内容同样也是超长的呢\n"
)
SRT = (
    "1\n00:00:01,000 --> 00:00:01,200\n"
    "This is the longest sentence and you will not have time\n\n"
    "2\n00:00:01,200 --> 00:00:01,400\n另一句中文也同样非常拥挤根本读不完呢\n"
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
        assert data["term_findings"] == 0  # 术语表为空时不产生术语发现
        sid = data["id"]

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

        assert len(client.get("/api/submissions").json()["items"]) == 3
        detail = client.get(f"/api/submissions/{sid}").json()
        assert len(detail["cues"]) == len(data["cues"])
        zh = client.get(f"/api/submissions/{sid}?language=zh").json()
        assert all(i["language"] == "zh" for i in zh["issues"])

        export = client.get(f"/api/submissions/{sid}/export")
        assert export.status_code == 200
        assert export.text.startswith("WEBVTT\n")
        assert export.text.count("-->") == len(data["cues"])

        # --- 术语审校 ---
        # 1. 维护按语言区分的术语表
        r = client.post("/api/terms", json={
            "language": "all", "canonical": "WebVTT",
            "forbidden": ["webvtt"], "note": "格式名称",
        })
        assert r.status_code == 201
        assert client.post("/api/terms", json={
            "language": "zh", "canonical": "账号",
            "forbidden": ["帐号"],
        }).status_code == 201
        assert client.post("/api/terms", json={
            "language": "zh", "canonical": "人工智能",
        }).status_code == 201
        # 校验：禁止替代词不能与规范词相同
        assert client.post("/api/terms", json={
            "language": "zh", "canonical": "账号",
            "forbidden": ["账号"],
        }).status_code == 400

        terms = client.get("/api/terms").json()
        assert len(terms["items"]) == 3
        assert terms["languages"]["all"] == "通用"
        # 更新术语
        first_id = terms["items"][0]["id"]
        assert client.put(f"/api/terms/{first_id}", json={
            "language": "all", "canonical": "WebVTT",
            "forbidden": ["webvtt"],
        }).status_code == 200

        # 2. 上传含各类不一致写法的字幕（每条 cue 均短于阈值，不会被重新切分）
        term_vtt = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.000\n本字幕采用 WebVTT 格式发布\n\n"
            "00:00:03.000 --> 00:00:05.000\nwebvtt 可以直接在浏览器播放\n\n"
            "00:00:05.000 --> 00:00:07.000\n全角写法 ＷｅｂＶＴＴ 不统一\n\n"
            "00:00:07.000 --> 00:00:09.000\n也会写成 Web VTT 的形式\n\n"
            "00:00:09.000 --> 00:00:11.000\n偶见笔误 WebVTs 需修正\n\n"
            "00:00:11.000 --> 00:00:13.000\n请在账号设置里绑定邮箱\n\n"
            "00:00:13.000 --> 00:00:15.000\n帐号安全同样也很重要\n\n"
            "00:00:15.000 --> 00:00:17.000\n人工智能改变译制流程\n\n"
            "00:00:17.000 --> 00:00:19.000\n人工知能译法需要统一\n"
        )
        checked = client.post(
            "/api/analyze",
            files={"file": ("terms-demo.vtt", term_vtt, "text/vtt")},
            data={"max_chars": "30", "min_dwell": "0.5", "max_cps": "20"},
        ).json()
        assert len(checked["cues"]) == 9
        assert checked["term_findings"] == 6
        tsid = checked["id"]

        findings = client.get(
            f"/api/submissions/{tsid}/term-findings").json()["items"]
        by_cat = {}
        matched = set()
        for f in findings:
            by_cat[f["category"]] = by_cat.get(f["category"], 0) + 1
            matched.add(f["matched"])
            # 上下文与偏移一致，来源 cue 与修订 cue 一一对应
            assert f["context"][f["start_off"]:f["end_off"]] == f["matched"]
            assert f["source_cues"] == [f["cue_seq"] + 1]
            assert f["status"] == "pending"
        assert by_cat == {"forbidden": 2, "width": 1, "punct": 1,
                          "spelling": 2}
        assert {"webvtt", "帐号", "ＷｅｂＶＴＴ", "Web VTT",
                "WebVTs", "人工知能"} <= matched

        # 3. 逐项/批量确认：接受全部
        ids = [f["id"] for f in findings]
        st = client.post("/api/terms/findings/status", json={
            "ids": ids, "status": "accepted",
        })
        assert st.status_code == 200
        assert st.json()["updated"] == 6
        assert client.post("/api/terms/findings/status", json={
            "ids": [1], "status": "bogus",
        }).status_code == 400

        # 4. 导出术语修订 WebVTT：所有问题写法被规范词替换
        revised = client.get(f"/api/submissions/{tsid}/term-export")
        assert revised.status_code == 200
        assert revised.headers["X-Term-Replacements"] == "6"
        assert revised.text.count("-->") == 9
        assert "webvtt" not in revised.text
        assert "帐号" not in revised.text
        assert "人工知能" not in revised.text
        assert revised.text.count("WebVTT") == 5
        assert revised.text.count("账号") == 2
        assert revised.text.count("人工智能") == 2

        # 忽略的发现不应被替换
        client.post("/api/terms/findings/status", json={
            "ids": ids[:1], "status": "dismissed",
        })
        partly = client.get(f"/api/submissions/{tsid}/term-export")
        assert partly.headers["X-Term-Replacements"] == "5"

        # 5. 重新检测：发现重建，确认状态重置为待确认
        recheck = client.post(
            f"/api/submissions/{tsid}/term-check").json()
        assert len(recheck["items"]) == 6
        assert all(f["status"] == "pending" for f in recheck["items"])

        # 6. 审校报告：CSV（含 BOM 与中文表头）与 JSON
        report = client.get(f"/api/submissions/{tsid}/term-report")
        assert report.status_code == 200
        assert report.text.startswith("﻿")
        assert "规范词,术语语言,类别" in report.text
        assert "WebVTT" in report.text and "待确认" in report.text
        rj = client.get(
            f"/api/submissions/{tsid}/term-report?format=json").json()
        assert len(rj["items"]) == 6

        # 删除全部术语后重新检测，发现清空
        client.delete(f"/api/terms/{first_id}")
        for t in client.get("/api/terms").json()["items"]:
            assert client.delete(f"/api/terms/{t['id']}").status_code == 200
        assert client.post(
            f"/api/submissions/{tsid}/term-check").json()["items"] == []
        assert client.delete("/api/terms/9999").status_code == 404

    print("SMOKE_TESTS_OK")


if __name__ == "__main__":
    main()
