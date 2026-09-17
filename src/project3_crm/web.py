"""FastAPI application entry point."""

import logging

from fastapi import FastAPI, HTTPException, status

from project3_crm.config import get_settings
from project3_crm.db.database import check_database_readiness, get_database_engine
from project3_crm.logging_config import configure_logging
from project3_crm.webhooks import router as webhook_router

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="Project #3 CRM Integration", version="0.1.0")
app.include_router(webhook_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Report process liveness without contacting external systems."""

    return {"status": "ok"}


@app.get("/ready", tags=["system"])
def ready() -> dict[str, str]:
    """Report structural configuration and PostgreSQL readiness."""

    try:
        get_settings()
        check_database_readiness(get_database_engine())
    except Exception:
        logger.error("Service readiness check failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service not ready",
        ) from None
    return {"status": "ready"}
