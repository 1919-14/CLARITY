"""Tests for the Nodus Compute execution adapter (Issue #12).

All tests run under ``ExecutionMode.MOCK`` or with mocked SDK boundaries.
No test may reach the real Nodus platform.

Coverage areas
--------------
1. Contract validation — NodusTask and NodusResult construction.
2. Payload validation — prohibited keys, non-serializable types.
3. Config loading — mode guard, missing credentials, env parsing.
4. Adapter execution — mock driver, local driver, retry logic.
5. Error sanitization — sdk_error → safe dict, result_error → safe dict.
6. Remote-mode failure — mocked SDK boundary, timeout and SDK error paths.
7. Mode guard in smoke test — local and mock mode rejection.
8. Secrets never leaked — canary test for message and details sanitization.
"""

from __future__ import annotations

import importlib
import sys
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.adapters.nodus.config import NodusConfig
from app.adapters.nodus.contracts import ExecutionMode, NodusResult, NodusTask, validate_json_payload
from app.adapters.nodus.errors import (
    NodusAdapterError,
    NodusConfigurationError,
    NodusErrorCode,
    NodusExecutionError,
    NodusResultError,
    NodusSmokeTestError,
    sanitize_result_error,
    sanitize_sdk_error,
)
from app.adapters.nodus.adapter import NodusExecutionAdapter, _make_failed_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(mode: str = "mock", api_key: str | None = None) -> NodusConfig:
    """Build a minimal NodusConfig for tests."""
    return NodusConfig(
        execution_mode=ExecutionMode.from_str(mode),
        app_name="test-app",
        function_name="test-fn",
        api_key=api_key or ("test-key" if mode == "nodus_remote" else None),
        timeout_seconds=5.0,
        max_retries=2,
    )


def _make_task(operation: str = "test_op", payload: dict | None = None) -> NodusTask:
    return NodusTask(
        task_id=f"t-{uuid.uuid4().hex[:8]}",
        operation=operation,
        payload=payload or {"input": 1},
        max_retries=0,
    )


# ===========================================================================
# 1. Contract validation
# ===========================================================================

class TestNodusTask:
    def test_valid_task_creates_successfully(self):
        task = _make_task()
        assert task.operation == "test_op"
        assert task.payload == {"input": 1}

    def test_empty_task_id_raises(self):
        with pytest.raises(ValueError, match="task_id"):
            NodusTask(task_id="", operation="op", payload={"x": 1})

    def test_empty_operation_raises(self):
        with pytest.raises(ValueError, match="operation"):
            NodusTask(task_id="t1", operation="", payload={"x": 1})

    def test_negative_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout_seconds"):
            NodusTask(task_id="t1", operation="op", payload={"x": 1}, timeout_seconds=-1)

    def test_negative_max_retries_raises(self):
        with pytest.raises(ValueError, match="max_retries"):
            NodusTask(task_id="t1", operation="op", payload={"x": 1}, max_retries=-1)

    def test_to_dict_round_trip(self):
        task = _make_task()
        d = task.to_dict()
        assert d["task_id"] == task.task_id
        assert d["operation"] == task.operation
        assert d["payload"] == task.payload


class TestNodusResult:
    def test_valid_succeeded_result(self):
        r = NodusResult(
            task_id="t1",
            execution_mode="mock",
            status="succeeded",
            result_data={"answer": 42},
        )
        assert r.is_success
        assert r.result_data == {"answer": 42}

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError, match="status"):
            NodusResult(task_id="t1", execution_mode="mock", status="unknown")

    def test_is_success_false_for_failed(self):
        r = NodusResult(task_id="t1", execution_mode="mock", status="failed")
        assert not r.is_success

    def test_to_dict_contains_all_fields(self):
        now = datetime.now(timezone.utc)
        r = NodusResult(
            task_id="t1",
            execution_mode="nodus_remote",
            status="succeeded",
            result_data={"v": 1},
            provider_task_id="prov-123",
            submitted_at=now,
            completed_at=now,
            duration_seconds=0.5,
        )
        d = r.to_dict()
        assert d["provider_task_id"] == "prov-123"
        assert d["duration_seconds"] == 0.5


# ===========================================================================
# 2. Payload validation
# ===========================================================================

