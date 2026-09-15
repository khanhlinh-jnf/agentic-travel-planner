"""FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import router as trips_router
from app.config import settings
from app.services.observability import langfuse_client

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app):
    yield
    client = langfuse_client()
    if client:
        try:
            await asyncio.to_thread(client.flush)
        except Exception as exc:
            logging.getLogger("travel.telemetry").warning(
                "langfuse_flush_failed error_type=%s", type(exc).__name__
            )

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Swarm travel planner with LangGraph, HITL, SerpApi MCP, and Booking MCP.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(trips_router)


@app.get("/", tags=["system"])
async def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
