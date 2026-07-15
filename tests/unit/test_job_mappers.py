from datetime import datetime, timezone

import pytest

from backend.app.services.job_mappers import (
    BUILTIN_SOURCES,
    MAPPERS,
    NormalizedJobCandidate,
    SkippedRecord,
    SourceSchemaChangedError,
)
from backend.app.services.tencent_smartsheet import TencentField, TencentRecord


def text(field: str, value: str) -> dict[str, object]:
    return {
        "field": field,
        "text_value": {"items": [{"text": value, "type": "text"}]},
    }


def texts(field: str, *values: str) -> dict[str, object]:
    return {
        "field": field,
        "text_value": {"items": [{"text": value, "type": "text"} for value in values]},
    }


def url(field: str, value: str) -> dict[str, object]:
    return {
        "field": field,
        "url_value": {"items": [{"text": "点击内推", "type": "url", "link": value}]},
    }


def options(field: str, *values: str) -> dict[str, object]:
    return {
        "field": field,
        "option_value": {"items": [{"text": value} for value in values]},
    }


def intern_record(
    *,
    company: str = "示例公司",
    title: str = "工程师",
    apply_url: str = "https://example.com/jobs",
    extra_fields: list[dict[str, object]] | None = None,
) -> TencentRecord:
    return TencentRecord(
        "record-id",
        [
            text("公司名称", company),
            text("招聘岗位", title),
            url("投递链接", apply_url),
            *(extra_fields or []),
        ],
    )


def test_builtin_sources_are_fixed_and_versioned() -> None:
    assert [source.source_key for source in BUILTIN_SOURCES] == [
        "tencent-27-referrals",
        "tencent-intern-referrals",
    ]
    assert [source.file_id for source in BUILTIN_SOURCES] == [
        "DZkdPVGtGb1ZvaG5R",
        "DY3pHYkNvb0ZRSHdi",
    ]
    assert [source.sheet_id for source in BUILTIN_SOURCES] == ["t00i2h", "BB08J2"]
    assert [source.mapper_version for source in BUILTIN_SOURCES] == ["v1", "v1"]
    assert all(
        source.mapper_version == MAPPERS[source.source_key].version
        for source in BUILTIN_SOURCES
    )


def test_first_source_never_invents_a_title() -> None:
    record = TencentRecord(
        "r1",
        [text("企业名称", "北方华创"), url("内推链接", "https://example.com/jobs")],
    )
    result = MAPPERS["tencent-27-referrals"].map(record)
    assert result == SkippedRecord("missing_title")


def test_intern_source_maps_complete_record() -> None:
    record = TencentRecord(
        "r2",
        [
            text("公司名称", "阿里云"),
            text("招聘岗位", "研发、算法"),
            text("工作地点", "北京、杭州"),
            options("招聘类型", "27届暑期实习"),
            options("多选", "互联网", "AI"),
            url("投递链接", "https://campus.example.com/jobs?id=1"),
            text("内推码", "ABC123"),
            text("截止日期", "尽快投递"),
            {"field": "更新时间", "string_value": "1773763200000"},
        ],
    )
    result = MAPPERS["tencent-intern-referrals"].map(record)
    assert result == NormalizedJobCandidate(
        company_name="阿里云",
        title="研发、算法",
        locations=["北京", "杭州"],
        recruitment_types=["27届暑期实习"],
        industries=["互联网", "AI"],
        apply_url="https://campus.example.com/jobs?id=1",
        referral_code="ABC123",
        deadline_text="尽快投递",
        source_updated_at=datetime(2026, 3, 17, 16, 0, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (intern_record(company=" "), "missing_company"),
        (intern_record(title="\t"), "missing_title"),
        (intern_record(apply_url=""), "missing_apply_url"),
    ],
)
def test_intern_source_skips_missing_required_values(
    record: TencentRecord, reason: str
) -> None:
    assert MAPPERS["tencent-intern-referrals"].map(record) == SkippedRecord(reason)


@pytest.mark.parametrize(
    "invalid_url",
    [
        "ftp://example.com/jobs",
        "https:///jobs",
        "https://user:password@example.com/jobs",
        "https://example.com/jobs\nnext",
        "https://example.com/" + "a" * 4077,
    ],
)
def test_invalid_apply_urls_are_skipped(invalid_url: str) -> None:
    assert len(invalid_url) == 4097 if invalid_url.endswith("a" * 4077) else True
    assert MAPPERS["tencent-intern-referrals"].map(
        intern_record(apply_url=invalid_url)
    ) == SkippedRecord("invalid_apply_url")


@pytest.mark.parametrize(
    "invalid_url",
    [
        "https://./jobs",
        "https://-/jobs",
        "https://example..com/jobs",
        "https://example.com../jobs",
        "https://-example.com/jobs",
        "https://example-.com/jobs",
        "https://exam_ple.com/jobs",
        "https://" + "a" * 64 + ".com/jobs",
        "https://example .com/jobs",
        "https://999.999.999.999/jobs",
        "https://example.com:not-a-port/jobs",
        "https://example.com:70000/jobs",
    ],
)
def test_malformed_hosts_and_ports_are_skipped(invalid_url: str) -> None:
    assert MAPPERS["tencent-intern-referrals"].map(
        intern_record(apply_url=invalid_url)
    ) == SkippedRecord("invalid_apply_url")