class TestValidateJsonPayload:
    def test_valid_nested_dict_passes(self):
        validate_json_payload({"a": 1, "b": [True, None, "x"], "c": {"d": 2.5}})

    def test_non_dict_raises_type_error(self):
        with pytest.raises(TypeError, match="must be a dict"):
            validate_json_payload([1, 2, 3])

    def test_callable_value_raises(self):
        with pytest.raises(TypeError):
            validate_json_payload({"fn": lambda: None})

    def test_prohibited_key_secret_key_raises(self):
        with pytest.raises(ValueError, match="prohibited"):
            validate_json_payload({"secret_key": "abc"})

    def test_prohibited_key_api_key_raises(self):
        with pytest.raises(ValueError, match="prohibited"):
            validate_json_payload({"api_key": "xyz"})

    def test_prohibited_key_ground_truth_raises(self):
        with pytest.raises(ValueError, match="prohibited"):
            validate_json_payload({"ground_truth": [1, 2]})

    def test_prohibited_key_case_insensitive(self):
        with pytest.raises(ValueError, match="prohibited"):
            validate_json_payload({"Secret_Key": "val"})

    def test_non_string_dict_key_raises(self):
        with pytest.raises(TypeError, match="keys must be strings"):
            validate_json_payload({1: "bad"})

    def test_empty_dict_passes(self):
        validate_json_payload({})


# ===========================================================================
# 3. Config loading
# ===========================================================================

class TestNodusConfig:
    def test_from_environment_missing_mode_raises(self, monkeypatch):
        monkeypatch.delenv("NODUS_EXECUTION_MODE", raising=False)
        with pytest.raises(NodusConfigurationError) as exc_info:
            NodusConfig.from_environment()
        assert exc_info.value.code == NodusErrorCode.INVALID_MODE

    def test_from_environment_invalid_mode_raises(self, monkeypatch):
        monkeypatch.setenv("NODUS_EXECUTION_MODE", "invalid_mode")
        with pytest.raises(NodusConfigurationError) as exc_info:
            NodusConfig.from_environment()
        assert exc_info.value.code == NodusErrorCode.INVALID_MODE

    def test_remote_mode_requires_api_key(self, monkeypatch):
        monkeypatch.setenv("NODUS_EXECUTION_MODE", "nodus_remote")
        monkeypatch.setenv("NODUS_APP_NAME", "app")
        monkeypatch.setenv("NODUS_FUNCTION_NAME", "fn")
        monkeypatch.delenv("NODUS_API_KEY", raising=False)
        with pytest.raises(NodusConfigurationError) as exc_info:
            NodusConfig.from_environment()
        assert exc_info.value.code == NodusErrorCode.MISSING_CREDENTIALS

    def test_mock_mode_without_api_key_succeeds(self, monkeypatch):
        monkeypatch.setenv("NODUS_EXECUTION_MODE", "mock")
        monkeypatch.setenv("NODUS_APP_NAME", "app")
        monkeypatch.setenv("NODUS_FUNCTION_NAME", "fn")
        monkeypatch.delenv("NODUS_API_KEY", raising=False)
        config = NodusConfig.from_environment()
        assert config.execution_mode == ExecutionMode.MOCK
        assert config.api_key is None

    def test_repr_hides_api_key(self):
        config = _make_config(mode="nodus_remote", api_key="super-secret-key")
        assert "super-secret-key" not in repr(config)
        assert "<redacted>" in repr(config)

    def test_invalid_timeout_raises(self, monkeypatch):
        monkeypatch.setenv("NODUS_EXECUTION_MODE", "mock")
        monkeypatch.setenv("NODUS_APP_NAME", "app")
        monkeypatch.setenv("NODUS_FUNCTION_NAME", "fn")
        monkeypatch.setenv("NODUS_TIMEOUT_SECONDS", "notanumber")
        with pytest.raises(NodusConfigurationError):
            NodusConfig.from_environment()

    def test_negative_timeout_raises(self):
        with pytest.raises(NodusConfigurationError):
            NodusConfig(
                execution_mode=ExecutionMode.MOCK,
                app_name="app",
                function_name="fn",
                api_key=None,
                timeout_seconds=-5.0,
                max_retries=0,
            )

    def test_missing_app_name_raises(self):
        with pytest.raises(NodusConfigurationError):
            NodusConfig(
                execution_mode=ExecutionMode.MOCK,
                app_name="",
                function_name="fn",
                api_key=None,
                timeout_seconds=5.0,
                max_retries=0,
            )


