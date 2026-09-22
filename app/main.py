"""工业事件日志时钟校准 API。

端点：

* ``GET  /health``          健康检查
* ``GET  /``                服务信息
* ``POST /align``           JSON 请求体（reference / target 为 JSONL 文本）
* ``POST /align/upload``    multipart 上传两个 ``.jsonl`` 文件
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .pipeline import AlignParams, AlignmentError, PipelineResult, run_alignment

SERVICE_NAME = "event-log-clock-alignment"
SERVICE_VERSION = "1.0.0"

app = FastAPI(
    title="工业事件日志时钟校准 API",
    version=SERVICE_VERSION,
    description=(
        "接收参考事件日志 JSONL 与存在时钟漂移、缺失/插入事件的待校准 JSONL，"
        "基于事件标签做单调动态规划匹配，并按匹配锚点拟合分段线性时钟映射，"
        "输出校准时间戳、匹配置信度及未匹配记录。"
    ),
)


class AlignRequest(BaseModel):
    """/align 请求体。"""

    reference: str = Field(
        ...,
        description="参考事件日志，JSONL 文本（每行一个 JSON 对象，按时间升序）",
        min_length=1,
    )
    target: str = Field(
        ...,
        description="待校准事件日志，JSONL 文本（每行一个 JSON 对象，按时间升序）",
        min_length=1,
    )
    timestamp_field: str = Field(
        "ts", description="时间戳字段名", alias="timestampField"
    )
    label_field: str = Field("label", description="事件标签字段名", alias="labelField")
    time_pref_scale: float = Field(
        60.0,
        gt=0,
        description="时间接近加分尺度（秒），用于消解周期重复标签歧义",
        alias="timePrefScale",
    )
    slope_clip: float = Field(
        2.0,
        gt=1.0,
        le=1000.0,
        description="分段斜率允许的最大漂移倍数（取倒数为下界）",
        alias="slopeClip",
    )
    residual_tol: Optional[float] = Field(
        None,
        gt=0,
        description="分段拟合残差容差（秒），默认 0.5 秒",
        alias="residualTol",
    )
    outlier_z: float = Field(
        6.0,
        ge=2.0,
        le=20.0,
        description="稳健锚点剪枝的 z 阈值（基于 MAD 估计的稳健标准差）",
        alias="outlierZ",
    )
    confidence_decay_seconds: float = Field(
        60.0,
        gt=0,
        description="非锚点记录置信度随距最近锚点距离衰减的尺度（秒）",
        alias="confidenceDecaySeconds",
    )

    model_config = {"populate_by_name": True, "json_schema_extra": {
        "example": {
            "reference": '{"ts": "2026-09-22T10:00:00Z", "label": "A"}\n'
            '{"ts": "2026-09-22T10:00:10Z", "label": "B"}',
            "target": '{"ts": "2026-09-22T10:00:03Z", "label": "A"}\n'
            '{"ts": "2026-09-22T10:00:13Z", "label": "B"}',
        }
    }}


@app.exception_handler(AlignmentError)
async def alignment_error_handler(_request, exc: AlignmentError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "alignment_failed", "detail": str(exc)})


def _build_params(req: Any) -> AlignParams:
    return AlignParams(
        timestamp_field=req.timestamp_field,
        label_field=req.label_field,
        time_pref_scale=req.time_pref_scale,
        slope_clip=req.slope_clip,
        residual_tol=req.residual_tol,
        outlier_z=req.outlier_z,
        confidence_decay_seconds=req.confidence_decay_seconds,
    )


def _serialize(result: PipelineResult) -> Dict[str, Any]:
    payload = asdict(result)
    # 指标以更易读的键名扁平输出；dataclass 结构原样保留
    payload["metrics"] = result.metrics
    return payload


@app.post("/align", summary="校准待校准事件日志")
def align(req: AlignRequest) -> Dict[str, Any]:
    """执行单调匹配 + 分段线性时钟校准。"""
    params = _build_params(req)
    try:
        result = run_alignment(req.reference, req.target, params)
    except AlignmentError:
        raise
    except Exception as exc:  # pragma: no cover - 防御性兜底
        raise HTTPException(status_code=500, detail=f"校准过程发生内部错误: {exc}")
    return _serialize(result)


@app.post("/align/upload", summary="通过文件上传执行校准")
async def align_upload(
    reference: UploadFile = File(..., description="参考事件日志 .jsonl"),
    target: UploadFile = File(..., description="待校准事件日志 .jsonl"),
    timestamp_field: str = "ts",
    label_field: str = "label",
    time_pref_scale: float = 60.0,
    slope_clip: float = 2.0,
    residual_tol: Optional[float] = None,
    outlier_z: float = 6.0,
    confidence_decay_seconds: float = 60.0,
) -> Dict[str, Any]:
    """multipart/form-data 上传两个 JSONL 文件（参数以表单字段覆盖默认值）。"""

    async def _read(upload: UploadFile) -> str:
        data = await upload.read()
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise AlignmentError(f"文件 {upload.filename!r} 不是 UTF-8 文本")

    ref_text = await _read(reference)
    tgt_text = await _read(target)
    params = AlignParams(
        timestamp_field=timestamp_field,
        label_field=label_field,
        time_pref_scale=time_pref_scale,
        slope_clip=slope_clip,
        residual_tol=residual_tol,
        outlier_z=outlier_z,
        confidence_decay_seconds=confidence_decay_seconds,
    )
    return _serialize(run_alignment(ref_text, tgt_text, params))


@app.get("/health", summary="健康检查")
def health() -> Dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION}


@app.get("/", include_in_schema=False)
def root() -> Dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "docs": "/docs",
        "endpoints": ["/health", "/align", "/align/upload"],
    }
