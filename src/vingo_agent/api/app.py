"""FastAPI app nội bộ: chỉ backend `chat` gọi vào, không public."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from vingo_agent.api.routes import router
from vingo_agent.observability.logging import configure_logging
from vingo_agent.observability.tracing import configure_tracing


@asynccontextmanager
async def lifespan(app):
    provider = configure_tracing()
    try:
        yield
    finally:
        provider.shutdown()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(lifespan=lifespan, title="vingo-agent", version="0.1.0", docs_url=None, redoc_url=None)
    app.include_router(router)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        code = error.detail if isinstance(error.detail, str) else "VALIDATION_ERROR"
        return JSONResponse({"status": error.status_code, "code": code, "title": code}, status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse({"status": 422, "code": "VALIDATION_ERROR", "title": "VALIDATION_ERROR"}, status_code=422)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