# ===========================================================================
# 4. Adapter execution — mock and local drivers
# ===========================================================================

class TestMockDriver:
    def test_mock_returns_succeeded(self):
        adapter = NodusExecutionAdapter(_make_config("mock"))
        task = _make_task(payload={"v": 99})
        result = adapter.execute(task)
        assert result.status == "succeeded"
        assert result.execution_mode == "mock"
        assert result.result_data is not None
        assert result.result_data["mock"] is True

    def test_mock_echoes_payload(self):
        adapter = NodusExecutionAdapter(_make_config("mock"))
        task = _make_task(payload={"answer": 42})
        result = adapter.execute(task)
        assert result.result_data["echo_payload"] == {"answer": 42}

    def test_mock_result_has_mock_marker_in_metadata(self):
        adapter = NodusExecutionAdapter(_make_config("mock"))
        result = adapter.execute(_make_task())
        assert result.provider_metadata.get("mock") is True

    def test_mock_provider_task_id_is_none(self):
        adapter = NodusExecutionAdapter(_make_config("mock"))
        result = adapter.execute(_make_task())
        assert result.provider_task_id is None


class TestLocalDriver:
    def test_local_handler_called_and_result_returned(self):
        def handler(payload: dict) -> dict:
            return {"doubled": payload["x"] * 2}

        adapter = NodusExecutionAdapter(
            _make_config("local"),
            local_handlers={"double": handler},
        )
        task = _make_task(operation="double", payload={"x": 5})
        result = adapter.execute(task)
        assert result.status == "succeeded"
        assert result.result_data == {"doubled": 10}
        assert result.execution_mode == "local"

    def test_missing_handler_raises_config_error(self):
        adapter = NodusExecutionAdapter(_make_config("local"))
        task = _make_task(operation="no_such_op")
        with pytest.raises(NodusConfigurationError) as exc_info:
            adapter.execute(task)
        assert "no_such_op" in str(exc_info.value)

    def test_handler_exception_returns_failed_result(self):
        def bad_handler(payload: dict) -> dict:
            raise RuntimeError("internal error")

        adapter = NodusExecutionAdapter(
            _make_config("local"),
            local_handlers={"crash": bad_handler},
        )
        task = _make_task(operation="crash", payload={"x": 1})
        result = adapter.execute(task)
        assert result.status == "failed"
        assert result.error is not None
        assert result.error["context"]["exception_type"] == "RuntimeError"

    def test_handler_returning_non_dict_returns_failed(self):
        def bad_handler(payload: dict):
            return "not a dict"

        adapter = NodusExecutionAdapter(
            _make_config("local"),
            local_handlers={"bad": bad_handler},
        )
        result = adapter.execute(_make_task(operation="bad"))
        assert result.status == "failed"
        assert result.error["code"] == NodusErrorCode.UNEXPECTED_RESULT_TYPE


# ===========================================================================
# 5. Error sanitization
# ===========================================================================

class TestSanitizeSDKError:
    def test_timeout_exception_maps_to_timeout_code(self):
        TimeoutError_ = type("TimeoutError", (Exception,), {})
        exc = TimeoutError_("timeout error")
        result = sanitize_sdk_error(exc)
        assert result["code"] == NodusErrorCode.TIMEOUT

    def test_ratelimit_exception_maps_correctly(self):
        exc = type("RateLimitError", (Exception,), {})(  )
        result = sanitize_sdk_error(exc)
        assert result["code"] == NodusErrorCode.RATE_LIMITED

    def test_generic_exception_maps_to_sdk_error(self):
        exc = ValueError("some sdk message")
        result = sanitize_sdk_error(exc)
        assert result["code"] == NodusErrorCode.SDK_ERROR
        assert "some sdk message" not in result["message"]
        assert "some sdk message" not in str(result.get("context", {}))

    def test_sanitized_message_does_not_contain_raw_exc_text(self):
        exc = RuntimeError("CANARY_SECRET_DO_NOT_FORWARD")
        result = sanitize_sdk_error(exc)
        assert "CANARY_SECRET_DO_NOT_FORWARD" not in result["message"]
        assert "CANARY_SECRET_DO_NOT_FORWARD" not in str(result["context"])


