from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

from backend.app.services.tencent_smartsheet import TencentField, TencentRecord


@dataclass(frozen=True)
class BuiltinJobSource:
    source_key: str
    name: str
    file_id: str
    sheet_id: str
    mapper_version: str


BUILTIN_SOURCES = (
    BuiltinJobSource(
        "tencent-27-referrals",
        "27届内推信息【重要】",
        "DZkdPVGtGb1ZvaG5R",
        "t00i2h",
        "v1",
    ),
    BuiltinJobSource(
        "tencent-intern-referrals",
        "实习内推汇总",
        "DY3pHYkNvb0ZRSHdi",
        "BB08J2",
        "v1",
    ),
)


@dataclass(frozen=True)
class NormalizedJobCandidate:
    company_name: str
    title: str
    locations: list[str]
    recruitment_types: list[str]
    industries: list[str]
    apply_url: str
    referral_code: str | None
    deadline_text: str | None
    source_updated_at: datetime | None


@dataclass(frozen=True)
class SkippedRecord:
    reason: str


class SourceSchemaChangedError(RuntimeError):
    error_code = "source_schema_changed"


class SourceMapper(Protocol):
    version: str

    def validate_schema(self, fields: list[TencentField]) -> None:
        raise NotImplementedError

    def source_updated_at(self, record: TencentRecord) -> datetime | None:
        raise NotImplementedError

    def map(self, record: TencentRecord) -> NormalizedJobCandidate | SkippedRecord:
        raise NotImplementedError


def _field_value(record: TencentRecord, title: str) -> Mapping[str, Any] | None:
    for field_value in record.field_values:
        if field_value.get("field") == title:
            return field_value
    return None


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Mapping):
        return ()
    items = value.get("items")
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray)):
        return items
    return ()


def _text(record: TencentRecord, title: str) -> str | None:
    field_value = _field_value(record, title)
    if field_value is None:
        return None
    parts: list[str] = []
    for item in _items(field_value.get("text_value")):
        if isinstance(item, Mapping) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    result = "".join(parts).strip()
    return result or None


def _url(record: TencentRecord, title: str) -> str | None:
    field_value = _field_value(record, title)
    if field_value is None:
        return None
    for item in _items(field_value.get("url_value")):
        if isinstance(item, Mapping) and isinstance(item.get("link"), str):
            link = item["link"]
            return link if link.strip() else None
    return None


def _options(record: TencentRecord, title: str) -> list[str]:
    field_value = _field_value(record, title)
    if field_value is None:
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in _items(field_value.get("option_value")):
        if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
            continue
        value = item["text"].strip()
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def _split_locations(value: str | None) -> list[str]:
    if value is None:
        return []
    locations: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[、，,；;]", value):
        location = part.strip()
        if location and location not in seen:
            seen.add(location)
            locations.append(location)
    return locations


def _source_updated_at(record: TencentRecord) -> datetime | None:
    field_value = _field_value(record, "更新时间")
    if field_value is None:
        return None
    value = field_value.get("string_value")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        milliseconds = int(value.strip())
        return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _normalize_url(value: str) -> str | None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 4096:
        return None
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or not _is_valid_hostname(hostname)
    ):
        return None
    return normalized


def _is_valid_hostname(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    if ":" in hostname:
        return False
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    dns_hostname = (
        ascii_hostname[:-1] if ascii_hostname.endswith(".") else ascii_hostname
    )
    if len(dns_hostname) > 253:
        return False
    labels = dns_hostname.split(".")
    if all(label.isdigit() for label in labels):
        return False
    return all(
        1 <= len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )


def _validate_schema(
    fields: list[TencentField], required_fields: Mapping[str, str]
) -> None:
    actual_fields = {field.title: field.field_type for field in fields}
    for title, expected_type in required_fields.items():
        if actual_fields.get(title) != expected_type:
            raise SourceSchemaChangedError(
                f"Required source field {title!r} is missing or changed type"
            )


class Tencent27ReferralsMapper:
    version = "v1"

    def validate_schema(self, fields: list[TencentField]) -> None:
        _validate_schema(fields, {"企业名称": "text", "内推链接": "url"})

    def source_updated_at(self, record: TencentRecord) -> datetime | None:
        return _source_updated_at(record)

    def map(self, record: TencentRecord) -> NormalizedJobCandidate | SkippedRecord:
        return SkippedRecord("missing_title")


class TencentInternReferralsMapper:
    version = "v1"

    def validate_schema(self, fields: list[TencentField]) -> None:
        _validate_schema(
            fields,
            {"公司名称": "text", "招聘岗位": "text", "投递链接": "url"},
        )

    def source_updated_at(self, record: TencentRecord) -> datetime | None:
        return _source_updated_at(record)

    def map(self, record: TencentRecord) -> NormalizedJobCandidate | SkippedRecord:
        company_name = _text(record, "公司名称")
        if company_name is None:
            return SkippedRecord("missing_company")
        title = _text(record, "招聘岗位")
        if title is None:
            return SkippedRecord("missing_title")
        apply_url = _url(record, "投递链接")
        if apply_url is None:
            return SkippedRecord("missing_apply_url")
        apply_url = _normalize_url(apply_url)
        if apply_url is None:
            return SkippedRecord("invalid_apply_url")
        return NormalizedJobCandidate(
            company_name=company_name,
            title=title,
            locations=_split_locations(_text(record, "工作地点")),
            recruitment_types=_options(record, "招聘类型"),
            industries=_options(record, "多选"),
            apply_url=apply_url,
            referral_code=_text(record, "内推码"),
            deadline_text=_text(record, "截止日期"),
            source_updated_at=self.source_updated_at(record),
        )


MAPPERS: dict[str, SourceMapper] = {
    "tencent-27-referrals": Tencent27ReferralsMapper(),
    "tencent-intern-referrals": TencentInternReferralsMapper(),
}
