"""Small, secret-safe logging foundation for web and worker processes."""

import logging

DEFAULT_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s %(message)s"
)


def configure_logging(level: str = "INFO") -> None:
    """Configure standard-library logging without request or secret payloads."""

    normalized_level = level.upper()
    numeric_level = getattr(logging, normalized_level, logging.INFO)
    logging.basicConfig(level=numeric_level, format=DEFAULT_LOG_FORMAT)