class TestSanitizeResultError:
    def test_sensitive_string_is_replaced(self):
        result = sanitize_result_error("api_key=abc123 secret details here")
        assert "abc123" not in result["message"]
        assert result.get("context", {}).get("sanitized") is True

    def test_safe_string_is_truncated_and_forwarded(self):
        result = sanitize_result_error("computation failed: step 3 diverged")
        assert result["code"] == NodusErrorCode.INVALID_RESULT
        # Safe text is included in context
        assert "computation failed" in result["context"].get("provider_summary", "")

    def test_non_string_error_records_type(self):
        result = sanitize_result_error({"nested": "dict"})
        assert result["context"]["error_type"] == "dict"

    def test_canary_in_message_is_never_forwarded(self):
        canary = "CANARY_SECRET_XYZ_DO_NOT_FORWARD"
        result = sanitize_result_error(f"token={canary}")
        assert canary not in result["message"]
        assert canary not in str(result.get("context", {}))

    def test_canary_in_details_is_never_forwarded(self):
        """Regression: secret embedded in a non-string observation error."""
        canary = "CANARY_SECRET_ABC_DO_NOT_FORWARD"
        # Simulate a non-string error value with canary embedded in repr
        class EvilError:
            def __repr__(self):
                return f"EvilError(secret={canary})"

        result = sanitize_result_error(EvilError())
        # Only type name is recorded, not repr
        assert canary not in str(result)
        assert result["context"]["error_type"] == "EvilError"


# ===========================================================================
# 6. Remote-mode failure — mocked SDK boundary
# ===========================================================================

def _build_nodus_mock_module(fn_return_value: dict | None = None) -> types.ModuleType:
    """Build a minimal mock ``nodus`` package hierarchy for unit tests.

    Models the verified nodus-compute==1.0.27 public API:
    - ``nodus.Function.from_name(app: str, name: str, project=None)``
    - ``fn.spawn(**kwargs) -> FunctionCall handle``
    - ``handle.get(timeout=...) -> Any``  (returns function return value directly)
    - ``handle.name: str``  (provider FunctionCall identifier)
    - ``nodus.Client(api_key=..., api_url=..., project=...)``
    - ``nodus.errors.NodusError.retryable: bool``
    """
    if fn_return_value is None:
        fn_return_value = {"echo_value": "ok"}  # direct function return, NOT envelope

    nodus_mod = types.ModuleType("nodus")
    errors_mod = types.ModuleType("nodus.errors")

    # --------------- Error classes (verified from nodus.errors) ----------------
    class NodusError(Exception):
        code = "Unknown"
        retryable = False

    class FunctionCallFailed(NodusError):
        code = "FunctionCallFailed"
        retryable = False
        def __init__(self, call="", *, reason=None, message=""):
            super().__init__(message)
            self.call = call
            self.reason = reason

    class TooManyRequests(NodusError):
        code = "TooManyRequests"
        retryable = True  # documented retryable

    class Unavailable(NodusError):
        code = "Unavailable"
        retryable = True  # documented retryable

    class APITimeoutError(NodusError):
        code = "APITimeoutError"
        retryable = False

    class NotFound(NodusError):
        code = "NotFound"
        retryable = False

    errors_mod.NodusError = NodusError
    errors_mod.FunctionCallFailed = FunctionCallFailed
    errors_mod.TooManyRequests = TooManyRequests
    errors_mod.Unavailable = Unavailable
    errors_mod.APITimeoutError = APITimeoutError
    errors_mod.NotFound = NotFound
    nodus_mod.errors = errors_mod

    # --------------- FunctionCall handle stub ----------------------------------
    _captured_return = fn_return_value

    class _FunctionCallStub:
        """Mimics nodus.FunctionCall: name property + async get()."""
        def __init__(self):
            self._name = "prov-abc"

        @property
        def name(self) -> str:
            return self._name

        async def get(self, timeout=None):
            return _captured_return

    # --------------- Function stub ---------------------------------------------
    class _FunctionStub:
        """Mimics the object returned by nodus.Function.from_name."""
        @classmethod
        def from_name(cls, app: str, name: str, project=None):
            """Verified public API: positional (app, name, project)."""
            return cls()

        async def spawn(self, **kwargs):
            return _FunctionCallStub()

    # Client context manager stub
    class _ClientStub:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    nodus_mod.Client = _ClientStub
    nodus_mod.Function = _FunctionStub

    sys.modules["nodus"] = nodus_mod
    sys.modules["nodus.errors"] = errors_mod

    return nodus_mod


