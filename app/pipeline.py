"""时钟校准流水线：JSONL 解析 -> 单调匹配 -> 分段拟合 -> 校准输出。"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from .matching import Event, MatchParams, two_pass_match
from .segments import Segment, fit_segments, map_x, robust_global_fit
from .timestamps import Timestamp, parse_timestamp

# 无锚点时的兜底置信度
_NO_ANCHOR_CONFIDENCE = 0.0


class AlignmentError(ValueError):
    """输入数据无法完成校准（格式错误、标签缺失等）。"""


@dataclass
class AlignParams:
    """/align 请求参数（见 API 层默认值与字段说明）。"""

    timestamp_field: str = "ts"
    label_field: str = "label"
    time_pref_scale: float = 60.0
    slope_clip: float = 2.0
    residual_tol: Optional[float] = None
    default_residual_tol: float = 0.5
    outlier_z: float = 6.0
    confidence_decay_seconds: float = 60.0
    max_records: int = 50_000


@dataclass
class MatchedPair:
    """一条匹配锚点的输出结构。"""

    ref_index: int
    target_index: int
    label: str
    ref_ts: Any
    target_ts: Any
    aligned_ts: Any
    offset_seconds: float
    residual_seconds: float
    confidence: float


@dataclass
class AlignedRecord:
    """一条待校准记录的输出结构。"""

    index: int
    matched: bool
    label: str
    original_ts: Any
    aligned_ts: Any
    delta_seconds: float
    confidence: float
    ref_index: Optional[int]
    segment: Optional[int]
    record: Dict[str, Any]


@dataclass
class UnmatchedRecord:
    """未匹配记录。side=ref 表示参考独有（目标缺失），target 表示插入事件。"""

    side: str
    index: int
    label: str
    ts: Any
    record: Dict[str, Any]


@dataclass
class SegmentInfo:
    """分段线性映射的输出结构。"""

    index: int
    x0_ts: Any
    x1_ts: Any
    x0_seconds: float
    x1_seconds: float
    slope: float
    intercept_seconds: float
    anchor_count: int
    rms_residual_seconds: float


@dataclass
class PipelineResult:
    """完整校准结果。"""

    matched: List[MatchedPair] = field(default_factory=list)
    aligned_records: List[AlignedRecord] = field(default_factory=list)
    unmatched_ref: List[UnmatchedRecord] = field(default_factory=list)
    unmatched_target: List[UnmatchedRecord] = field(default_factory=list)
    segments: List[SegmentInfo] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# JSONL 解析
# ---------------------------------------------------------------------------


def parse_jsonl(
    text: str,
    source: str,
    params: AlignParams,
) -> List[Tuple[Dict[str, Any], Timestamp]]:
    """解析 JSONL 文本，返回 (原始记录, 时间戳) 列表。"""
    if not text or not text.strip():
        raise AlignmentError(f"{source} 为空")

    out: List[Tuple[Dict[str, Any], Timestamp]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AlignmentError(f"{source} 第 {lineno} 行不是合法 JSON: {exc.msg}")
        if not isinstance(obj, dict):
            raise AlignmentError(f"{source} 第 {lineno} 行必须是 JSON 对象")
        if params.timestamp_field not in obj:
            raise AlignmentError(
                f"{source} 第 {lineno} 行缺少时间戳字段 {params.timestamp_field!r}"
            )
        if params.label_field not in obj:
            raise AlignmentError(
                f"{source} 第 {lineno} 行缺少标签字段 {params.label_field!r}"
            )
        label = obj[params.label_field]
        if not isinstance(label, (str, int)) or isinstance(label, bool):
            raise AlignmentError(
                f"{source} 第 {lineno} 行标签必须是字符串或整数"
            )
        try:
            ts = parse_timestamp(obj[params.timestamp_field])
        except ValueError as exc:
            raise AlignmentError(f"{source} 第 {lineno} 行: {exc}")
        out.append((obj, ts))

    if not out:
        raise AlignmentError(f"{source} 不包含任何有效记录")
    if len(out) > params.max_records:
        raise AlignmentError(
            f"{source} 记录数 {len(out)} 超过上限 {params.max_records}"
        )
    return out


# ---------------------------------------------------------------------------
# 置信度
# ---------------------------------------------------------------------------


def _anchor_confidence(residual: float, tol: float, blend: float = 1.0) -> float:
    score = math.exp(-0.5 * (abs(residual) / max(tol, 1e-9)) ** 2)
    return round(min(1.0, score * blend), 6)


def _gap_scale(anchor_xs: List[float]) -> float:
    if len(anchor_xs) >= 2:
        gaps = [b - a for a, b in zip(anchor_xs, anchor_xs[1:]) if b > a]
        if gaps:
            return max(statistics.median(gaps), 1e-9)
    return 1.0


def _interior_density(x: float, anchor_xs: List[float]) -> Tuple[float, bool]:
    """锚点跨度内：到所在间隙两端点的最近距离。"""
    for a, b in zip(anchor_xs, anchor_xs[1:]):
        if a <= x <= b:
            return min(x - a, b - x), True
    return 0.0, True


def _density(x: float, anchor_xs: List[float]) -> Tuple[float, bool]:
    """返回 (到最近锚点的距离, 是否在锚点跨度内)。"""
    if not anchor_xs:
        return 0.0, False
    if x <= anchor_xs[0]:
        return anchor_xs[0] - x, False
    if x >= anchor_xs[-1]:
        return x - anchor_xs[-1], False
    return _interior_density(x, anchor_xs)


# ---------------------------------------------------------------------------
# 稳健锚点剪枝
# ---------------------------------------------------------------------------


def _robust_prune(
    pairs: List[Tuple[int, int]],
    ref: List[Event],
    target: List[Event],
    tol: float,
    outlier_z: float,
) -> List[Tuple[int, int]]:
    """全局稳健拟合 + 残差剪枝。残差全部很小时一律保留（不剪真实斜率变化点）。"""
    if len(pairs) < 6:
        return pairs

    anchors = [(target[j].t, ref[i].t) for i, j in pairs]
    _, _, residuals = robust_global_fit(anchors)
    med = statistics.median(residuals)
    mad = statistics.median([abs(r - med) for r in residuals])
    sigma = 1.4826 * mad
    # 残差普遍很紧凑：没有真正的离群点
    if sigma <= tol / max(outlier_z, 1.0):
        return pairs
    threshold = max(outlier_z * sigma, tol)
    kept = [
        pair
        for pair, res in zip(pairs, residuals)
        if abs(res) <= threshold
    ]
    # 至少保留 3 个锚点、且最多丢弃 40%，避免过度剪枝
    if len(kept) < 3 or len(kept) < 0.6 * len(pairs):
        return pairs
    return kept


# ---------------------------------------------------------------------------
# 主流水线
# ---------------------------------------------------------------------------


def run_alignment(ref_text: str, target_text: str, params: AlignParams) -> PipelineResult:
    """执行完整校准流程。"""
    ref_records = parse_jsonl(ref_text, "参考日志", params)
    tgt_records = parse_jsonl(target_text, "待校准日志", params)

    ref = [
        Event(index=i, label=str(rec[params.label_field]), t=ts.seconds)
        for i, (rec, ts) in enumerate(ref_records)
    ]
    target = [
        Event(index=i, label=str(rec[params.label_field]), t=ts.seconds)
        for i, (rec, ts) in enumerate(tgt_records)
    ]

    raw_pairs = two_pass_match(
        ref, target, MatchParams(time_pref_scale=params.time_pref_scale)
    )

    tol = (
        params.residual_tol
        if params.residual_tol is not None
        else params.default_residual_tol
    )
    pairs = _robust_prune(raw_pairs, ref, target, tol, params.outlier_z)

    result = PipelineResult()

    anchor_xs = sorted(target[j].t for _, j in pairs)
    gap_scale = _gap_scale(anchor_xs)
    decay = max(params.confidence_decay_seconds, gap_scale)

    # 每个目标事件的映射信息
    aligned_seconds: List[float] = [0.0] * len(target)
    mapped_segments: List[Optional[int]] = [None] * len(target)
    anchor_residual: Dict[int, float] = {}
    anchor_confidence: Dict[int, float] = {}

    matched_ref: Dict[int, int] = {}
    matched_tgt: Dict[int, int] = {}
    for r_idx, t_idx in pairs:
        matched_ref[r_idx] = t_idx
        matched_tgt[t_idx] = r_idx

    if not pairs:
        # 完全没有共同标签：原样输出，置信度为 0
        for t_ev, (rec, ts) in zip(target, tgt_records):
            aligned_seconds[t_ev.index] = t_ev.t
            mapped_segments[t_ev.index] = None
        segments: List[Segment] = []
    else:
        anchors = [(target[j].t, ref[i].t) for i, j in pairs]
        segments = fit_segments(anchors, residual_tol=tol, slope_clip=params.slope_clip)

        for i, j in pairs:
            x, y = target[j].t, ref[i].t
            y_map, ka, kb, wa = map_x(segments, x)
            residual = y_map - y
            if ka != kb:
                # 锚点落在段间跳变区：两段都无法精确描述，按混合权重折扣
                conf = wa * _anchor_confidence(
                    segments[ka].predict(x) - y, tol
                ) + (1.0 - wa) * _anchor_confidence(
                    segments[kb].predict(x) - y, tol
                )
            else:
                conf = _anchor_confidence(residual, tol)
            anchor_residual[j] = residual
            anchor_confidence[j] = round(conf, 6)

        for t_ev in target:
            y_map, ka, _, _ = map_x(segments, t_ev.t)
            aligned_seconds[t_ev.index] = y_map
            mapped_segments[t_ev.index] = ka

    # 待校准记录逐条输出
    for t_ev, (rec, ts) in zip(target, tgt_records):
        j = t_ev.index
        aligned_ts = _format_at(ts, aligned_seconds[j])
        if j in matched_tgt:
            confidence = anchor_confidence[j]
        elif not pairs:
            confidence = _NO_ANCHOR_CONFIDENCE
        else:
            distance, interior = _density(t_ev.t, anchor_xs)
            confidence = round(
                0.8 * math.exp(-distance / decay) if interior
                else 0.5 * math.exp(-distance / decay),
                6,
            )
        result.aligned_records.append(
            AlignedRecord(
                index=j,
                matched=j in matched_tgt,
                label=t_ev.label,
                original_ts=rec[params.timestamp_field],
                aligned_ts=aligned_ts,
                delta_seconds=round(aligned_seconds[j] - t_ev.t, 6),
                confidence=confidence,
                ref_index=matched_tgt.get(j),
                segment=mapped_segments[j],
                record=rec,
            )
        )

    # 参考独有：目标缺失的事件
    for i, (rec, ts) in enumerate(ref_records):
        if i not in matched_ref:
            result.unmatched_ref.append(
                UnmatchedRecord(
                    side="ref",
                    index=i,
                    label=str(rec[params.label_field]),
                    ts=rec[params.timestamp_field],
                    record=rec,
                )
            )

    # 目标独有：插入的噪声事件
    for j, (rec, ts) in enumerate(tgt_records):
        if j not in matched_tgt:
            result.unmatched_target.append(
                UnmatchedRecord(
                    side="target",
                    index=j,
                    label=str(rec[params.label_field]),
                    ts=rec[params.timestamp_field],
                    record=rec,
                )
            )

    # 匹配对详情
    for i, j in pairs:
        r_ev, t_ev = ref[i], target[j]
        aligned_ts = _format_at(tgt_records[j][1], aligned_seconds[j])
        result.matched.append(
            MatchedPair(
                ref_index=i,
                target_index=j,
                label=r_ev.label,
                ref_ts=ref_records[i][0][params.timestamp_field],
                target_ts=tgt_records[j][0][params.timestamp_field],
                aligned_ts=aligned_ts,
                offset_seconds=round(t_ev.t - r_ev.t, 6),
                residual_seconds=round(anchor_residual[j], 6),
                confidence=anchor_confidence[j],
            )
        )

    # 分段信息
    for k, seg in enumerate(segments):
        result.segments.append(
            SegmentInfo(
                index=k,
                x0_ts=_format_at(tgt_records[0][1], seg.x0),
                x1_ts=_format_at(tgt_records[0][1], seg.x1),
                x0_seconds=round(seg.x0, 6),
                x1_seconds=round(seg.x1, 6),
                slope=round(seg.slope, 9),
                intercept_seconds=round(seg.intercept, 6),
                anchor_count=seg.anchor_count,
                rms_residual_seconds=round(seg.rms_residual, 6),
            )
        )

    residuals = [abs(p.residual_seconds) for p in result.matched]
    max_res = round(max(residuals), 6) if residuals else None
    rms = (
        round(math.sqrt(sum(r * r for r in residuals) / len(residuals)), 6)
        if residuals
        else None
    )
    confidences = [r.confidence for r in result.aligned_records]
    result.metrics = {
        "ref_count": float(len(ref)),
        "target_count": float(len(target)),
        "matched_anchors": float(len(pairs)),
        "raw_candidate_matches": float(len(raw_pairs)),
        "pruned_anchors": float(len(raw_pairs) - len(pairs)),
        "missing_in_target": float(len(result.unmatched_ref)),
        "inserted_in_target": float(len(result.unmatched_target)),
        "segment_count": float(len(segments)),
        "max_abs_residual_seconds": max_res,
        "rms_residual_seconds": rms,
        "mean_confidence": round(statistics.fmean(confidences), 6)
        if confidences
        else 0.0,
        "match_coverage": round(len(pairs) / len(target), 6) if target else 0.0,
    }
    return result


def _format_at(ts_template: Timestamp, seconds: float) -> Any:
    """以某条记录的时间戳形态输出给定秒值。"""
    return replace(ts_template, seconds=seconds).format()
