"""Security utilities for credential redaction and sensitive data masking.

Guarantees that credentials, tokens, authorization headers, and cardholder data
are never exposed in effect ledgers, console outputs, JSON/HTML reports, or CI logs.
"""

import re
from typing import Any

# Common sensitive parameter names and headers
SENSITIVE_KEY_PATTERNS = re.compile(
    r"(?i)(api[_-]?key|auth|authorization|bearer|token|secret|password|"
    r"passwd|credential|private[_-]?key|card[_-]?number|pan|cvv|cvc|ssn)"
)

# Credit card number pattern (13 to 19 digits with optional hyphens/spaces)
CREDIT_CARD_REGEX = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

# JWT / Bearer token pattern
BEARER_TOKEN_REGEX = re.compile(r"(?i)\bBearer\s+[a-zA-Z0-9\-_.]+\b")

# API key patterns (e.g. sk_live_..., key-..., etc.)
API_KEY_REGEX = re.compile(r"\b(?:sk|pk|ak|api|key)[_-](?:live|test|prod)?[_-]?[a-zA-Z0-9_\-.]{8,}\b", re.IGNORECASE)

# Inline credential assignment pattern (e.g. secret=..., api_key: ...)
SECRET_ASSIGN_REGEX = re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?([^\s,'\"]+)")

REDACTED_STR = "[REDACTED]"


def is_sensitive_key(key: str) -> bool:
    """Check if a dictionary key name matches sensitive naming patterns."""
    return bool(SENSITIVE_KEY_PATTERNS.search(key))


def mask_string_secrets(value: str) -> str:
    """Mask embedded credit card numbers, tokens, API keys, and credentials within a string."""
    masked = BEARER_TOKEN_REGEX.sub("Bearer [REDACTED_TOKEN]", value)
    masked = CREDIT_CARD_REGEX.sub("[REDACTED_CARD]", masked)
    masked = API_KEY_REGEX.sub("[REDACTED_KEY]", masked)
    masked = SECRET_ASSIGN_REGEX.sub(r"\1=[REDACTED_SECRET]", masked)
    return masked


def redact_sensitive_data(data: Any) -> Any:
    """Recursively redact sensitive keys and string patterns from arbitrary data structures.

    Handles dicts, lists, tuples, and primitive strings without modifying non-sensitive state.
    """
    if isinstance(data, dict):
        sanitized: dict[str, Any] = {}
        for k, v in data.items():
            str_key = str(k)
            if is_sensitive_key(str_key):
                sanitized[str_key] = REDACTED_STR
            else:
                sanitized[str_key] = redact_sensitive_data(v)
        return sanitized

    if isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]

    if isinstance(data, tuple):
        return tuple(redact_sensitive_data(item) for item in data)

    if isinstance(data, str):
        return mask_string_secrets(data)

    return data
