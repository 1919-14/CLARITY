"""Pure domain sanitization and secret-redaction utilities for CLARITY.

Redacts credentials, tokens, API keys, DSN passwords, and sensitive parameters
from metadata payloads, logs, and exception texts.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Sequence

_SENSITIVE_KEY_PATTERNS = [
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"api_key", re.IGNORECASE),
    re.compile(r"auth", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"bearer", re.IGNORECASE),
    re.compile(r"ground_truth", re.IGNORECASE),
]

_REDACTION_PATTERNS = [
    # Redact URI credentials: ://user:password@ -> ://user:***@
    (re.compile(r"://([^:@/\s]+):([^@/\s]+)@", re.IGNORECASE), r"://\1:***@"),
    # Redact password/token/key in query strings: ?password=secret or &token=secret
    (re.compile(r"([?&](?:password|token|api_key|secret)=)[^&\s]+", re.IGNORECASE), r"\1***"),
    # Redact password / secret assignments with quotes or whitespace: password='xyz'
    (re.compile(r"""(password|passwd|secret|token|api_key|ground_truth)['"]?\s*[:=]\s*(?:'[^']*'|"[^"]*"|[^\s,;)]+)""", re.IGNORECASE), r"\1=***"),
    # Redact parameters / DSN key-value pairs: pwd=..., password=...
    (re.compile(r"\b(pwd|password)\s*=\s*[^\s,)]+", re.IGNORECASE), r"\1=***"),
]


def _is_sensitive_key(key: str) -> bool:
    s = str(key)
    return any(p.search(s) for p in _SENSITIVE_KEY_PATTERNS)


def sanitize_metadata(data: Any) -> Any:
    """Recursively scrub sensitive keys and credential patterns from dictionaries and lists."""
    if isinstance(data, Mapping):
        sanitized: Dict[str, Any] = {}
        for k, v in data.items():
            key_str = str(k)
            if _is_sensitive_key(key_str):
                sanitized[key_str] = "***REDACTED***"
            else:
                sanitized[key_str] = sanitize_metadata(v)
        return sanitized
    elif isinstance(data, (list, tuple)):
        return [sanitize_metadata(item) for item in data]
    elif isinstance(data, str):
        return sanitize_exception_text(data)
    return data


def sanitize_exception_text(text: str) -> str:
    """Redact connection strings, credentials, and sensitive parameters from exception messages."""
    sanitized = str(text)
    for pattern, replacement in _REDACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized
