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

    print("SMOKE_TESTS_OK")


if __name__ == "__main__":
    main()
