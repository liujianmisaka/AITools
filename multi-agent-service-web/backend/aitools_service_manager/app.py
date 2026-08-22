from __future__ import annotations

from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, HTTPException, Query
from misaka_service_runtime import ServiceManagerError, ServiceNotFound

from aitools_service_manager.client import ControlPlaneRequestError
from aitools_service_manager.models import (
    GroupActionView,
    ManagedServiceView,
    ManagementConfigurationView,
)
from aitools_service_manager.service import (
    GroupAction,
    GroupId,
    ManagementService,
    ManagementServiceError,
)


def create_app(service: ManagementService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        await service.start()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(
        title="AITools Service Manager",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok", "service": "aitools-service-manager"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ready", "service": "aitools-service-manager"}

    @app.get("/configuration", response_model=ManagementConfigurationView)
    async def configuration() -> ManagementConfigurationView:  # pyright: ignore[reportUnusedFunction]
        return service.configuration()

    @app.get("/services", response_model=list[ManagedServiceView])
    async def list_services() -> list[ManagedServiceView]:  # pyright: ignore[reportUnusedFunction]
        return await service.services()

    @app.get("/services/{service_id}", response_model=ManagedServiceView)
    async def get_service(service_id: str) -> ManagedServiceView:  # pyright: ignore[reportUnusedFunction]
        try:
            return await service.service(service_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/services/{service_id}/start", response_model=ManagedServiceView)
    async def start_service(  # pyright: ignore[reportUnusedFunction]
        service_id: str,
        epoch: int = Query(ge=0),
    ) -> ManagedServiceView:
        try:
            return await service.start_service(service_id, expected_epoch=epoch)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/services/{service_id}/stop", response_model=ManagedServiceView)
    async def stop_service(  # pyright: ignore[reportUnusedFunction]
        service_id: str,
        epoch: int = Query(ge=0),
    ) -> ManagedServiceView:
        try:
            return await service.stop_service(service_id, expected_epoch=epoch)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/groups/{group_id}/{action}", response_model=GroupActionView)
    async def change_group(  # pyright: ignore[reportUnusedFunction]
        group_id: str,
        action: str,
    ) -> GroupActionView:
        if group_id not in {"core", "all"} or action not in {"start", "stop"}:
            raise HTTPException(status_code=404, detail="unknown service group action")
        try:
            return await service.change_group(
                cast(GroupId, group_id),
                cast(GroupAction, action),
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    return app


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, ManagementServiceError):
        return HTTPException(status_code=error.status_code, detail=str(error))
    if isinstance(error, ServiceNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ServiceManagerError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, ControlPlaneRequestError):
        return HTTPException(status_code=error.status_code, detail=str(error))
    return HTTPException(status_code=500, detail=str(error))