class TestRemoteDriver:
    """Tests that mock the SDK boundary to avoid real network calls.

    All mocks model the verified nodus-compute==1.0.27 public API.
    """

    def setup_method(self):
        self._nodus = _build_nodus_mock_module()

    def teardown_method(self):
        for key in ["nodus", "nodus.errors"]:
            sys.modules.pop(key, None)

    def test_remote_success_returns_succeeded_result(self):
        """get() returns the function return value directly; no envelope."""
        config = _make_config("nodus_remote", api_key="test-key")
        adapter = NodusExecutionAdapter(config)
        task = _make_task(payload={"echo_value": "clarity-smoke-test-v1"})
        result = adapter.execute(task)
        assert result.status == "succeeded"
        assert result.execution_mode == "nodus_remote"
        assert result.provider_task_id == "prov-abc"  # from handle.name
        # result_data IS the function return value, not an inner .result field
        assert result.result_data == {"echo_value": "ok"}

    def test_remote_function_return_is_used_as_result_data(self):
        """Verify get() return value is treated as result_data, not parsed as envelope."""
        # If the adapter were still treating return value as an envelope it would
        # look for a 'result' key and find nothing, failing. This confirms the fix.
        _build_nodus_mock_module(fn_return_value={"computed": 42, "steps": 3})
        config = _make_config("nodus_remote", api_key="test-key")
        adapter = NodusExecutionAdapter(config)
        result = adapter.execute(_make_task())
        assert result.status == "succeeded"
        assert result.result_data == {"computed": 42, "steps": 3}

    def test_remote_api_timeout_returns_timed_out_result(self):
        """nodus.errors.APITimeoutError maps to timed_out status."""
        import nodus.errors as nodus_errors

        config = _make_config("nodus_remote", api_key="test-key")

        original_fn = sys.modules["nodus"].Function

        class _TimeoutFunctionStub:
            @classmethod
            def from_name(cls, app, name, project=None):
                return cls()

            async def spawn(self, **kwargs):
                return _TimeoutHandleStub()

        class _TimeoutHandleStub:
            name = "prov-timeout"
            async def get(self, timeout=None):
                raise nodus_errors.APITimeoutError()

        sys.modules["nodus"].Function = _TimeoutFunctionStub

        try:
            adapter = NodusExecutionAdapter(config)
            result = adapter.execute(_make_task())
        finally:
            sys.modules["nodus"].Function = original_fn

        assert result.status == "timed_out"
        assert result.error["code"] == NodusErrorCode.TIMEOUT

    def test_function_call_failed_is_terminal(self):
        """FunctionCallFailed (deadline, OOM, crash) maps to failed status, no retry."""
        import nodus.errors as nodus_errors

        config = NodusConfig(
            execution_mode=ExecutionMode.REMOTE,
            app_name="app",
            function_name="fn",
            api_key="test-key",
            timeout_seconds=5.0,
            max_retries=3,  # must not retry despite max_retries=3
        )

        spawn_count = 0

        class _FailedFunctionStub:
            @classmethod
            def from_name(cls, app, name, project=None):
                return cls()

            async def spawn(self, **kwargs):
                nonlocal spawn_count
                spawn_count += 1
                return _FailedHandleStub()

        class _FailedHandleStub:
            name = "prov-failed"
            async def get(self, timeout=None):
                raise nodus_errors.FunctionCallFailed(call="prov-failed", reason="DeadlineExceeded")

        sys.modules["nodus"].Function = _FailedFunctionStub

        adapter = NodusExecutionAdapter(config)
        task = NodusTask(task_id="fc-t", operation="op", payload={"x": 1}, max_retries=3)
        result = adapter.execute(task)

        assert result.status == "failed"
        assert result.error["code"] == NodusErrorCode.REMOTE_EXECUTION_FAILED
        assert spawn_count == 1  # submitted once only; get() not retried

    def test_remote_sdk_error_is_sanitized(self):
        """Raw SDK exception text must not appear in the result error."""
        class _SanitizeHandleStub:
            name = "prov-san"
            async def get(self, timeout=None):
                raise ValueError("CANARY_SDK_SECRET_DO_NOT_FORWARD")

        class _SanitizeFunctionStub:
            @classmethod
            def from_name(cls, app, name, project=None):
                return cls()
            async def spawn(self, **kwargs):
                return _SanitizeHandleStub()

        sys.modules["nodus"].Function = _SanitizeFunctionStub

        adapter = NodusExecutionAdapter(NodusConfig(
            execution_mode=ExecutionMode.REMOTE,
            app_name="app",
            function_name="fn",
            api_key="test-key",
            timeout_seconds=5.0,
            max_retries=0,
        ))
        result = adapter.execute(_make_task())
        assert result.status == "failed"
        result_str = str(result.to_dict())
        assert "CANARY_SDK_SECRET_DO_NOT_FORWARD" not in result_str

    def test_remote_non_dict_result_is_failed(self):
        """If the function returns a non-dict, the adapter returns a failed result."""
        _build_nodus_mock_module(fn_return_value="not-a-dict")
        config = NodusConfig(
            execution_mode=ExecutionMode.REMOTE,
            app_name="app",
            function_name="fn",
            api_key="test-key",
            timeout_seconds=5.0,
            max_retries=0,
        )
        adapter = NodusExecutionAdapter(config)
        result = adapter.execute(_make_task())
        assert result.status == "failed"
        assert result.error is not None

    def test_function_from_name_uses_public_api(self):
        """Verify from_name is called with (app, name, project) positional args."""
        calls = []

        class _SpyFunctionStub:
            @classmethod
            def from_name(cls, app, name, project=None):
                calls.append({"app": app, "name": name, "project": project})
                return cls()
            async def spawn(self, **kwargs):
                return _SpyHandle()

        class _SpyHandle:
            name = "prov-spy"
            async def get(self, timeout=None):
                return {"result": "ok"}

        sys.modules["nodus"].Function = _SpyFunctionStub

        config = NodusConfig(
            execution_mode=ExecutionMode.REMOTE,
            app_name="my-app",
            function_name="my-fn",
            api_key="key",
            timeout_seconds=5.0,
            max_retries=0,
        )
        adapter = NodusExecutionAdapter(config)
        adapter.execute(_make_task())

        assert len(calls) == 1
        assert calls[0]["app"] == "my-app"
        assert calls[0]["name"] == "my-fn"


