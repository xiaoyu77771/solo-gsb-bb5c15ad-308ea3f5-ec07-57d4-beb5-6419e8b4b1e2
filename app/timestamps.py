"""时间戳解析与格式化。

支持三种输入形式：

* ISO-8601 字符串（``2026-09-22T10:00:00+08:00``），含 ``Z`` 后缀；
* 数值型秒（整数或浮点）；
* 数值型毫秒（>= 1e12 且 < 1e14 的数值，视为 Unix 毫秒）。

所有时间戳在内部统一为 Unix 秒（float），输出时尽量还原记录原本的形态
（数值还原数值、ISO 字符串还原为 ISO 字符串）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# 2001-09-09 ~ 2286-11-20（秒）之外的数值通常已经是毫秒量级
_MS_LOWER = 10**12
_MS_UPPER = 10**14

_ZERO_OFFSET = timedelta(0)


class TimestampError(ValueError):
    """时间戳无法解析。"""


@dataclass(frozen=True)
class Timestamp:
    """解析后的时间戳。

    Attributes:
        seconds: Unix 纪元秒（UTC 视角）。
        kind: ``iso`` / ``seconds`` / ``millis``。
        tz: 原始 ISO 字符串携带的时区；朴素字符串按 UTC 输出。
        as_millis_int: 原始数值是否为整型毫秒（决定输出是否保留整数形态）。
    """

    seconds: float
    kind: str
    tz: Optional[timezone] = None
    as_millis_int: bool = False

    def format(self) -> Any:
        """按记录原始形态输出校准后的时间戳。"""
        if self.kind == "iso":
            dt = datetime.fromtimestamp(self.seconds, tz=timezone.utc)
            if self.tz is not None:
                dt = dt.astimezone(self.tz)
            text = dt.isoformat()
            # UTC 统一用 Z 后缀，朴素（无时区）时间戳保持朴素
            if self.tz is None:
                text = text.replace("+00:00", "")
            elif self.tz.utcoffset(None) == _ZERO_OFFSET:
                text = text.replace("+00:00", "Z")
            return text
        if self.kind == "millis":
            value = self.seconds * 1000.0
            return int(round(value)) if self.as_millis_int else value
        value = self.seconds
        return int(value) if float(value).is_integer() else value


def parse_timestamp(value: Any) -> Timestamp:
    """把单条记录的时间戳字段解析为 :class:`Timestamp`。"""
    if isinstance(value, bool):  # bool 是 int 的子类，显式拒绝
        raise TimestampError(f"不支持的时间戳类型: {type(value).__name__}")

    if isinstance(value, (int, float)):
        number = float(value)
        if _MS_LOWER <= abs(number) < _MS_UPPER:
            return Timestamp(
                seconds=number / 1000.0,
                kind="millis",
                as_millis_int=isinstance(value, int),
            )
        return Timestamp(seconds=number, kind="seconds")

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise TimestampError("空字符串无法解析为时间戳")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            # 兼容纯数字字符串
            try:
                return parse_timestamp(float(text))
            except ValueError:
                raise TimestampError(f"无法解析时间戳字符串: {value!r}")
        tz: Optional[timezone] = None
        if dt.tzinfo is not None:
            offset = dt.utcoffset()
            tz = timezone(offset) if offset is not None else timezone.utc
        return Timestamp(seconds=dt.timestamp(), kind="iso", tz=tz)

    raise TimestampError(f"不支持的时间戳类型: {type(value).__name__}")
