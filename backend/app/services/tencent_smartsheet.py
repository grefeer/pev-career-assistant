from __future__ import annotations

import asyncio
from builtins import BaseExceptionGroup
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import time
from typing import Any, Protocol, TypeGuard

from anyio import fail_after
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client


TENCENT_MCP_ENDPOINT = "https://docs.qq.com/openapi/mcp"
PAGE_SIZE = 100
MAX_PAGES = 1_000
MAX_RECORDS = 100_000


class TencentGatewayError(RuntimeError):
    error_code = "tencent_unavailable"


class TencentTokenMissingError(TencentGatewayError):
    error_code = "tencent_token_missing"


class TencentAuthError(TencentGatewayError):
    error_code = "tencent_auth_failed"


class TencentRateLimitError(TencentGatewayError):
    error_code = "tencent_rate_limited"


class TencentTimeoutError(TencentGatewayError):
    error_code = "tencent_timeout"


class TencentUnavailableError(TencentGatewayError):
    error_code = "tencent_unavailable"


class TencentProtocolError(TencentGatewayError):
    error_code = "tencent_protocol_error"


@dataclass(frozen=True)
class TencentField:
    field_id: str
    title: str
    field_type: str


@dataclass(frozen=True)
class TencentRecord:
    record_id: str
    field_values: list[dict[str, Any]]


@dataclass(frozen=True)
class TencentRecordPage:
    records: list[TencentRecord]
    total: int
    has_more: bool
    next_offset: int


class SmartsheetGateway(Protocol):
    def list_fields(self, file_id: str, sheet_id: str) -> list[TencentField]:
        raise NotImplementedError

    def list_records(
        self, file_id: str, sheet_id: str, *, offset: int, limit: int = PAGE_SIZE
    ) -> TencentRecordPage:
        raise NotImplementedError


ToolCaller = Callable[[str, dict[str, object]], dict[str, object]]
Sleeper = Callable[[float], None]


