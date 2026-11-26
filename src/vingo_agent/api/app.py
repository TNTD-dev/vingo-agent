"""FastAPI app nội bộ: chỉ backend `chat` gọi vào, không public."""

from fastapi import FastAPI

from vingo_agent.api.routes import router
from vingo_agent.observability.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="vingo-agent", version="0.1.0", docs_url=None, redoc_url=None)
    app.include_router(router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