# ===========================================================================
# 7. Retry logic — pre-submission only
# ===========================================================================

class TestRetryLogic:
    """Retry tests targeting the pre-submission (spawn) phase only.

    Post-submission errors (from get()) must never trigger retries because
    the task is already submitted and re-spawning would create duplicates.
    """

    def setup_method(self):
        _build_nodus_mock_module()

    def teardown_method(self):
        for key in ["nodus", "nodus.errors"]:
            sys.modules.pop(key, None)

    def test_retryable_sdk_error_on_spawn_is_retried(self):
        """NodusError with retryable=True on spawn is retried up to max_retries."""
        import nodus.errors as nodus_errors

        spawn_count = 0

        class _RetryFunctionStub:
            @classmethod
            def from_name(cls, app, name, project=None):
                return cls()

            async def spawn(self, **kwargs):
                nonlocal spawn_count
                spawn_count += 1
                if spawn_count <= 2:
                    raise nodus_errors.TooManyRequests("rate limited")
                return _RetryHandle()

        class _RetryHandle:
            name = "prov-retry"
            async def get(self, timeout=None):
                return {"v": 1}

        sys.modules["nodus"].Function = _RetryFunctionStub

        config = NodusConfig(
            execution_mode=ExecutionMode.REMOTE,
            app_name="app",
            function_name="fn",
            api_key="key",
            timeout_seconds=5.0,
            max_retries=3,
        )

        with patch("app.adapters.nodus.adapter._RETRY_DELAY_SECONDS", 0.0):
            adapter = NodusExecutionAdapter(config)
            task = NodusTask(
                task_id="retry-t",
                operation="op",
                payload={"x": 1},
                max_retries=3,
            )
            result = adapter.execute(task)

        assert result.status == "succeeded"
        assert spawn_count == 3  # failed twice, succeeded third

    def test_non_retryable_sdk_error_on_spawn_is_terminal(self):
        """NodusError with retryable=False on spawn must not be retried."""
        import nodus.errors as nodus_errors

        spawn_count = 0

        class _NonRetryFunctionStub:
            @classmethod
            def from_name(cls, app, name, project=None):
                return cls()

            async def spawn(self, **kwargs):
                nonlocal spawn_count
                spawn_count += 1
                raise nodus_errors.NotFound("function not found")

        sys.modules["nodus"].Function = _NonRetryFunctionStub

        config = NodusConfig(
            execution_mode=ExecutionMode.REMOTE,
            app_name="app",
            function_name="fn",
            api_key="key",
            timeout_seconds=5.0,
            max_retries=3,
        )
        adapter = NodusExecutionAdapter(config)
        task = NodusTask(task_id="nr-t", operation="op", payload={"x": 1}, max_retries=3)
        result = adapter.execute(task)

        assert result.status == "failed"
        assert spawn_count == 1  # terminal, not retried

    def test_post_submission_error_does_not_trigger_retry(self):
        """An error in get() after a successful spawn must NOT cause re-spawn."""
        spawn_count = 0

        class _PostFailFunctionStub:
            @classmethod
            def from_name(cls, app, name, project=None):
                return cls()

            async def spawn(self, **kwargs):
                nonlocal spawn_count
                spawn_count += 1
                return _PostFailHandle()

        class _PostFailHandle:
            name = "prov-pf"
            async def get(self, timeout=None):
                raise RuntimeError("post-submission error")

        sys.modules["nodus"].Function = _PostFailFunctionStub

        config = NodusConfig(
            execution_mode=ExecutionMode.REMOTE,
            app_name="app",
            function_name="fn",
            api_key="key",
            timeout_seconds=5.0,
            max_retries=3,
        )
        adapter = NodusExecutionAdapter(config)
        task = NodusTask(task_id="pf-t", operation="op", payload={"x": 1}, max_retries=3)
        result = adapter.execute(task)

        assert result.status == "failed"
        assert spawn_count == 1  # spawned once only — no re-spawn on get() failure


