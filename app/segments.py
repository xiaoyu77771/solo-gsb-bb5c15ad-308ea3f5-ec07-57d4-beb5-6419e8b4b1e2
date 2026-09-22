"""分段线性时钟映射拟合。

匹配锚点形如 ``(x, y) = (待校准设备时间秒, 参考时间秒)``。设备时钟相对参考
时钟可能存在分段漂移（NTP 跳变、晶振频率变化等），因此对锚点做**分段线性
最小二乘拟合**，每段映射为 ``y = slope * x + intercept``。

分段策略（贪心扩张）：从左到右，每段先取两个锚点，只要纳入下一个锚点后
* 段内最大绝对残差不超过 ``residual_tol``，且
* 拟合斜率落在 ``[1/slope_clip, slope_clip]``（物理上合理的漂移范围），

就继续扩张，否则在当前锚点处断开。无法用直线描述的“跳变点”会自然落在两段
之间，段间空隙的查询用逆距离加权混合两段预测值（最多在单段内取到该段直线，
跳变位置附近平滑过渡）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Segment:
    """一段线性时钟映射 ``y = slope * x + intercept``。"""

    x0: float
    x1: float
    slope: float
    intercept: float
    anchor_count: int
    rms_residual: float

    def predict(self, x: float) -> float:
        return self.slope * x + self.intercept


def _ols(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float, float]:
    """最小二乘线性拟合，返回 (slope, intercept, rms 残差)。

    x 完全相同（含仅一个点）时退化为斜率 1、截距取 y-x 中位数。
    """
    n = len(xs)
    if n == 1:
        return 1.0, ys[0] - xs[0], 0.0

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    # x 的取值跨度极小（小于 1 微秒）时视为“x 全部相同”，退化为斜率 1；
    # 用绝对阈值，避免对纪元秒量级或小量级的 x 产生尺度依赖误判。
    if sxx <= 1e-12:
        intercepts = sorted(y - x for x, y in zip(xs, ys))
        intercept = (
            intercepts[n // 2]
            if n % 2
            else (intercepts[n // 2 - 1] + intercepts[n // 2]) / 2.0
        )
        rms = math.sqrt(sum((y - (x + intercept)) ** 2 for x, y in zip(xs, ys)) / n)
        return 1.0, intercept, rms

    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    rms = math.sqrt(
        sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)) / n
    )
    return slope, intercept, rms


def _max_abs_residual(
    xs: Sequence[float],
    ys: Sequence[float],
    slope: float,
    intercept: float,
) -> float:
    return max(abs(y - (slope * x + intercept)) for x, y in zip(xs, ys))


def _clip_slope(slope: float, clip: float) -> float:
    return min(clip, max(1.0 / clip, slope))


def _segment_stats(
    anchors: Sequence[Tuple[float, float]],
    slope_clip: float,
) -> Tuple[float, float, float, float]:
    xs = [a[0] for a in anchors]
    ys = [a[1] for a in anchors]
    slope, intercept, rms = _ols(xs, ys)
    slope = _clip_slope(slope, slope_clip)
    intercept = sum(ys) / len(ys) - slope * sum(xs) / len(xs)
    max_res = _max_abs_residual(xs, ys, slope, intercept)
    rms = math.sqrt(
        sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)) / len(xs)
    )
    return slope, intercept, rms, max_res


def fit_segments(
    anchors: Sequence[Tuple[float, float]],
    residual_tol: float,
    slope_clip: float = 2.0,
) -> List[Segment]:
    """对已按 x 升序排列的锚点贪心拟合分段直线。

    Args:
        anchors: ``[(设备时间, 参考时间), ...]``。
        residual_tol: 段内允许的最大绝对残差（秒），超出则分段。
        slope_clip: 斜率裁剪倍数，物理上时钟漂移率不应远离 1。

    Returns:
        分段列表，x 区间互不重叠且整体连续覆盖锚点范围。
    """
    pts = sorted(anchors, key=lambda a: a[0])
    segments: List[Segment] = []
    i = 0
    n = len(pts)
    while i < n:
        end = min(i + 2, n)
        while end < n:
            slope, intercept, _, max_res = _segment_stats(pts[i : end + 1], slope_clip)
            if max_res > residual_tol:
                break
            end += 1

        slope, intercept, rms, _ = _segment_stats(pts[i:end], slope_clip)
        segments.append(
            Segment(
                x0=pts[i][0],
                x1=pts[end - 1][0],
                slope=slope,
                intercept=intercept,
                anchor_count=end - i,
                rms_residual=rms,
            )
        )
        i = end
    return segments


def map_x(
    segments: Sequence[Segment],
    x: float,
    crossfade_gap: float = 5.0,
) -> Tuple[float, Optional[int], Optional[int], float]:
    """查询校准时间。

    Args:
        segments: 分段映射。
        x: 待校准设备时间（秒）。
        crossfade_gap: 段间空隙中逆距离混合的参考宽度（秒）。

    Returns:
        ``(校准时间, 段A下标, 段B下标, 段A权重)``；落在单段内时 A==B 且
        权重为 1；无段时校准时间等于原值。
    """
    if not segments:
        return x, None, None, 1.0

    if x <= segments[0].x0:
        seg = segments[0]
        return seg.predict(x), 0, 0, 1.0
    if x >= segments[-1].x1:
        seg = segments[-1]
        idx = len(segments) - 1
        return seg.predict(x), idx, idx, 1.0

    for k, seg in enumerate(segments):
        if seg.x0 <= x <= seg.x1:
            return seg.predict(x), k, k, 1.0
        if k + 1 < len(segments) and seg.x1 < x < segments[k + 1].x0:
            left, right = seg, segments[k + 1]
            gap = right.x0 - left.x1
            # 逆距离混合：空隙窄时线性交叉；空隙宽（漂移未知区）时只有靠近
            # 边界才显著混合，中段几乎等于较近一段的直线。
            denom = max(gap, crossfade_gap)
            blend = 0.0 if gap <= 1e-9 else (
                max(0.0, min(1.0, (x - left.x1) / denom))
            )
            y = (1.0 - blend) * left.predict(x) + blend * right.predict(x)
            return y, k, k + 1, 1.0 - blend

    return x, None, None, 1.0


def robust_global_fit(
    anchors: Sequence[Tuple[float, float]],
    slope_clip: float = 2.0,
) -> Tuple[float, float, List[float]]:
    """对全部锚点做一次稳健全局拟合（裁剪斜率），返回斜率、截距与残差列表。"""
    xs = [a[0] for a in anchors]
    ys = [a[1] for a in anchors]
    slope, intercept, _ = _ols(xs, ys)
    slope = _clip_slope(slope, slope_clip)
    intercept = sum(ys) / len(ys) - slope * sum(xs) / len(xs)
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    return slope, intercept, residuals
