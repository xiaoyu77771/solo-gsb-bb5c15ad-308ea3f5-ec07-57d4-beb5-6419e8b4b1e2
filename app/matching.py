"""单调标签匹配。

核心问题与生物信息学中的序列比对 / 最长公共子序列（LCS）类似：

* 参考序列 R 与待校准序列 S 都按真实时间单调排列；
* S 相对 R 可能缺失若干事件（R 中有、S 中没有）；
* S 中也可能插入了若干噪声事件（S 中有、R 中没有）；
* 时钟漂移不改变事件先后顺序，因此匹配结果必须单调
  （i 与 j 都严格递增）；
* 标签相同才允许匹配，时间关系一致的候选越可信。

朴素二维 DP 是 O(mn) 时间与空间。这里利用“只有标签相同才成候选”的稀疏性，
构造候选边集合，再用 Fenwick 树（BIT）求带权最长单调链（可正确处理同一标签
反复出现的情况），复杂度约 O(k log m)，k 为候选边数。

两遍策略（见 :func:`two_pass_match`）：

1. 每条候选边等权，求纯标签 LCS，稳健估计全局时钟偏移
   ``median(s_t - r_t)``；
2. 以“去掉偏移后的时间残差”为接近度重新加权匹配。匹配奖励远大于时间加分，
   保证匹配数量优先，时间项只用于消解相同标签的周期重复歧义。
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# 匹配奖励：每成一条锚点固定得分；时间接近加分上限远小于匹配奖励，
# 保证“多匹配正确标签”永远优先于“为了时间更近而少匹配”。
MATCH_REWARD = 10.0
MAX_TIME_BONUS = 1.0


@dataclass(frozen=True)
class MatchParams:
    """匹配参数。

    Attributes:
        time_pref_scale: 时间接近加分的尺度（秒）。第二遍匹配中，候选边相对
            估计偏移的残差为 0 时加 ``MAX_TIME_BONUS``，等于该尺度时衰减到 0。
    """

    time_pref_scale: float = 60.0


@dataclass(frozen=True)
class Event:
    """参与匹配的事件。"""

    index: int
    label: str
    t: float


class _FenwickMax:
    """前缀最大值查询 / 单点更新（Fenwick 树），位置从 0 开始。"""

    __slots__ = ("_n", "_val", "_choice")

    def __init__(self, n: int) -> None:
        self._n = n
        self._val: List[float] = [float("-inf")] * (n + 1)
        self._choice: List[Optional[int]] = [None] * (n + 1)

    def update(self, pos: int, value: float, choice: int) -> None:
        i = pos + 1  # 0-based -> 1-based
        while i <= self._n:
            if value > self._val[i]:
                self._val[i] = value
                self._choice[i] = choice
            i += i & -i

    def query(self, pos: int) -> Tuple[float, Optional[int]]:
        """返回区间 [0, pos] 的最大值及其写入时的候选编号。"""
        best = float("-inf")
        chosen: Optional[int] = None
        i = pos + 1
        while i > 0:
            if self._val[i] > best:
                best = self._val[i]
                chosen = self._choice[i]
            i -= i & -i
        return best, chosen


def _edge_weight(residual: float, scale: float) -> float:
    closeness = max(0.0, 1.0 - abs(residual) / scale)
    return MATCH_REWARD + MAX_TIME_BONUS * closeness


def monotone_match(
    ref: Sequence[Event],
    target: Sequence[Event],
    offset_hint: Optional[float] = None,
    time_pref_scale: float = 60.0,
) -> List[Tuple[int, int]]:
    """求总权重最高的单调匹配。

    Args:
        ref: 参考事件（按时间升序）。
        target: 待校准事件（按时间升序）。
        offset_hint: 已知时钟偏移估计 ``s_t - r_t``。为 ``None`` 时纯按标签
            匹配（每条候选等权，即标准 LCS）。
        time_pref_scale: 时间接近加分尺度（秒）。

    Returns:
        ``[(ref_index, target_index), ...]``，按两条序列顺序排列。
    """
    if not ref or not target:
        return []

    n = len(ref)
    by_label: dict[str, List[Event]] = defaultdict(list)
    for ev in ref:
        by_label[ev.label].append(ev)

    # 候选边：(s_index, r_index, residual)，标签相同才产生候选
    candidates: List[Tuple[int, int, float]] = []
    for s_ev in target:
        for r_ev in by_label.get(s_ev.label, ()):
            residual = 0.0
            if offset_hint is not None:
                residual = (s_ev.t - r_ev.t) - offset_hint
            candidates.append((s_ev.index, r_ev.index, residual))
    if not candidates:
        return []

    # 按 s 升序、r 降序排列：r 降序保证同一目标事件的候选不会自链
    # （同一 s 分组内先处理大 r，前缀查询 r-1 查不到本组其他边）。
    candidates.sort(key=lambda e: (e[0], -e[1]))

    bit = _FenwickMax(n)
    dp = [0.0] * len(candidates)
    parent: List[Optional[int]] = [None] * len(candidates)

    for c, (s_idx, r_idx, residual) in enumerate(candidates):
        weight = (
            MATCH_REWARD
            if offset_hint is None
            else _edge_weight(residual, time_pref_scale)
        )
        if r_idx > 0:
            base, pred = bit.query(r_idx - 1)
        else:
            base, pred = float("-inf"), None
        dp[c] = weight + (base if base != float("-inf") else 0.0)
        parent[c] = pred
        bit.update(r_idx, dp[c], c)

    end = max(range(len(candidates)), key=lambda c: dp[c])
    chain: List[Tuple[int, int]] = []
    cur: Optional[int] = end
    while cur is not None:
        s_idx, r_idx, _ = candidates[cur]
        chain.append((r_idx, s_idx))
        cur = parent[cur]
    chain.reverse()
    return chain


def estimate_offset(
    ref: Sequence[Event],
    target: Sequence[Event],
    pairs: Sequence[Tuple[int, int]],
) -> Optional[float]:
    """由匹配对稳健估计全局时钟偏移（秒）。"""
    if not pairs:
        return None
    return statistics.median(target[j].t - ref[i].t for i, j in pairs)


def two_pass_match(
    ref: Sequence[Event],
    target: Sequence[Event],
    params: Optional[MatchParams] = None,
) -> List[Tuple[int, int]]:
    """两遍匹配：纯标签 LCS -> 估计偏移 -> 带时间偏好的重匹配。"""
    params = params or MatchParams()
    first = monotone_match(ref, target)
    if len(first) < 2:
        return first  # 锚点太少，偏移估计无意义
    offset = estimate_offset(ref, target, first)
    second = monotone_match(
        ref, target, offset_hint=offset, time_pref_scale=params.time_pref_scale
    )
    # 第二遍不应比第一遍匹配更少（奖励恒定分量相同），保险起见取较长者
    return second if len(second) >= len(first) else first


def lcs_grid_match(
    ref: Sequence[Event],
    target: Sequence[Event],
) -> List[Tuple[int, int]]:
    """朴素 O(mn) 网格 LCS（无时间权重），仅供测试交叉验证。"""
    m, n = len(ref), len(target)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            best = max(dp[i - 1][j], dp[i][j - 1])
            if ref[i - 1].label == target[j - 1].label:
                best = max(best, dp[i - 1][j - 1] + 1)
            dp[i][j] = best
    pairs: List[Tuple[int, int]] = []
    i, j = m, n
    while i > 0 and j > 0:
        if dp[i][j] == dp[i - 1][j]:
            i -= 1
        elif dp[i][j] == dp[i][j - 1]:
            j -= 1
        else:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
    pairs.reverse()
    return pairs
