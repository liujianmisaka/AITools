from fastapi import FastAPI, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from codex_sessions.models import SessionSummary
from codex_sessions.sessions import CodexSessionReadError, CodexSessionStore


def create_app(store: CodexSessionStore | None = None) -> FastAPI:
    app = FastAPI(
        title="Local Codex Sessions API",
        version="1.0.0",
        description="以只读方式查询本机 Codex 会话的名称和 ID。",
    )
    session_store = store or CodexSessionStore()

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/sessions",
        response_model=list[SessionSummary],
        tags=["sessions"],
        summary="查询本地 Codex 会话",
    )
    async def list_sessions(
        include_archived: bool = Query(default=True, description="是否包含已归档会话"),
    ) -> list[SessionSummary]:
        try:
            return await run_in_threadpool(session_store.list_sessions, include_archived)
        except CodexSessionReadError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return app


app = create_app()
