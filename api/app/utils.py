"""Shared small helpers: logging + postgres-safe string sanitation."""

import logging
import re

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def setup_logger(name: str = "custom_chat") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def sanitize_string(value: str) -> str:
    """Strip NUL bytes and UTF-16 surrogates (copied from Onyx)."""
    return _SURROGATE_RE.sub("", value.replace("\x00", ""))
