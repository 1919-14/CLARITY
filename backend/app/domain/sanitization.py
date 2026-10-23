"""Sanitization and redaction utilities for connection strings, credentials, and sensitive metadata.

Design Principles:
------------------
1. Prevent credentials, secrets, tokens, and passwords from leaking into logs, audit events, or API responses.
2. Safe for recursive execution over arbitrary JSON-compatible payloads.
3. Decoupled from Flask/HTTP layers so domain services can safely use sanitization.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

_REDACTION_PATTERNS = [
    # Redact URI credentials: ://user:password@ -> ://user:***@
    (re.compile(r"://([^:@/\s]+):([^@/\s]+)@", re.IGNORECASE), r"://\1:***@"),
    # Redact password/token/key in query strings: ?password=secret or &token=secret
    (re.compile(r"([?&](?:password|token|api_key|secret)=)[^&\s]+", re.IGNORECASE), r"\1***"),
    # Redact password / secret assignments with quotes or whitespace: password='xyz' or password="xyz"
    (re.compile(r"""(password|passwd|secret|token|api_key|ground_truth)['"]?\s*[:=]\s*(?:'[^']*'|"[^"]*"|[^\s,;)]+)""", re.IGNORECASE), r"\1=***"),
    # Redact parameters / DSN key-value pairs: pwd=..., secret=...
    (re.compile(r"\b(pwd|password)\s*=\s*[^\s,)]+", re.IGNORECASE), r"\1=***"),
    # Redact raw Authorization header values: Authorization: Bearer abc123 or Authorization: Basic xyz
    (re.compile(r"""\b(Authorization\s*:\s*(?:Bearer|Basic|Token)?\s*)['"]?[a-zA-Z0-9_\-\.~+/=]+['"]?""", re.IGNORECASE), r"\1***"),
    # Redact raw Bearer tokens in free-form text: Bearer abc123 or 'Bearer abc123'
    (re.compile(r"""\b(Bearer\s+)['"]?[a-zA-Z0-9_\-\.~+/=]+['"]?""", re.IGNORECASE), r"\1***"),
    # Redact raw Basic auth credentials in free-form text: Basic dXNlcjpwYXNz
    (re.compile(r"""\b(Basic\s+)['"]?[a-zA-Z0-9+/=]{6,}['"]?""", re.IGNORECASE), r"\1***"),
]

SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(password|secret|token|api_key|apikey|auth|authorization|credential|private_key|cookie|pwd|ground_truth|bearer)"
)


def sanitize_exception_text(text: str) -> str:
    """Redact connection strings, credentials, and sensitive parameters from text/exceptions."""
    sanitized = str(text)
    for pattern, replacement in _REDACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def sanitize_reason_text(reason: Optional[str]) -> Optional[str]:
    """Redact credentials from a free-text explanation while keeping its wording.

    Researcher-supplied reasons are stored verbatim in history, so a pasted
    connection string or bearer token must not end up in the audit trail.
    """
    if reason is None:
        return None
    return sanitize_exception_text(str(reason))


def sanitize_metadata(obj: Any) -> Any:
    """Recursively scrub sensitive keys and credential patterns from metadata payloads."""
    if isinstance(obj, Mapping):
        sanitized = {}
        for k, v in obj.items():
            k_str = str(k)
            if SENSITIVE_KEY_PATTERN.search(k_str):
                sanitized[k_str] = "***REDACTED***"
            else:
                sanitized[k_str] = sanitize_metadata(v)
        return sanitized
    if isinstance(obj, (list, tuple)):
        return [sanitize_metadata(item) for item in obj]
    if isinstance(obj, str):
        return sanitize_exception_text(obj)
    return obj
