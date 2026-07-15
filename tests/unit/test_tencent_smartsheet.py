import asyncio
from builtins import BaseExceptionGroup, ExceptionGroup
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, contextmanager

import httpx
from mcp import types
import pytest

from backend.app.services import tencent_smartsheet as gateway_module
from backend.app.services.tencent_smartsheet import (
    TencentAuthError,
    TencentProtocolError,
    TencentRateLimitError,
    TencentSmartsheetGateway,
    TencentTokenMissingError,
    TencentUnavailableError,
)


def test_list_records_parses_a_page() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def call(tool: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((tool, arguments))
        return {
            "error": "",
            "total": 1,
            "has_more": False,
            "next": 0,
            "records": [
                {
                    "record_id": "r1",
                    "field_values": [
                        {
                            "field": "公司名称",
                            "text_value": {
                                "items": [{"text": "示例公司", "type": "text"}]
                            },
                        }
                    ],
                }
            ],
        }

    gateway = TencentSmartsheetGateway(token="secret", tool_caller=call)
    page = gateway.list_records("file", "sheet", offset=0, limit=100)
    assert page.total == 1
    assert page.records[0].record_id == "r1"
    assert calls == [
        (
            "smartsheet.list_records",
            {"file_id": "file", "sheet_id": "sheet", "offset": 0, "limit": 100},
        )
    ]


def test_list_records_rejects_non_advancing_cursor() -> None:
    gateway = TencentSmartsheetGateway(
        token="secret",
        tool_caller=lambda *_: {
            "error": "",
            "total": 2,
            "has_more": True,
            "next": 0,
            "records": [{"record_id": "r1", "field_values": []}],
        },
    )
    with pytest.raises(TencentProtocolError):
        gateway.list_records("file", "sheet", offset=0, limit=100)


def test_temporary_failure_retries_at_most_three_attempts() -> None:
    attempts = 0

    def call(_tool: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("secret upstream detail")
        return {"error": "", "total": 0, "has_more": False, "next": 0, "records": []}

    gateway = TencentSmartsheetGateway(
        token="secret", tool_caller=call, sleeper=lambda _seconds: None
    )
    assert gateway.list_records("file", "sheet", offset=0, limit=100).total == 0
    assert attempts == 3


def test_missing_token_is_rejected_before_connecting() -> None:
    gateway = TencentSmartsheetGateway(token=None)

    with pytest.raises(TencentTokenMissingError):
        gateway.list_fields("file", "sheet")


@pytest.mark.parametrize("error_code", ["400006", "400007"])
def test_authorization_error_codes_are_not_retried(error_code: str) -> None:
    attempts = 0

    def call(_tool: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        return {"error": f"upstream error {error_code}"}

    gateway = TencentSmartsheetGateway(token="secret", tool_caller=call)

    with pytest.raises(TencentAuthError, match="authorization failed"):
        gateway.list_fields("file", "sheet")
    assert attempts == 1


def test_rate_limit_error_code_retries_three_times() -> None:
    attempts = 0

    def call(_tool: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        return {"error": "upstream error 400008"}

    gateway = TencentSmartsheetGateway(
        token="secret", tool_caller=call, sleeper=lambda _seconds: None
    )

    with pytest.raises(TencentRateLimitError, match="rate limit exceeded"):
        gateway.list_fields("file", "sheet")
    assert attempts == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "", "total": 0, "has_more": False, "next": 0, "records": {}},
        {"error": "", "total": -1, "has_more": False, "next": 0, "records": []},
        {"error": "", "total": 0, "has_more": False, "next": -1, "records": []},
    ],
)
def test_list_records_rejects_malformed_content(payload: dict[str, object]) -> None:
    gateway = TencentSmartsheetGateway(token="secret", tool_caller=lambda *_: payload)

    with pytest.raises(TencentProtocolError):
        gateway.list_records("file", "sheet", offset=0, limit=100)


def test_list_records_rejects_more_records_than_limit() -> None:
    payload = {
        "error": "",
        "total": 2,
        "has_more": False,
        "next": 0,
        "records": [
            {"record_id": "r1", "field_values": []},
            {"record_id": "r2", "field_values": []},
        ],
    }
    gateway = TencentSmartsheetGateway(token="secret", tool_caller=lambda *_: payload)

    with pytest.raises(TencentProtocolError):
        gateway.list_records("file", "sheet", offset=0, limit=1)


def test_list_records_requires_record_id() -> None:
    gateway = TencentSmartsheetGateway(
        token="secret",
        tool_caller=lambda *_: {
            "error": "",
            "total": 1,
            "has_more": False,
            "next": 0,
            "records": [{"field_values": []}],
        },
    )

    with pytest.raises(TencentProtocolError):
        gateway.list_records("file", "sheet", offset=0, limit=100)


def test_list_fields_parses_fields() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def call(tool: str, arguments: dict[str, object]) -> dict[str, object]:
        calls.append((tool, arguments))
        return {
            "error": "",
            "fields": [
                {"field_id": "f1", "title": "公司名称", "type": "text"},
            ],
        }

    gateway = TencentSmartsheetGateway(token="secret", tool_caller=call)

    fields = gateway.list_fields("file", "sheet")

    assert fields[0].field_id == "f1"
    assert fields[0].title == "公司名称"
    assert fields[0].field_type == "text"
    assert calls == [
        ("smartsheet.list_fields", {"file_id": "file", "sheet_id": "sheet"})
    ]


def test_mcp_lifecycle_runs_inside_fifteen_second_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline_active = False

    @contextmanager
    def fail_after(seconds: float):
        nonlocal deadline_active
        assert seconds == 15.0
        deadline_active = True
        try:
            yield
        finally:
            deadline_active = False

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            assert deadline_active
            return self

        async def __aexit__(self, *_args: object) -> None:
            assert deadline_active

    class FakeStreamContext:
        async def __aenter__(self) -> tuple[object, object, object]:
            assert deadline_active
            return object(), object(), object()

        async def __aexit__(self, *_args: object) -> None:
            assert deadline_active

    class FakeSession:
        def __init__(self, *_args: object) -> None:
            assert deadline_active

        async def __aenter__(self) -> "FakeSession":
            assert deadline_active
            return self

        async def __aexit__(self, *_args: object) -> None:
            assert deadline_active

        async def initialize(self) -> None:
            assert deadline_active

        async def call_tool(
            self, _tool: str, *, arguments: dict[str, object]
        ) -> types.CallToolResult:
            assert deadline_active
            return types.CallToolResult(
                content=[], structuredContent={"error": "", "fields": []}
            )

    def stream_client(
        _endpoint: str, *, http_client: object
    ) -> AbstractAsyncContextManager[tuple[object, object, object]]:
        assert deadline_active
        assert isinstance(http_client, FakeAsyncClient)
        return FakeStreamContext()

    monkeypatch.setattr(gateway_module, "fail_after", fail_after, raising=False)
    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(gateway_module, "streamable_http_client", stream_client)
    monkeypatch.setattr(gateway_module, "ClientSession", FakeSession)

    gateway = TencentSmartsheetGateway(token="placeholder-token")
    payload = asyncio.run(gateway._mcp_call("smartsheet.list_fields", {}))

    assert payload == {"error": "", "fields": []}
    assert deadline_active is False


@pytest.mark.parametrize(
    ("result", "error_type"),
    [
        (
            types.CallToolResult(
                isError=True,
                content=[],
                structuredContent={"code": 400006},
            ),
            TencentAuthError,
        ),
        (
            types.CallToolResult(
                isError=True,
                content=[],
                structuredContent={"error_code": "400007"},
            ),
            TencentAuthError,
        ),
        (
            types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text='{"code": 400008}')],
            ),
            TencentRateLimitError,
        ),
        (
            types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text="429")],
            ),
            TencentRateLimitError,
        ),
        (
            types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text='{"code": 503}')],
            ),
            TencentUnavailableError,
        ),
        (
            types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text='{"code": -32602}')],
            ),
            TencentProtocolError,
        ),
        (
            types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text="service unavailable")],
            ),
            TencentProtocolError,
        ),
        (
            types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text='"503"')],
            ),
            TencentProtocolError,
        ),
    ],
)
def test_sdk_tool_errors_are_classified_before_retry(
    result: types.CallToolResult,
    error_type: type[Exception],
) -> None:
    gateway = TencentSmartsheetGateway(token="placeholder-token")

    with pytest.raises(error_type):
        gateway._parse_tool_result(result)


