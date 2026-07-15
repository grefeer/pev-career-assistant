import pytest

from backend.app.services.tencent_smartsheet import (
    TencentAuthError,
    TencentProtocolError,
    TencentRateLimitError,
    TencentSmartsheetGateway,
    TencentTokenMissingError,
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
