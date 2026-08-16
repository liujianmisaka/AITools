from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict

from multi_agent_v2.packages.observability.health import (
    HealthReport,
    HealthService,
    ReadinessStatus,
)

router = APIRouter()


class LivenessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = "ok"


def _health_service(request: Request) -> HealthService:
    return cast(HealthService, request.app.state.health_service)


@router.get("/live", response_model=LivenessResponse)
async def live() -> LivenessResponse:
    return LivenessResponse()


@router.get(
    "/ready",
    response_model=HealthReport,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthReport}},
)
async def ready(request: Request, response: Response) -> HealthReport:
    report = await _health_service(request).report()
    if report.status is ReadinessStatus.NOT_READY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report


@router.get("/health/components", response_model=HealthReport)
async def component_health(request: Request) -> HealthReport:
    return await _health_service(request).report()
