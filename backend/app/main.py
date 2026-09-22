"""
Agent Backend - FastAPI Application

Main entry point for the backend service.
"""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings, require_postgres_url
from app.api import router as api_router
from app.api.internal import router as internal_router
from app.api.webhook import router as webhook_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown events.

    require_postgres_url() runs here, not lazily on first pool use: every
    turn this service handles — linking, unlinking, dedupe — goes through
    PostgreSQL, so this is a core dependency, not a peripheral one. Deferring
    the check meant a misconfigured deployment reported /health as healthy
    and then lost every turn inside process_update's background task,
    silently, with no record it ever happened. Checking here instead makes
    uvicorn refuse to finish starting up — the pod never becomes ready — so
    the failure is loud and at deploy time, not per-turn in a log line no
    one is watching.
    """
    settings = get_settings()
    require_postgres_url()
    logger.info("%s v%s starting — prefix=%s debug=%s",
                settings.app_name, settings.app_version,
                settings.api_prefix, settings.debug)
    yield
    logger.info("%s shutting down", settings.app_name)


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()
    
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Agent - Backend API for Nekazari Platform",
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=f"{settings.api_prefix}/redoc",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        lifespan=lifespan,
    )
    
    # CORS Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Health check (at root for k8s probes)
    @app.get("/health")
    async def health_check():
        """Health check endpoint for Kubernetes probes."""
        return {
            "status": "healthy",
            "service": settings.app_name,
            "version": settings.app_version,
        }
    
    # Include API routes
    app.include_router(api_router, prefix=settings.api_prefix)

    # Internal routes (entity-manager, other in-cluster callers) — authenticated
    # by X-Internal-Service-Secret, NOT gateway headers. See app/api/internal.py.
    app.include_router(internal_router, prefix=settings.api_prefix)

    # Public channel webhook — the only route that does NOT arrive through the
    # api-gateway. Authenticated by a shared secret, not gateway headers. See
    # app/api/webhook.py.
    app.include_router(webhook_router, prefix=settings.api_prefix)

    return app


# Create application instance
app = create_app()
