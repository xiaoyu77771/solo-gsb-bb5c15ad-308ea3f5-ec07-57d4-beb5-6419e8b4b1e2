import json
from datetime import datetime, timedelta, timezone

from app.pipeline import AlignParams, AlignmentError, run_alignment

T0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)


def iso(t):
    return (T0 + timedelta(seconds=t)).isoformat().replace("+00:00", "Z")


def lines(rows):
    return "\n".join(json.dumps({"ts": iso(y), "label": label}) for y, label in rows)


REF = [
    (0, "POWER_ON"),
    (10, "VALVE_OPEN"),
    (25, "PRESSURE_OK"),
    (40, "MOTOR_START"),
    (60, "SENSOR_PING"),
    (120, "SENSOR_PING"),
    (150, "HEAT_ON"),
    (180, "SENSOR_PING"),
    (200, "VALVE_CLOSE"),
    (230, "COOLING_ON"),
    (260, "MOTOR_STOP"),
    (300, "POWER_OFF"),
]


def device(t):
    if t <= 100:
        return (t - 5) / 1.02
    return (t - 12) / 0.98


def build_target():
    rows = [(device(y), label) for y, label in REF if label != "PRESSURE_OK"]
    rows += [(80, "SENSOR_NOISE"), (250, "MAINT_LOG")]
    return lines(sorted(rows))


def test_end_to_end_alignment_quality():
    result = run_alignment(lines(REF), build_target(), AlignParams(residual_tol=0.5))

    assert result.metrics["matched_anchors"] == 11
    assert result.metrics["missing_in_target"] == 1
    assert result.metrics["inserted_in_target"] == 2
    assert result.metrics["segment_count"] == 2
    assert result.metrics["max_abs_residual_seconds"] < 0.5

    # 校准时间戳接近参考时间（全部记录含插入事件）
    for rec in result.aligned_records:
        assert rec.aligned_ts is not None
        assert 0.0 <= rec.confidence <= 1.0

    # 锚点置信度应较高
    assert min(m.confidence for m in result.matched) > 0.6

    # 缺失/插入事件归类正确
    missing = [r.label for r in result.unmatched_ref]
    inserted = [r.label for r in result.unmatched_target]
    assert missing == ["PRESSURE_OK"]
    assert set(inserted) == {"SENSOR_NOISE", "MAINT_LOG"}


def test_segment_parameters_recover_drift():
    result = run_alignment(lines(REF), build_target(), AlignParams(residual_tol=0.5))
    segs = result.segments
    assert abs(segs[0].slope - 1.02) < 5e-4
    assert abs(segs[1].slope - 0.98) < 5e-4


def test_no_common_labels():
    ref = lines([(0, "A"), (10, "B")])
    tgt = lines([(100, "X"), (110, "Y")])
    result = run_alignment(ref, tgt, AlignParams())
    assert result.metrics["matched_anchors"] == 0
    assert len(result.unmatched_ref) == 2
    assert len(result.unmatched_target) == 2
    assert all(r.confidence == 0.0 for r in result.aligned_records)
    # 无锚点时时间戳原样返回
    assert result.aligned_records[0].aligned_ts == result.aligned_records[0].original_ts


def test_numeric_epoch_milliseconds_preserved():
    ref = "\n".join(
        json.dumps({"ts": 1_789_000_000_000 + i * 10_000, "label": f"E{i}"})
        for i in range(5)
    )
    tgt = "\n".join(
        json.dumps({"ts": 1_789_000_000_000 + 30_000 + i * 10_000, "label": f"E{i}"})
        for i in range(5)
    )
    result = run_alignment(ref, tgt, AlignParams(residual_tol=0.1))
    assert result.metrics["matched_anchors"] == 5
    assert isinstance(result.aligned_records[0].aligned_ts, int)
    # 30 秒常数偏移应被校准掉
    assert result.aligned_records[0].aligned_ts == 1_789_000_000_000


def test_invalid_jsonl_rejected():
    import pytest

    with pytest.raises(AlignmentError):
        run_alignment('{"ts": 1, "label": "A"}', "not json at all", AlignParams())
    with pytest.raises(AlignmentError):
        run_alignment(
            '{"ts": 1, "label": "A"}', '{"ts": 1}', AlignParams()
        )
    with pytest.raises(AlignmentError):
        run_alignment("", '{"ts": 1, "label": "A"}', AlignParams())