@pytest.mark.parametrize(
    "apply_url",
    [
        "http://127.0.0.1/jobs",
        "https://[2001:db8::1]:8443/jobs",
        "https://例子.公司/职位",
        "https://xn--fsqu00a.xn--55qx5d/jobs",
        "https://example.com./jobs",
        "https://例子.公司./职位",
    ],
)
def test_valid_ip_and_idna_hosts_are_allowed(apply_url: str) -> None:
    result = MAPPERS["tencent-intern-referrals"].map(intern_record(apply_url=apply_url))
    assert isinstance(result, NormalizedJobCandidate)
    assert result.apply_url == apply_url


def test_apply_url_normalizes_only_surrounding_whitespace() -> None:
    result = MAPPERS["tencent-intern-referrals"].map(
        intern_record(apply_url="  https://example.com/a%20b?next=a%20b  ")
    )
    assert isinstance(result, NormalizedJobCandidate)
    assert result.apply_url == "https://example.com/a%20b?next=a%20b"


def test_url_at_maximum_length_is_allowed() -> None:
    apply_url = "https://example.com/" + "a" * 4076
    assert len(apply_url) == 4096
    result = MAPPERS["tencent-intern-referrals"].map(intern_record(apply_url=apply_url))
    assert isinstance(result, NormalizedJobCandidate)
    assert result.apply_url == apply_url


def test_text_items_are_joined_without_altering_internal_content() -> None:
    record = TencentRecord(
        "r-text-items",
        [
            texts("公司名称", " 示例", " 公  司 "),
            text("招聘岗位", "工程师"),
            url("投递链接", "https://example.com/jobs"),
        ],
    )
    result = MAPPERS["tencent-intern-referrals"].map(record)
    assert isinstance(result, NormalizedJobCandidate)
    assert result.company_name == "示例 公  司"


def test_lists_drop_blanks_and_exact_duplicates_but_keep_first_seen_order() -> None:
    result = MAPPERS["tencent-intern-referrals"].map(
        intern_record(
            extra_fields=[
                text("工作地点", "北京、 上海，深圳,北京；广州; 上海 ;  "),
                options("招聘类型", "暑期实习", " ", "暑期实习", "日常实习"),
                options("多选", "AI", "互联网", "AI"),
            ]
        )
    )
    assert isinstance(result, NormalizedJobCandidate)
    assert result.locations == ["北京", "上海", "深圳", "广州"]
    assert result.recruitment_types == ["暑期实习", "日常实习"]
    assert result.industries == ["AI", "互联网"]


@pytest.mark.parametrize("timestamp", ["not-a-timestamp", "", "999999999999999999999"])
def test_invalid_source_timestamps_become_missing(timestamp: str) -> None:
    record = intern_record(
        extra_fields=[{"field": "更新时间", "string_value": timestamp}]
    )
    mapper = MAPPERS["tencent-intern-referrals"]
    assert mapper.source_updated_at(record) is None
    result = mapper.map(record)
    assert isinstance(result, NormalizedJobCandidate)
    assert result.source_updated_at is None


def test_first_source_exposes_valid_source_timestamp_even_when_skipped() -> None:
    record = TencentRecord(
        "r-source-time",
        [{"field": "更新时间", "string_value": "0"}],
    )
    mapper = MAPPERS["tencent-27-referrals"]
    assert mapper.source_updated_at(record) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert mapper.map(record) == SkippedRecord("missing_title")


@pytest.mark.parametrize(
    ("source_key", "fields"),
    [
        (
            "tencent-27-referrals",
            [
                TencentField("company", "企业名称", "text"),
                TencentField("link", "内推链接", "text"),
            ],
        ),
        (
            "tencent-intern-referrals",
            [
                TencentField("company", "公司名称", "text"),
                TencentField("title", "招聘岗位", "text"),
                TencentField("link", "投递链接", "text"),
            ],
        ),
    ],
)
def test_schema_field_type_drift_raises_stable_error(
    source_key: str, fields: list[TencentField]
) -> None:
    with pytest.raises(SourceSchemaChangedError) as exc_info:
        MAPPERS[source_key].validate_schema(fields)
    assert exc_info.value.error_code == "source_schema_changed"


def test_missing_required_schema_field_raises_stable_error() -> None:
    fields = [
        TencentField("company", "公司名称", "text"),
        TencentField("link", "投递链接", "url"),
    ]
    with pytest.raises(SourceSchemaChangedError) as exc_info:
        MAPPERS["tencent-intern-referrals"].validate_schema(fields)
    assert exc_info.value.error_code == "source_schema_changed"


def test_valid_source_schemas_accept_additional_fields() -> None:
    MAPPERS["tencent-27-referrals"].validate_schema(
        [
            TencentField("company", "企业名称", "text"),
            TencentField("link", "内推链接", "url"),
            TencentField("extra", "不相关字段", "image"),
        ]
    )
    MAPPERS["tencent-intern-referrals"].validate_schema(
        [
            TencentField("company", "公司名称", "text"),
            TencentField("title", "招聘岗位", "text"),
            TencentField("link", "投递链接", "url"),
        ]
    )
