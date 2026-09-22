"""生成示例数据：参考日志 reference.jsonl 与待校准日志 target.jsonl。

待校准日志相对参考日志：
* 时钟分段漂移：前段 y = 1.02x + 5（走快），后段 y = 0.98x + 12（跳变后走慢）；
* 缺失一个 PRESSURE_OK 事件；
* 插入两个参考日志中没有的噪声事件（SENSOR_NOISE / MAINT_LOG）；
* 每条记录带 ±0.15 秒随机抖动。
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

T0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)

# (相对参考起点秒数, 标签)
REFERENCE = [
    (0, "POWER_ON"),
    (10, "VALVE_OPEN"),
    (25, "PRESSURE_OK"),   # 将在待校准日志中缺失
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

# 待校准侧插入的噪声事件：(设备时间相对秒, 标签)
INSERTED = [
    (80, "SENSOR_NOISE"),
    (250, "MAINT_LOG"),
]


def iso(t_seconds: float) -> str:
    dt = T0 + timedelta(seconds=t_seconds)
    return dt.isoformat().replace("+00:00", "Z")


def to_device(y: float) -> float:
    # 分段映射的逆映射：y = 1.02x + 5（前段）；y = 0.98x + 12（后段）
    if y <= 100:
        return (y - 5) / 1.02
    if y >= 140:
        return (y - 12) / 0.98
    # 100 < y < 140 为跳变区间，示例锚点不落入其中
    return (y - 5) / 1.02


def main() -> None:
    rng = random.Random(42)
    out_dir = Path(__file__).resolve().parent

    with (out_dir / "reference.jsonl").open("w", encoding="utf-8") as f:
        for y, label in REFERENCE:
            f.write(json.dumps({"ts": iso(y), "label": label, "line": "line-A"}) + "\n")

    rows = []
    for y, label in REFERENCE:
        if label == "PRESSURE_OK":
            continue  # 模拟事件缺失
        x = to_device(y) + rng.uniform(-0.15, 0.15)
        rows.append((x, label))
    for x, label in INSERTED:
        rows.append((x, label))
    rows.sort(key=lambda r: r[0])

    with (out_dir / "target.jsonl").open("w", encoding="utf-8") as f:
        for x, label in rows:
            f.write(json.dumps({"ts": iso(x), "label": label, "device": "plc-07"}) + "\n")

    print(f"wrote reference.jsonl ({len(REFERENCE)} rows) and target.jsonl ({len(rows)} rows)")


if __name__ == "__main__":
    main()
