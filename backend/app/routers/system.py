from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import ai_gateway
from ..audio import tool_available
from ..auth import CurrentUser, require_admin
from ..config import get_settings
from ..db import get_db
from ..export import soffice_available
from ..processing import jobs_status
from ..schemas import HealthOut, SystemStatusOut

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthOut, responses={503: {"model": HealthOut}})
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degraded", "database": "error"})
    return HealthOut(status="ok", database="ok")


def _llm_status() -> dict:
    settings = get_settings()
    result = {"model": settings.llm_model or None, "endpoint_allowed": False, "reachable": False, "error": None}
    try:
        from ..llm import LLMClient, LLMError, check_endpoint  # AI-owned module, used read-only
    except Exception as exc:
        result["error"] = f"LLM-клиент недоступен: {type(exc).__name__}"
        return result
    try:
        check_endpoint(settings)
        result["endpoint_allowed"] = True
    except LLMError as exc:
        result["error"] = exc.message
        return result
    ok, err = LLMClient(settings).ping()
    result["reachable"], result["error"] = ok, err
    return result


@router.get("/system/status", response_model=SystemStatusOut)
def system_status(_: CurrentUser = Depends(require_admin)):
    s = get_settings()
    info = ai_gateway.voice_model_info()
    return SystemStatusOut(
        models=ai_gateway.models_status(),
        llm=_llm_status(),
        tools={"ffmpeg": tool_available("ffmpeg"), "ffprobe": tool_available("ffprobe"),
               "soffice": soffice_available()},
        voice={"model_id": info.model_id, "revision": info.revision, "dimension": info.dimension,
               "match_threshold": s.voice_match_threshold, "match_margin": s.voice_match_margin,
               "min_segment_seconds": s.voice_min_segment_seconds},
        jobs=jobs_status(),
    )
