from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_align_endpoint():
    body = {
        "reference": (
            '{"ts": "2026-09-22T10:00:00Z", "label": "A"}\n'
            '{"ts": "2026-09-22T10:00:10Z", "label": "B"}\n'
            '{"ts": "2026-09-22T10:00:20Z", "label": "C"}'
        ),
        "target": (
            '{"ts": "2026-09-22T10:00:03Z", "label": "X"}\n'
            '{"ts": "2026-09-22T10:00:13Z", "label": "A"}\n'
            '{"ts": "2026-09-22T10:00:23Z", "label": "B"}\n'
            '{"ts": "2026-09-22T10:00:33Z", "label": "C"}'
        ),
        "residualTol": 0.5,
    }
    resp = client.post("/align", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["metrics"]["matched_anchors"] == 3
    assert data["metrics"]["inserted_in_target"] == 1
    assert len(data["aligned_records"]) == 4
    assert data["matched"][0]["label"] == "A"


def test_align_validation_error_is_400():
    resp = client.post(
        "/align", json={"reference": "garbage", "target": '{"ts":1,"label":"A"}'}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "alignment_failed"


def test_upload_endpoint():
    ref = '{"ts": 1000, "label": "A"}\n{"ts": 1010, "label": "B"}'
    tgt = '{"ts": 2000, "label": "A"}\n{"ts": 2010, "label": "B"}'
    resp = client.post(
        "/align/upload",
        files={
            "reference": ("reference.jsonl", ref, "application/x-ndjson"),
            "target": ("target.jsonl", tgt, "application/x-ndjson"),
        },
        data={"residual_tol": "0.5"},
    )
    assert resp.status_code == 200
    assert resp.json()["metrics"]["matched_anchors"] == 2
