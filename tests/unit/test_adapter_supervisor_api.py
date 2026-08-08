"""A1 certified public-JSON adapters: contract, gating, failure semantics.

Covers docs/findjobs-optimization-plan.zh-CN.md §4.1 acceptance:
validate/execute contract, double gating (human-reviewed allowlist +
backend flag is the caller's job), polite pacing, 300/company cap, stable
job_id, and explicit ``blocked`` codes for every failure mode — never a
silent empty result.  All network I/O is mocked via httpx.MockTransport.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

SKILL_SCRIPTS = (
    Path(__file__).resolve().parents[2] / "skill" / "job-discovery" / "scripts"
)
sys.path.insert(0, str(SKILL_SCRIPTS))

from adapters import AdapterError, company_for_url, load_company_adapter  # noqa: E402
from adapters.base import (  # noqa: E402
    allowlist_reviewed,
    host_matches,
    load_allowlist,
)
from adapters.didi import DidiAdapter  # noqa: E402


def _reviewed_allowlist(tmp_path: Path) -> Path:
    """A human-reviewed allowlist (reviewer + date recorded) for tests."""
    data = {
        "version": 1,
        "review_status": "reviewed",
        "reviewed_by": "unit-test",
        "reviewed_on": "2026-08-08",
        "endpoints": [
            {
                "company": "didi",
                "host": "talent.didiglobal.com",
                "path_prefixes": ["/api/jobList", "/position/"],
                "max_items": 300,
                "min_delay_s": 0.2,
                "max_delay_s": 0.5,
            },
            {
                "company": "netease",
                "host": "hr.163.com",
                "path_prefixes": ["/api/hr163/position/queryPage"],
                "max_items": 300,
                "min_delay_s": 0.2,
                "max_delay_s": 0.5,
            },
            {
                "company": "baidu",
                "host": "talent.baidu.com",
                "path_prefixes": ["/httservice/getPostListNew"],
                "max_items": 300,
                "min_delay_s": 0.2,
                "max_delay_s": 0.5,
            },
        ],
    }
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _handler(pages: list[dict[str, Any]], status: int = 200) -> Any:
    """MockTransport handler returning pages in order, then a sentinel."""
    state = {"index": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["index"] < len(pages):
            payload = pages[state["index"]]
        else:
            payload = {"data": {"list": [], "total": 0}}
        state["index"] += 1
        return httpx.Response(status, json=payload)

    return handler


def _didi(allowlist: Path, handler: Any, **kwargs: Any) -> DidiAdapter:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DidiAdapter(
        allowlist_path=allowlist, client=client, sleep=lambda s: None, **kwargs
    )


def _didi_page(records: list[dict[str, Any]], total: int) -> dict[str, Any]:
    return {"data": {"list": records, "total": total}}


def _raw_didi(count: int, start: int = 0) -> list[dict[str, Any]]:
    return [
        {"id": start + i, "name": f"职位{start + i}", "city": "北京"}
        for i in range(count)
    ]


# --------------------------------------------------------------------------
# registry + company_for_url / load_company_adapter
# --------------------------------------------------------------------------


def test_company_for_url_maps_known_hosts_and_rejects_unknown() -> None:
    assert company_for_url("https://talent.didiglobal.com/position/1") == "didi"
    assert company_for_url("https://hr.163.com/api/hr163/position/queryPage") == "netease"
    assert company_for_url("https://talent.baidu.com/jobs/detail/9") == "baidu"
    assert company_for_url("https://jobs.bytedance.com/experienced") is None
    assert company_for_url("not a url") is None


def test_load_company_adapter_unknown_raises_stable_code() -> None:
    with pytest.raises(AdapterError) as exc:
        load_company_adapter("nonexistent")
    assert exc.value.code == "adapter_unknown"


# --------------------------------------------------------------------------
# allowlist gating
# --------------------------------------------------------------------------


def test_allowlist_reviewed_requires_recorded_reviewer(tmp_path) -> None:
    pending = _reviewed_allowlist(tmp_path)
    data = json.loads(pending.read_text(encoding="utf-8"))
    data["review_status"] = "pending_review"
    data["reviewed_by"] = None
    pending.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert allowlist_reviewed(load_allowlist(pending)) is False
    assert allowlist_reviewed(load_allowlist(_reviewed_allowlist(tmp_path))) is True


def test_validate_false_when_allowlist_not_reviewed(tmp_path) -> None:
    allowlist = _reviewed_allowlist(tmp_path)
    data = json.loads(allowlist.read_text(encoding="utf-8"))
    data["review_status"] = "pending_review"
    data["reviewed_by"] = None
    allowlist.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    adapter = _didi(allowlist, _handler([]))
    assert adapter.validate("https://talent.didiglobal.com/api/jobList") is False


def test_validate_accepts_only_allowlisted_host_and_prefix(tmp_path) -> None:
    adapter = _didi(_reviewed_allowlist(tmp_path), _handler([]))
    assert adapter.validate("https://talent.didiglobal.com/api/jobList") is True
    assert adapter.validate("https://talent.didiglobal.com/position/7") is True
    assert adapter.validate("https://other.example.com/api/jobList") is False
    assert adapter.validate("https://talent.didiglobal.com/other/path") is False


def test_validate_rejects_private_or_unsafe_targets(tmp_path) -> None:
    adapter = _didi(_reviewed_allowlist(tmp_path), _handler([]))
    assert adapter.validate("http://127.0.0.1/api/jobList") is False
    assert adapter.validate("http://10.0.0.5/api/jobList") is False
    assert adapter.validate("http://talent.didiglobal.com/api/jobList") is True


def test_missing_allowlist_raises_allowlist_code(tmp_path) -> None:
    adapter = _didi(tmp_path / "nope.json", _handler([]))
    with pytest.raises(AdapterError) as exc:
        adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert exc.value.code == "allowlist_missing"


def test_host_matches_supports_subdomain_patterns() -> None:
    assert host_matches("talent.didiglobal.com", "talent.didiglobal.com")
    assert host_matches("*.didiglobal.com", "talent.didiglobal.com")
    assert host_matches("*.didiglobal.com", "didiglobal.com")
    assert not host_matches("*.didiglobal.com", "other.com")


# --------------------------------------------------------------------------
# execute contract + record shape
# --------------------------------------------------------------------------


def test_execute_returns_records_with_stable_job_id(tmp_path) -> None:
    pages = [
        _didi_page(_raw_didi(2, start=10), total=2),
        _didi_page([], total=2),
    ]
    adapter = _didi(_reviewed_allowlist(tmp_path), _handler(pages))
    result = adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert result["company"] == "didi"
    records = result["records"]
    assert [r["job_id"] for r in records] == ["DD_10", "DD_11"]
    assert records[0]["title"] == "职位10"
    assert records[0]["apply_url"] == "https://talent.didiglobal.com/position/10"
    assert records[0]["description"] or records[0]["title"]


def test_execute_fails_on_non_allowlisted_url(tmp_path) -> None:
    adapter = _didi(_reviewed_allowlist(tmp_path), _handler([]))
    with pytest.raises(AdapterError) as exc:
        adapter.execute("https://jobs.example.com/jobList", None, None)
    assert exc.value.code == "url_not_allowlisted"


def test_execute_empty_result_is_explicit_blocked(tmp_path) -> None:
    adapter = _didi(_reviewed_allowlist(tmp_path), _handler([{"data": {"list": [], "total": 0}}]))
    with pytest.raises(AdapterError) as exc:
        adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert exc.value.code == "empty_result"


def test_execute_malformed_payload_is_explicit_blocked(tmp_path) -> None:
    handler = lambda request: httpx.Response(200, json={"unexpected": "shape"})  # noqa: E731
    adapter = _didi(_reviewed_allowlist(tmp_path), handler)
    with pytest.raises(AdapterError) as exc:
        adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert exc.value.code == "empty_result"


# --------------------------------------------------------------------------
# failure injection -> stable blocked codes
# --------------------------------------------------------------------------


def test_http_403_maps_to_http_error_code(tmp_path) -> None:
    handler = lambda request: httpx.Response(403)  # noqa: E731
    adapter = _didi(_reviewed_allowlist(tmp_path), handler)
    with pytest.raises(AdapterError) as exc:
        adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert exc.value.code == "http_error:403"


def test_timeout_maps_to_timeout_code(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    adapter = _didi(_reviewed_allowlist(tmp_path), handler)
    with pytest.raises(AdapterError) as exc:
        adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert exc.value.code == "timeout"


def test_transport_error_retries_then_maps_to_transport_code(tmp_path) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("conn refused", request=request)

    adapter = _didi(_reviewed_allowlist(tmp_path), handler)
    with pytest.raises(AdapterError) as exc:
        adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert exc.value.code == "transport_error"
    assert len(attempts) == 3  # retry window exhausted


def test_non_json_response_is_malformed_payload(tmp_path) -> None:
    handler = lambda request: httpx.Response(200, text="<html>not json</html>")  # noqa: E731
    adapter = _didi(_reviewed_allowlist(tmp_path), handler)
    with pytest.raises(AdapterError) as exc:
        adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert exc.value.code == "malformed_payload"


# --------------------------------------------------------------------------
# pacing + cap
# --------------------------------------------------------------------------


def test_pacing_sleep_is_invoked_between_pages(tmp_path) -> None:
    sleeps: list[float] = []

    def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    # total > page size forces 5 pages (1 record each), 4 paced transitions
    pages = [
        _didi_page(_raw_didi(1, start=1), total=250),
        _didi_page(_raw_didi(1, start=2), total=250),
        _didi_page(_raw_didi(1, start=3), total=250),
        _didi_page(_raw_didi(1, start=4), total=250),
        _didi_page([], total=250),
    ]
    client = httpx.Client(transport=httpx.MockTransport(_handler(pages)))
    adapter = DidiAdapter(
        allowlist_path=_reviewed_allowlist(tmp_path), client=client, sleep=record_sleep
    )
    adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert len(sleeps) == 4  # one pace before each page after the first
    assert all(0.2 <= delay <= 0.5 for delay in sleeps)


def test_cap_enforces_max_items_per_company(tmp_path) -> None:
    # 4 pages x 50 raw items but max_items=300: the 4th page must be capped.
    pages = [
        _didi_page(_raw_didi(50, start=0), total=500),
        _didi_page(_raw_didi(50, start=50), total=500),
        _didi_page(_raw_didi(50, start=100), total=500),
        _didi_page(_raw_didi(50, start=150), total=500),
        _didi_page(_raw_didi(50, start=200), total=500),
        _didi_page(_raw_didi(50, start=250), total=500),
        _didi_page(_raw_didi(50, start=300), total=500),
    ]
    client = httpx.Client(transport=httpx.MockTransport(_handler(pages)))
    adapter = DidiAdapter(
        allowlist_path=_reviewed_allowlist(tmp_path), client=client, sleep=lambda s: None
    )
    result = adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert len(result["records"]) == 300
    assert result["records"][-1]["job_id"] == "DD_299"


# --------------------------------------------------------------------------
# paging logic per company
# --------------------------------------------------------------------------


def test_didi_pages_until_total_exhausted(tmp_path) -> None:
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        page = int(request.url.params.get("page", "1"))
        if page >= 3:
            return httpx.Response(200, json=_didi_page([], total=100))
        return httpx.Response(200, json=_didi_page(_raw_didi(50, start=(page - 1) * 50), total=100))

    adapter = _didi(_reviewed_allowlist(tmp_path), handler)
    result = adapter.execute("https://talent.didiglobal.com/api/jobList", None, None)
    assert len(result["records"]) == 100
    assert [r["page"] for r in requests] == ["1", "2"]


def test_netease_posts_and_checks_code_and_pages(tmp_path) -> None:
    from adapters.netease import NeteaseAdapter

    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        requests.append(body)
        page = body.get("currentPage", 1)
        if page >= 2:
            return httpx.Response(200, json={"code": 200, "data": {"list": [], "pages": 2}})
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "list": [{"id": 7, "name": "前端工程师", "requirement": "熟悉 Vue"}],
                    "pages": 2,
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = NeteaseAdapter(
        allowlist_path=_reviewed_allowlist(tmp_path), client=client, sleep=lambda s: None
    )
    result = adapter.execute("https://hr.163.com/api/hr163/position/queryPage", None, None)
    assert [r["job_id"] for r in result["records"]] == ["NE_7"]
    assert requests[0]["currentPage"] == 1
    assert requests[0]["pageSize"] == 100


def test_baidu_pages_both_recruit_types(tmp_path) -> None:
    from adapters.baidu import BaiduAdapter

    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        requests.append(body)
        recruit_type = body.get("recruitType")
        cur_page = body.get("curPage", 1)
        if recruit_type == "SOCIAL":
            if cur_page == 1:
                return httpx.Response(
                    200,
                    json={"data": {"list": [{"postId": "s1", "name": "社招岗"}], "total": 1}},
                )
            return httpx.Response(200, json={"data": {"list": [], "total": 1}})
        # CAMPUS
        return httpx.Response(
            200,
            json={"data": {"list": [{"postId": "c1", "name": "校招岗"}], "total": 1}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BaiduAdapter(
        allowlist_path=_reviewed_allowlist(tmp_path), client=client, sleep=lambda s: None
    )
    result = adapter.execute("https://talent.baidu.com/httservice/getPostListNew", None, None)
    assert [r["job_id"] for r in result["records"]] == ["BD_s1", "BD_c1"]
    assert [r["title"] for r in result["records"]] == ["社招岗", "校招岗"]
    # SOCIAL page 1 (1 record) -> SOCIAL page 2 (empty, type ends) -> CAMPUS
    assert [r["recruitType"] for r in requests] == ["SOCIAL", "SOCIAL", "CAMPUS"]


# --------------------------------------------------------------------------
# edge branches: allowlist shape, classification, boundary helpers
# --------------------------------------------------------------------------


def test_load_allowlist_requires_endpoints_list(tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1, "endpoints": "oops"}), encoding="utf-8")
    with pytest.raises(AdapterError) as exc:
        load_allowlist(bad)
    assert exc.value.code == "allowlist_missing"


def test_endpoint_missing_for_company_raises_allowlist_code(tmp_path) -> None:
    from adapters.netease import NeteaseAdapter

    # an allowlist that reviews only didi -> no endpoint entry for netease
    didi_only = tmp_path / "didi-only.json"
    data = json.loads(_reviewed_allowlist(tmp_path).read_text(encoding="utf-8"))
    data["endpoints"] = [e for e in data["endpoints"] if e["company"] == "didi"]
    didi_only.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    adapter = NeteaseAdapter(allowlist_path=didi_only)
    with pytest.raises(AdapterError) as exc:
        adapter.execute("https://hr.163.com/api/hr163/position/queryPage", None, None)
    assert exc.value.code == "allowlist_missing"


def test_validate_rejects_malformed_url_and_wrong_host(tmp_path, monkeypatch) -> None:
    import adapters.base as base_mod

    adapter = _didi(_reviewed_allowlist(tmp_path), _handler([]))
    # defensive branch: urlparse raising after the public-url gate passed
    monkeypatch.setattr(
        base_mod, "urlparse", lambda value: (_ for _ in ()).throw(ValueError("bad url"))
    )
    assert adapter.validate("https://talent.didiglobal.com/api/jobList") is False
    monkeypatch.undo()
    assert adapter.validate("https://hr.163.com/api/jobList") is False


def test_classify_http_error_dns_cause() -> None:
    from adapters.base import _classify_http_error

    transport = httpx.ConnectError("dns failed")
    transport.__cause__ = socket.gaierror(11001, "getaddrinfo failed")
    assert _classify_http_error(transport).code == "dns_error"


def test_public_ip_safe_handles_all_failure_shapes(monkeypatch) -> None:
    from adapters.base import _public_ip_safe

    assert _public_ip_safe(None) is False  # no hostname
    monkeypatch.setattr(
        "adapters.base.socket.getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("nxdomain")),
    )
    assert _public_ip_safe("host.example") is False  # resolution failure
    monkeypatch.setattr(
        "adapters.base.socket.getaddrinfo", lambda *a, **k: [("A", 0, 0, "", ())]
    )
    assert _public_ip_safe("host.example") is False  # no resolved addresses
    monkeypatch.setattr(
        "adapters.base.socket.getaddrinfo",
        lambda *a, **k: [("A", 0, 0, "", ("not-an-ip",))],
    )
    assert _public_ip_safe("host.example") is False  # ipaddress ValueError


def test_request_json_rejects_unsafe_endpoint_url(tmp_path) -> None:
    adapter = _didi(_reviewed_allowlist(tmp_path), _handler([]))
    with pytest.raises(AdapterError) as exc:
        adapter._request_json(method="GET", url="http://127.0.0.1/jobs")
    assert exc.value.code == "url_not_allowlisted"


def test_request_json_rejects_non_object_json(tmp_path) -> None:
    adapter = _didi(_reviewed_allowlist(tmp_path), _handler([]))
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[1, 2]))
    )
    adapter._client = client
    with pytest.raises(AdapterError) as exc:
        adapter._request_json(method="GET", url="https://talent.didiglobal.com/api/jobList")
    assert exc.value.code == "malformed_payload"


def test_request_json_dns_gaierror_is_stable(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise socket.gaierror(11001, "getaddrinfo failed")

    adapter = _didi(_reviewed_allowlist(tmp_path), handler)
    with pytest.raises(AdapterError) as exc:
        adapter._request_json(method="GET", url="https://talent.didiglobal.com/api/jobList")
    assert exc.value.code == "dns_error"


def test_execute_task_without_url_raises_adapter_invalid(tmp_path) -> None:
    adapter = _didi(_reviewed_allowlist(tmp_path), _handler([]))
    with pytest.raises(AdapterError) as exc:
        adapter.execute(object())
    assert exc.value.code == "adapter_invalid"


def test_execute_accepts_task_like_object_with_url(tmp_path) -> None:
    from types import SimpleNamespace

    # the browse_fetch seam passes a task-like object with .url; execute
    # resolves it the same way as a bare url string
    adapter = _didi(_reviewed_allowlist(tmp_path), _handler([_didi_page(_raw_didi(1), 1)]))
    result = adapter.execute(
        SimpleNamespace(url="https://talent.didiglobal.com/api/jobList"), None, None
    )
    assert result["records"][0]["job_id"] == "DD_0"


def test_base_hooks_are_not_implemented(tmp_path) -> None:
    from adapters.base import BaseAdapter

    bare = BaseAdapter(allowlist_path=_reviewed_allowlist(tmp_path))
    with pytest.raises(NotImplementedError):
        bare.fetch_page(1)
    with pytest.raises(NotImplementedError):
        bare.build_record({})


# --------------------------------------------------------------------------
# company adapter defensive shapes
# --------------------------------------------------------------------------


def test_didi_list_not_a_list_is_clean_empty_page(tmp_path) -> None:
    adapter = _didi(
        _reviewed_allowlist(tmp_path), _handler([{"data": {"list": "oops", "total": 3}}])
    )
    assert adapter.fetch_page(1) == ([], False)


def test_netease_defensive_shapes(tmp_path) -> None:
    from adapters.netease import NeteaseAdapter

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": 500})
        )
    )
    adapter = NeteaseAdapter(
        allowlist_path=_reviewed_allowlist(tmp_path), client=client, sleep=lambda s: None
    )
    assert adapter.fetch_page(1) == ([], False)  # code != 200
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": 200, "data": "oops"})
        )
    )
    adapter._client = client
    assert adapter.fetch_page(1) == ([], False)  # data not a dict
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"code": 200, "data": {"list": "oops", "pages": 2}}
            )
        )
    )
    adapter._client = client
    assert adapter.fetch_page(1) == ([], False)  # list not a list


def test_baidu_type_exhausted_and_bad_list_shape(tmp_path) -> None:
    from adapters.baidu import BaiduAdapter
    from adapters.base import ERROR_EMPTY_RESULT

    # both recruit types drain to empty -> execute sees no records at all
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": {"list": [], "total": 0}})
        )
    )
    adapter = BaiduAdapter(
        allowlist_path=_reviewed_allowlist(tmp_path), client=client, sleep=lambda s: None
    )
    assert adapter.fetch_page(1) == ([], False)
    with pytest.raises(AdapterError) as exc:
        adapter.execute("https://talent.baidu.com/httservice/getPostListNew", None, None)
    assert exc.value.code == ERROR_EMPTY_RESULT

    # raw_posts not a list -> treated as empty page, moves to next type
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": {"list": {"oops": 1}}})
        )
    )
    fresh = BaiduAdapter(
        allowlist_path=_reviewed_allowlist(tmp_path), client=client, sleep=lambda s: None
    )
    # SOCIAL page yields no records -> skip to CAMPUS -> also empty -> drained
    assert fresh.fetch_page(1) == ([], False)

    # data itself not a dict -> treated as empty page
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": "oops"})
        )
    )
    fresh = BaiduAdapter(
        allowlist_path=_reviewed_allowlist(tmp_path), client=client, sleep=lambda s: None
    )
    assert fresh.fetch_page(1) == ([], False)


def test_registry_load_success_and_urlparse_value_error() -> None:
    from adapters import load_company_adapter

    assert load_company_adapter("didi").company == "didi"
    assert load_company_adapter("baidu").company == "baidu"
    # urlparse ValueError path in company_for_url
    assert company_for_url("http://[::1") is None


# --------------------------------------------------------------------------
# smoke CLI (__main__.py): usage, blocked codes, JSON output
# --------------------------------------------------------------------------


def test_smoke_cli_usage_and_bad_company(capsys) -> None:
    from adapters import __main__ as smoke

    assert smoke.main([]) == 2
    assert "usage:" in capsys.readouterr().out
    assert smoke.main(["didi"]) == 2
    assert smoke.main(["ghost", "https://talent.didiglobal.com/api/jobList"]) == 1
    assert "blocked: adapter_unknown" in capsys.readouterr().out


def test_smoke_cli_prints_records_json(tmp_path, capsys, monkeypatch) -> None:
    from adapters import __main__ as smoke

    adapter = _didi(_reviewed_allowlist(tmp_path), _handler([_didi_page(_raw_didi(1), 1)]))
    # smoke imports load_company_adapter at module load; patch that binding
    monkeypatch.setattr(smoke, "load_company_adapter", lambda company: adapter)
    assert smoke.main(["didi", "https://talent.didiglobal.com/api/jobList"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["company"] == "didi" and payload["records"][0]["job_id"] == "DD_0"


def test_smoke_cli_blocked_on_execute_failure(tmp_path, capsys, monkeypatch) -> None:
    from adapters import __main__ as smoke

    class BoomAdapter:
        company = "didi"

        def execute(self, *args, **kwargs):
            raise AdapterError("captcha", "human review required")

    monkeypatch.setattr(smoke, "load_company_adapter", lambda company: BoomAdapter())
    assert smoke.main(["didi", "https://talent.didiglobal.com/api/jobList"]) == 1
    assert "blocked: captcha" in capsys.readouterr().out


def test_smoke_cli_entrypoint_guard_runs_main(monkeypatch, capsys) -> None:
    """Cover the ``if __name__ == "__main__"`` guard (100% branch gate).

    Executes the module file as ``__main__`` in-process with sys.exit patched
    and a real argv, so the guard's sys.exit(main()) runs the real CLI through
    the unknown-company path (no LLM, no network; a subprocess would escape
    coverage measurement).
    """
    import sys

    from adapters import __main__ as smoke

    exits: list[int] = []
    monkeypatch.setattr(sys, "exit", lambda code: exits.append(code))
    monkeypatch.setattr(
        sys, "argv", ["adapters", "ghost", "https://example.com/job"]
    )
    exec(
        compile(
            Path(smoke.__file__).read_text(encoding="utf-8"),
            str(smoke.__file__),
            "exec",
        ),
        {"__name__": "__main__", "__package__": "adapters", "__file__": str(smoke.__file__)},
    )
    assert exits == [1]
    assert "blocked: adapter_unknown" in capsys.readouterr().out
