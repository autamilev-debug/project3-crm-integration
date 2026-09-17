"""Inbound webhook routing, authentication, and raw-event persistence."""

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError

from project3_crm.config import Settings, get_settings
from project3_crm.db.database import get_database_engine
from project3_crm.db.webhook_events import persist_webhook_event


logger = logging.getLogger(__name__)
router = APIRouter()

SUPPORTED_SOURCES = frozenset({"website", "linkedin", "partner"})
WEBSITE_SIGNATURE_PATTERN = re.compile(r"sha256=[0-9a-f]{64}")


def _authentication_failed() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication failed",
    )


def _invalid_request() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid request",
    )


async def _read_limited_body(request: Request, maximum_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum_bytes:
                raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE)
        body.extend(chunk)
    return bytes(body)


def _verify_website_authentication(
    request: Request,
    raw_body: bytes,
    settings: Settings,
) -> None:
    supplied_timestamp = request.headers.get("X-Webhook-Timestamp")
    supplied_signature = request.headers.get("X-Webhook-Signature")
    if (
        supplied_timestamp is None
        or not supplied_timestamp.isascii()
        or not supplied_timestamp.isdigit()
        or supplied_signature is None
        or WEBSITE_SIGNATURE_PATTERN.fullmatch(supplied_signature) is None
    ):
        raise _authentication_failed()

    try:
        timestamp = int(supplied_timestamp)
        outside_replay_window = (
            abs(time.time() - timestamp) > settings.website_hmac_max_skew_seconds
        )
    except (ValueError, OverflowError):
        raise _authentication_failed() from None

    if outside_replay_window:
        raise _authentication_failed()

    canonical_bytes = supplied_timestamp.encode("ascii") + b"." + raw_body
    expected_signature = "sha256=" + hmac.new(
        settings.website_hmac_secret.get_secret_value().encode("utf-8"),
        canonical_bytes,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise _authentication_failed()


def _verify_linkedin_authentication(request: Request, settings: Settings) -> None:
    authorization = request.headers.get("Authorization")
    if authorization is None or not authorization.startswith("Bearer "):
        raise _authentication_failed()

    supplied_token = authorization.removeprefix("Bearer ")
    if not supplied_token or supplied_token != supplied_token.strip():
        raise _authentication_failed()

    if not _credentials_match(
        settings.linkedin_bearer_token.get_secret_value(), supplied_token
    ):
        raise _authentication_failed()


def _credentials_match(configured: str, supplied: str) -> bool:
    """Compare exact credential bytes and fail closed on unsuitable text."""

    try:
        configured_bytes = configured.encode("utf-8")
        supplied_bytes = supplied.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(configured_bytes, supplied_bytes)


def _verify_partner_authentication(request: Request, settings: Settings) -> None:
    supplied_api_key = request.headers.get("X-API-Key")
    if supplied_api_key is None or not _credentials_match(
        settings.partner_api_key.get_secret_value(), supplied_api_key
    ):
        raise _authentication_failed()


def _authenticate_source(
    source: str,
    request: Request,
    raw_body: bytes,
    settings: Settings,
) -> None:
    if source == "website":
        _verify_website_authentication(request, raw_body, settings)
    elif source == "linkedin":
        _verify_linkedin_authentication(request, settings)
    else:
        _verify_partner_authentication(request, settings)


def _reject_nonstandard_json_constant(_: str) -> None:
    raise ValueError("Non-standard JSON constant")


def _parse_json_object(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(
            raw_body,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise _invalid_request() from None

    if not isinstance(payload, dict):
        raise _invalid_request()
    return payload


def _extract_event_id(source: str, payload: dict[str, Any]) -> str:
    if source == "website":
        event_id = payload.get("submission_id")
    elif source == "linkedin":
        event = payload.get("event")
        event_id = event.get("id") if isinstance(event, dict) else None
    else:
        event_id = payload.get("reference")

    if (
        not isinstance(event_id, str)
        or not 1 <= len(event_id) <= 128
        or event_id != event_id.strip()
    ):
        raise _invalid_request()
    return event_id


@router.post("/webhooks/{source}", status_code=status.HTTP_202_ACCEPTED)
async def receive_webhook(source: str, request: Request) -> dict[str, bool | str]:
    """Authenticate and durably store one provider-neutral raw event."""

    if source not in SUPPORTED_SOURCES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    settings = get_settings()
    raw_body = await _read_limited_body(request, settings.max_webhook_body_bytes)
    _authenticate_source(source, request, raw_body, settings)
    payload = _parse_json_object(raw_body)
    event_id = _extract_event_id(source, payload)

    try:
        with get_database_engine().begin() as connection:
            inserted = persist_webhook_event(
                connection,
                source=source,
                event_id=event_id,
                raw_payload=payload,
            )
    except SQLAlchemyError:
        logger.error(
            "Webhook persistence failed",
            extra={"source": source},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable",
        ) from None

    return {"status": "accepted", "duplicate": not inserted}