# ===========================================================================
# 8. Smoke test mode guard
# ===========================================================================

class TestSmokeTestModeGuard:
    def test_mock_mode_exits_with_code_2(self, monkeypatch):
        monkeypatch.setenv("NODUS_EXECUTION_MODE", "mock")
        monkeypatch.setenv("NODUS_APP_NAME", "app")
        monkeypatch.setenv("NODUS_FUNCTION_NAME", "fn")
        monkeypatch.delenv("NODUS_API_KEY", raising=False)

        from app.nodus_smoke_test import run_smoke_test
        exit_code = run_smoke_test()
        assert exit_code == 2

    def test_local_mode_exits_with_code_2(self, monkeypatch):
        monkeypatch.setenv("NODUS_EXECUTION_MODE", "local")
        monkeypatch.setenv("NODUS_APP_NAME", "app")
        monkeypatch.setenv("NODUS_FUNCTION_NAME", "fn")
        monkeypatch.delenv("NODUS_API_KEY", raising=False)

        from app.nodus_smoke_test import run_smoke_test
        exit_code = run_smoke_test()
        assert exit_code == 2

    def test_missing_mode_exits_with_code_1(self, monkeypatch):
        monkeypatch.delenv("NODUS_EXECUTION_MODE", raising=False)
        monkeypatch.setenv("NODUS_APP_NAME", "app")
        monkeypatch.setenv("NODUS_FUNCTION_NAME", "fn")

        from app.nodus_smoke_test import run_smoke_test
        exit_code = run_smoke_test()
        assert exit_code == 1


# ===========================================================================
# 9. ExecutionMode parsing
# ===========================================================================

class TestExecutionMode:
    def test_from_str_remote(self):
        assert ExecutionMode.from_str("nodus_remote") == ExecutionMode.REMOTE

    def test_from_str_local(self):
        assert ExecutionMode.from_str("local") == ExecutionMode.LOCAL

    def test_from_str_mock(self):
        assert ExecutionMode.from_str("mock") == ExecutionMode.MOCK

    def test_from_str_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid execution mode"):
            ExecutionMode.from_str("auto")

    def test_from_str_case_insensitive(self):
        assert ExecutionMode.from_str("MOCK") == ExecutionMode.MOCK
