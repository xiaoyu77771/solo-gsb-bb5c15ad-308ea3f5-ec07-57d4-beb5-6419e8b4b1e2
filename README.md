# 工业事件日志时钟校准 API

基于 **Python + FastAPI** 的事件日志时钟漂移校准服务。接收一份“参考时间”的事件日志
（JSONL）与一份存在时钟漂移、缺失事件、插入噪声事件的待校准日志（JSONL），自动完成：

1. **单调动态规划匹配**：以事件标签为键，按真实时间单调性求最长带权公共子序列，容忍
   缺失 / 插入事件；
2. **分段线性时钟映射拟合**：对匹配锚点 `(设备时间, 参考时间)` 做分段最小二乘拟合，
   刻画分段漂移率与跳变（如 NTP 校时、晶振频率变化）；
3. **校准输出**：每条待校准记录的校准时间戳、校准偏移、匹配置信度、所属映射段，
   以及未匹配记录（参考侧缺失 / 设备侧插入）。

交互式文档：服务启动后访问 <http://localhost:8000/docs>。

---

## 目录结构

```
app/
  main.py        FastAPI 路由：POST /align、POST /align/upload、GET /health
  pipeline.py    校准流水线：JSONL 解析 -> 匹配 -> 剪枝 -> 分段拟合 -> 输出
  matching.py    单调带权 LCS 匹配（Fenwick 树优化的稀疏 DP，两遍匹配）
  segments.py    分段线性最小二乘拟合与段间混合映射
  timestamps.py  时间戳解析/格式化（ISO-8601、Unix 秒、Unix 毫秒）
samples/         示例数据与生成脚本
tests/           单元测试与 API 测试（17 个）
Dockerfile
docker-compose.yml
```

## 一键启动（Docker Compose，端口 8000）

```bash
docker compose up --build -d
```

服务监听 **`8000`** 端口：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | <http://localhost:8000/health> |
| 校准接口（JSON 请求体） | `POST http://localhost:8000/align` |
| 校准接口（文件上传） | `POST http://localhost:8000/align/upload` |
| Swagger 交互文档 | <http://localhost:8000/docs> |
| OpenAPI Schema | <http://localhost:8000/openapi.json> |

停止：

```bash
docker compose down
```

## 本地直接运行（不使用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install pytest httpx
pytest -q
```

## 输入数据格式

JSONL（每行一个 JSON 对象），记录需按时间升序，默认字段：

* `ts`：时间戳。支持 ISO-8601 字符串（`...Z` 或带偏移）、Unix 秒数值、Unix 毫秒
  数值（绝对值 ≥ 10¹² 自动识别）；输出时保持记录原有形态；
* `label`：事件标签（字符串或整数），相同标签才允许匹配；
* 其余字段原样保留并随结果返回。

字段名可通过 `timestampField` / `labelField` 参数修改。

## 调用示例

### 1) JSON 请求体

```bash
curl -X POST http://localhost:8000/align \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "{\"ts\":\"2026-09-22T10:00:00Z\",\"label\":\"A\"}\n{\"ts\":\"2026-09-22T10:00:10Z\",\"label\":\"B\"}",
    "target":    "{\"ts\":\"2026-09-22T10:00:03Z\",\"label\":\"A\"}\n{\"ts\":\"2026-09-22T10:00:13Z\",\"label\":\"B\"}",
    "residualTol": 0.5
  }'
```

### 2) 上传 JSONL 文件（随仓库示例）

```bash
python samples/generate.py          # 生成 samples/reference.jsonl 与 samples/target.jsonl

curl -X POST http://localhost:8000/align/upload \
  -F "reference=@samples/reference.jsonl" \
  -F "target=@samples/target.jsonl" \
  -F "residual_tol=0.5"
```

### 3) 健康检查

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"event-log-clock-alignment","version":"1.0.0"}
```

### 请求参数（均可选）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `timestampField` | `ts` | 时间戳字段名 |
| `labelField` | `label` | 标签字段名 |
| `timePrefScale` | `60` 秒 | 时间接近加分尺度，用于消解周期重复标签的配对歧义 |
| `slopeClip` | `2.0` | 分段漂移率允许的最大倍数（下界取倒数 0.5） |
| `residualTol` | `0.5` 秒 | 分段拟合残差容差，超出则断开为新段 |
| `outlierZ` | `6` | 稳健锚点剪枝的 z 阈值（基于 MAD 稳健标准差） |
| `confidenceDecaySeconds` | `60` 秒 | 非锚点记录置信度随距最近锚点距离衰减的尺度 |

## 响应说明