def test_sdk_unavailable_tool_error_retries_at_most_three_attempts() -> None:
    attempts = 0
    result = types.CallToolResult(
        isError=True,
        content=[types.TextContent(type="text", text='{"code": 503}')],
    )
    gateway = TencentSmartsheetGateway(
        token="placeholder-token", sleeper=lambda _seconds: None
    )

    def call(_tool: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        return gateway._parse_tool_result(result)

    gateway.tool_caller = call

    with pytest.raises(TencentUnavailableError):
        gateway.list_fields("file", "sheet")
    assert attempts == 3


def test_sdk_malformed_success_text_is_protocol_error_without_retry() -> None:
    attempts = 0
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text="{malformed")]
    )
    gateway = TencentSmartsheetGateway(
        token="placeholder-token", sleeper=lambda _seconds: None
    )

    def call(_tool: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        return gateway._parse_tool_result(result)

    gateway.tool_caller = call

    with pytest.raises(TencentProtocolError):
        gateway.list_fields("file", "sheet")
    assert attempts == 1


@pytest.mark.parametrize(
    ("response_factory", "error_type"),
    [
        (
            lambda request: httpx.Response(429, request=request),
            TencentRateLimitError,
        ),
        (
            lambda request: httpx.Response(503, request=request),
            TencentUnavailableError,
        ),
        (
            lambda request: (_ for _ in ()).throw(
                httpx.ConnectError("connection failed", request=request)
            ),
            TencentUnavailableError,
        ),
    ],
)
def test_production_transport_retries_grouped_retryable_failures(
    monkeypatch: pytest.MonkeyPatch,
    response_factory: Callable[[httpx.Request], httpx.Response],
    error_type: type[Exception],
) -> None:
    attempts = 0
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return response_factory(request)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", client_factory)
    gateway = TencentSmartsheetGateway(
        token="placeholder-token", sleeper=lambda _seconds: None
    )

    with pytest.raises(error_type):
        gateway.list_fields("file", "sheet")
    assert attempts == 3


def test_transport_group_with_unrelated_leaf_is_not_retried() -> None:
    attempts = 0
    request = httpx.Request("POST", "https://example.invalid")
    grouped_error = ExceptionGroup(
        "mixed transport failures",
        [
            httpx.ConnectError("connection failed", request=request),
            ValueError("unrelated failure"),
        ],
    )

    def call(_tool: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        raise grouped_error

    gateway = TencentSmartsheetGateway(
        token="placeholder-token",
        tool_caller=call,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(ExceptionGroup) as exc_info:
        gateway.list_fields("file", "sheet")
    assert exc_info.value is grouped_error
    assert attempts == 1


def test_transport_group_with_unrelated_base_exception_is_not_retried() -> None:
    attempts = 0
    request = httpx.Request("POST", "https://example.invalid")
    grouped_error = BaseExceptionGroup(
        "mixed transport failures",
        [
            httpx.ConnectError("connection failed", request=request),
            BaseExceptionGroup("cancellation", [KeyboardInterrupt()]),
        ],
    )

    def call(_tool: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        raise grouped_error

    gateway = TencentSmartsheetGateway(
        token="placeholder-token",
        tool_caller=call,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(BaseExceptionGroup) as exc_info:
        gateway.list_fields("file", "sheet")
    assert exc_info.value is grouped_error
    assert attempts == 1