class TencentSmartsheetGateway:
    def __init__(
        self,
        token: str | None,
        tool_caller: ToolCaller | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self.token = token
        self.tool_caller = tool_caller or (
            lambda tool, arguments: asyncio.run(self._mcp_call(tool, arguments))
        )
        self.sleeper = sleeper or time.sleep

    async def _mcp_call(
        self, tool: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        if not self.token:
            raise TencentTokenMissingError("Tencent Docs token is not configured")
        headers = {"Authorization": self.token}
        with fail_after(15.0):
            async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
                async with streamable_http_client(
                    TENCENT_MCP_ENDPOINT, http_client=client
                ) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(tool, arguments=arguments)
        return self._parse_tool_result(result)

    def _parse_tool_result(self, result: types.CallToolResult) -> dict[str, object]:
        if result.isError:
            error_code = _extract_tool_error_code(result)
            if error_code in {400006, 400007}:
                raise TencentAuthError("Tencent authorization failed")
            if error_code in {400008, 429}:
                raise TencentRateLimitError("Tencent rate limit exceeded")
            if error_code is not None and 500 <= error_code < 600:
                raise TencentUnavailableError("Tencent service unavailable")
            raise TencentProtocolError("Tencent MCP returned a tool error")
        if isinstance(result.structuredContent, dict):
            return dict(result.structuredContent)
        for block in result.content:
            if isinstance(block, types.TextContent):
                try:
                    parsed = json.loads(block.text)
                except json.JSONDecodeError:
                    raise TencentProtocolError(
                        "Tencent MCP returned malformed JSON"
                    ) from None
                if isinstance(parsed, dict):
                    return parsed
        raise TencentProtocolError("Tencent MCP returned no object payload")

    def _invoke(self, tool: str, arguments: dict[str, object]) -> dict[str, object]:
        last_error: TencentGatewayError | None = None
        for attempt in range(3):
            try:
                try:
                    payload = self.tool_caller(tool, arguments)
                    error = str(payload.get("error", ""))
                    if "400006" in error or "400007" in error:
                        raise TencentAuthError("Tencent authorization failed")
                    if "400008" in error:
                        raise TencentRateLimitError("Tencent rate limit exceeded")
                    if error:
                        raise TencentUnavailableError("Tencent service unavailable")
                    return payload
                except BaseExceptionGroup as exc:
                    transport_error = _classified_transport_leaf(exc)
                    if transport_error is None:
                        raise
                    raise transport_error from exc
            except TencentAuthError:
                raise
            except TencentProtocolError:
                raise
            except TencentRateLimitError:
                last_error = TencentRateLimitError("Tencent rate limit exceeded")
            except TencentUnavailableError:
                last_error = TencentUnavailableError("Tencent service unavailable")
            except (TimeoutError, httpx.TimeoutException):
                last_error = TencentTimeoutError("Tencent request timed out")
            except (httpx.ConnectError, httpx.NetworkError):
                last_error = TencentUnavailableError("Tencent service unavailable")
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in {401, 403}:
                    raise TencentAuthError("Tencent authorization failed") from None
                if status == 429:
                    last_error = TencentRateLimitError("Tencent rate limit exceeded")
                elif 500 <= status < 600:
                    last_error = TencentUnavailableError("Tencent service unavailable")
                else:
                    raise TencentProtocolError(
                        "Tencent MCP request was rejected"
                    ) from None
            if attempt < 2:
                self.sleeper((0.25, 0.5)[attempt])
        assert last_error is not None
        raise last_error

    def list_fields(self, file_id: str, sheet_id: str) -> list[TencentField]:
        payload = self._invoke(
            "smartsheet.list_fields", {"file_id": file_id, "sheet_id": sheet_id}
        )
        fields = payload.get("fields")
        if not _is_object_sequence(fields):
            raise TencentProtocolError("Tencent MCP returned malformed fields")

        parsed_fields: list[TencentField] = []
        for value in fields:
            if not isinstance(value, Mapping):
                raise TencentProtocolError("Tencent MCP returned malformed fields")
            field_id = value.get("field_id")
            title = value.get("title")
            field_type = value.get("type")
            if (
                not _is_non_empty_string(field_id)
                or not _is_non_empty_string(title)
                or not _is_non_empty_string(field_type)
            ):
                raise TencentProtocolError("Tencent MCP returned malformed fields")
            parsed_fields.append(
                TencentField(field_id=field_id, title=title, field_type=field_type)
            )
        return parsed_fields

    def list_records(
        self, file_id: str, sheet_id: str, *, offset: int, limit: int = PAGE_SIZE
    ) -> TencentRecordPage:
        if not _is_non_negative_int(offset) or not _is_positive_int(limit):
            raise TencentProtocolError("Tencent MCP record pagination is invalid")
        payload = self._invoke(
            "smartsheet.list_records",
            {
                "file_id": file_id,
                "sheet_id": sheet_id,
                "offset": offset,
                "limit": limit,
            },
        )
        total = payload.get("total")
        has_more = payload.get("has_more")
        next_offset = payload.get("next")
        records = payload.get("records")
        if (
            not _is_non_negative_int(total)
            or not isinstance(has_more, bool)
            or not _is_non_negative_int(next_offset)
            or not _is_object_sequence(records)
            or len(records) > limit
            or (has_more and next_offset <= offset)
        ):
            raise TencentProtocolError("Tencent MCP returned malformed records")

        parsed_records: list[TencentRecord] = []
        for value in records:
            if not isinstance(value, Mapping):
                raise TencentProtocolError("Tencent MCP returned malformed records")
            record_id = value.get("record_id")
            field_values = value.get("field_values")
            if not _is_non_empty_string(record_id):
                raise TencentProtocolError("Tencent MCP returned malformed records")
            if not _is_object_sequence(field_values):
                raise TencentProtocolError("Tencent MCP returned malformed records")
            parsed_field_values: list[dict[str, Any]] = []
            for field_value in field_values:
                if not isinstance(field_value, Mapping):
                    raise TencentProtocolError("Tencent MCP returned malformed records")
                parsed_field_values.append(dict(field_value))
            parsed_records.append(
                TencentRecord(
                    record_id=record_id,
                    field_values=parsed_field_values,
                )
            )
        return TencentRecordPage(
            records=parsed_records,
            total=total,
            has_more=has_more,
            next_offset=next_offset,
        )


def _is_object_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _is_non_negative_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_non_empty_string(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value)


def _extract_tool_error_code(result: types.CallToolResult) -> int | None:
    if isinstance(result.structuredContent, Mapping):
        code = _numeric_error_code(result.structuredContent)
        if code is not None:
            return code
    for block in result.content:
        if not isinstance(block, types.TextContent):
            continue
        text = block.text.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        if isinstance(parsed, Mapping):
            code = _numeric_error_code(parsed)
            if code is not None:
                return code
        elif _is_integer_code(text):
            return int(text)
    return None


def _numeric_error_code(payload: Mapping[str, object]) -> int | None:
    for key in ("code", "error_code"):
        value = payload.get(key)
        if _is_integer_code(value):
            return int(value)
    return None


def _is_integer_code(value: object) -> bool:
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return bool(stripped) and stripped.lstrip("+-").isdigit()


def _classified_transport_leaf(
    group: BaseExceptionGroup[BaseException],
) -> Exception | None:
    leaves = _exception_group_leaves(group)
    if not leaves:
        return None
    if all(_is_auth_transport_error(leaf) for leaf in leaves):
        return leaves[0]
    if any(not _is_retryable_transport_error(leaf) for leaf in leaves):
        return None
    for leaf in leaves:
        if isinstance(leaf, httpx.HTTPStatusError) and leaf.response.status_code == 429:
            return leaf
    return leaves[0]


def _is_auth_transport_error(error: Exception) -> bool:
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {
        401,
        403,
    }


def _exception_group_leaves(
    group: BaseExceptionGroup[BaseException],
) -> list[Exception] | None:
    leaves: list[Exception] = []
    for error in group.exceptions:
        if isinstance(error, BaseExceptionGroup):
            nested_leaves = _exception_group_leaves(error)
            if nested_leaves is None:
                return None
            leaves.extend(nested_leaves)
        elif isinstance(error, Exception):
            leaves.append(error)
        else:
            return None
    return leaves


def _is_retryable_transport_error(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return True
    if isinstance(error, (httpx.ConnectError, httpx.NetworkError)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status == 429 or 500 <= status < 600
    return False