```jsonc
{
  "matched": [                    // 匹配锚点明细
    {
      "ref_index": 0, "target_index": 0, "label": "POWER_ON",
      "ref_ts": "...", "target_ts": "...", "aligned_ts": "...",
      "offset_seconds": -4.86,   // 原始偏移 s_t - r_t
      "residual_seconds": 0.12,  // 分段映射预测与参考时间的残差
      "confidence": 0.97
    }
  ],
  "aligned_records": [           // 待校准日志逐条结果（含未匹配记录）
    {
      "index": 0, "matched": true, "label": "POWER_ON",
      "original_ts": "...", "aligned_ts": "...", "delta_seconds": 4.98,
      "confidence": 0.97, "ref_index": 0, "segment": 0,
      "record": { ... }          // 原始记录
    }
  ],
  "unmatched_ref":    [ ... ],   // 参考独有 = 待校准日志缺失的事件
  "unmatched_target": [ ... ],   // 待校准独有 = 插入的噪声事件
  "segments": [                  // 分段线性时钟映射 y = slope*x + intercept
    {
      "index": 0,
      "x0_ts": "...", "x1_ts": "...",
      "slope": 1.019, "intercept_seconds": -34225348.17,
      "anchor_count": 5, "rms_residual_seconds": 0.073
    }
  ],
  "metrics": {
    "ref_count": 12, "target_count": 13,
    "matched_anchors": 11, "pruned_anchors": 0,
    "missing_in_target": 1, "inserted_in_target": 2,
    "segment_count": 2,
    "max_abs_residual_seconds": 0.119,
    "rms_residual_seconds": 0.071,
    "mean_confidence": 0.936,
    "match_coverage": 0.846
  }
}
```

**置信度语义**（0–1）：

* 锚点记录：`exp(-0.5·(残差/容差)²)`，残差为 0 时为 1；落在段间跳变区时按
  两段混合权重折扣；
* 非锚点记录：由最近锚点距离的指数衰减给出（锚点跨度内封顶 0.8、跨度外封顶 0.5）；
* 完全没有共同标签时所有记录置信度为 0，时间戳原样返回。

## 算法说明

### 单调标签匹配（`app/matching.py`）

* 时钟漂移不改变事件先后顺序，因此匹配必须关于两条序列**严格单调**，这是一个
  LCS/序列对齐问题；
* 只有标签相同的 `(参考事件, 待校准事件)` 才成为候选边。朴素二维 DP 为 O(mn)；
  本实现把候选边按 `(s 升序, r 降序)` 排序后，用 Fenwick 树维护前缀最大值求带权
  最长单调链，复杂度约 **O(k·log m)**，k 为候选边数，可正确处理同一标签周期重复；
* **两遍匹配**：第一遍纯标签 LCS 得到稳健全局偏移（偏移中位数）；第二遍以
  “残差 = (s−r) − 偏移”的接近度给候选加权，消解大常数偏移下的重复标签配对歧义。
  固定匹配奖励（10）远大于时间加分上限（1），保证匹配数量永远优先。

### 稳健锚点剪枝（`app/pipeline.py`）

对全部锚点做一次斜率裁剪后的全局最小二乘，按残差的 **MAD 稳健标准差**识别离群配对
（错误标签、跨周期误配）。残差整体很紧凑时不剪枝，避免误删真实的漂移拐点；并设置
“至少保留 3 个 / 最多丢弃 40%”的保护。

### 分段线性拟合（`app/segments.py`）

* 从左到右贪心扩张当前段：纳入下一个锚点后，只要段内最大绝对残差 ≤ `residualTol`
  且拟合斜率落在 `[1/slopeClip, slopeClip]` 内就继续，否则断开；
* 无法用单一直线描述的时钟跳变自然落在两段之间；
* 段内查询用本段直线；段外按最近段外推；段间空隙按到两端的距离做逆距离加权混合，
  空隙很宽时只有跳变边界附近显著混合（该区域置信度随之降低）。

### 规模与限制

* 单份日志默认上限 50,000 行（`max_records`，可在 `AlignParams` 中调整）；
* 标签基数很大、候选边接近满笛卡尔积的极端场景下，候选数可能接近 m·n；
  典型工业事件标签集合有限，通常远低于此。

## 示例数据

`samples/generate.py` 构造的待校准日志包含：前段走快（斜率 1.02、+5 秒偏移）、
后段跳变后走慢（斜率 0.98、+12 秒偏移）、缺失一个 `PRESSURE_OK`、插入两个噪声
事件（`SENSOR_NOISE`、`MAINT_LOG`），以及 ±0.15 秒抖动。服务可恢复出约
1.019 / 0.981 的分段斜率，锚点 RMS 残差约 0.07 秒。
