"""Tests for safe structured logging and credential redaction (Reviewer Item 5)."""

import logging
from app.routes.v1.shared import log_safe_error, sanitize_exception_text


def test_sanitize_exception_text_redacts_database_credentials():
    raw_error = "OperationalError: (pymysql.err.OperationalError) (1045, \"Access denied for user 'clarity_user':password@tcp(127.0.0.1:3306)/clarity_db\")"
    url_error = "sqlalchemy.exc.DBAPIError: mysql+pymysql://root:super_secret_pw@db.internal.net:3306/clarity"

    sanitized = sanitize_exception_text(url_error)
    assert "super_secret_pw" not in sanitized
    assert "root:***@" in sanitized


def test_sanitize_exception_text_redacts_tokens_and_oracle_formulas():
    token_error = "ValueError: Invalid API token: token='ghp_abc123secret' during query"
    sanitized_token = sanitize_exception_text(token_error)
    assert "ghp_abc123secret" not in sanitized_token
    assert "token=***" in sanitized_token

    oracle_error = "Exception: ground_truth='y = 2*x + 1' failed validation"
    sanitized_oracle = sanitize_exception_text(oracle_error)
    assert "y = 2*x + 1" not in sanitized_oracle
    assert "ground_truth=***" in sanitized_oracle


def test_log_safe_error_formats_structured_output(caplog):
    caplog.set_level(logging.ERROR)
    logger = logging.getLogger("test_logger")

    exc = ValueError("Failed connection to postgresql://user:secretpass@host:5432/db")
    log_safe_error("test_context", exc, logger=logger)

    assert len(caplog.records) == 1
    log_record = caplog.records[0]
    assert "secretpass" not in log_record.message
    assert "user:***@" in log_record.message
    assert "test_context" in log_record.message
    assert "exception_type=ValueError" in log_record.message
