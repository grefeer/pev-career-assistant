"""Unit tests for the final semantic candidate dedup in result_contract.

The supervisor frequently emits the same job twice: ``run_web_navigation``
returns verified + packaged candidates AND the LLM re-runs
``package_candidates`` on the same evidence. The re-packaged copies carry
different ``idempotency_key`` values, so ``_unique_items`` (byte-identical
JSON) cannot collapse them. ``_dedupe_candidate_dicts`` applies the production
canonical dedup on dicts while preserving the original packaging fields.
"""

from __future__ import annotations

import json

from backend.app.services.job_discovery.result_contract import (
    _as_candidate_dicts,
    _dedupe_candidate_dicts,
    _to_candidate_objects,
    parse_agent_result,
)
from backend.app.services.job_discovery.schemas import NormalizedJobCandidate


def _packaged(title: str, *, idempotency_key: str, responsibilities: str = "",
              requirements: str = "", company_name: str | None = None,
              apply_url: str | None = None, locations: list[str] | None = None) -> dict:
    """A packaged candidate dict shaped like the supervisor's tool output."""
    return {
        "title": title,
        "company_name": company_name,
        "responsibilities": responsibilities,
        "requirements": requirements,
        "locations": list(locations or []),
        "apply_url": apply_url,
        "idempotency_key": idempotency_key,
        "similarity_group_key": f"sim-{idempotency_key}",
    }


class TestNavPackageCandidateCollapse:
    def test_duplicate_run_web_navigation_and_package_collapses_to_one(self) -> None:
        # run_web_navigation copy and a re-packaged package_candidates copy of
        # the same title-only job (different idempotency keys).
        nav = _packaged("自动驾驶算法工程师", idempotency_key="nav-1")
        pkg = _packaged("自动驾驶算法工程师", idempotency_key="pkg-1")
        out = _dedupe_candidate_dicts([nav, pkg])
        assert len(out) == 1
        # First-seen (nav) packaging dict is retained.
        assert out[0]["idempotency_key"] == "nav-1"

    def test_preserves_packaging_fields_for_survivor(self) -> None:
        nav = _packaged("感知工程师", idempotency_key="nav-keep", apply_url="https://x/1")
        pkg = _packaged("感知工程师", idempotency_key="pkg-drop", apply_url="https://x/1")
        out = _dedupe_candidate_dicts([nav, pkg])
        assert len(out) == 1
        assert out[0]["idempotency_key"] == "nav-keep"
        assert out[0]["similarity_group_key"] == "sim-nav-keep"
        assert out[0]["apply_url"] == "https://x/1"

    def test_distinct_titles_all_survive(self) -> None:
        jobs = [(_packaged(f"岗位{i}", idempotency_key=f"nav-{i}"),
                 _packaged(f"岗位{i}", idempotency_key=f"pkg-{i}"))
                for i in range(5)]
        flat = [d for pair in jobs for d in pair]
        out = _dedupe_candidate_dicts(flat)
        assert len(out) == 5
        titles = [c["title"] for c in out]
        assert sorted(titles) == sorted(f"岗位{i}" for i in range(5))


class TestFullJdClusteringOnDicts:
    def test_same_jd_different_role_not_merged(self) -> None:
        body = "负责算法研发\n精通Python"
        a = _packaged("算法工程师", idempotency_key="a", responsibilities=body)
        b = _packaged("算法研究员", idempotency_key="b", responsibilities=body)
        out = _dedupe_candidate_dicts([a, b])
        assert len(out) == 2  # distinct roles -> two jobs

    def test_same_jd_duplicate_capture_collapses(self) -> None:
        body = "负责算法研发\n精通Python"
        a = _packaged("算法工程师", idempotency_key="nav")
        a["responsibilities"] = body
        b = _packaged("算法工程师", idempotency_key="pkg")
        b["responsibilities"] = body
        out = _dedupe_candidate_dicts([a, b])
        assert len(out) == 1

    def test_same_jd_city_variants_collapse(self) -> None:
        body = "负责算法研发\n精通Python"
        a = _packaged("算法工程师", idempotency_key="nav",
                      responsibilities=body, locations=["深圳"])
        b = _packaged("算法工程师-北京", idempotency_key="pkg",
                      responsibilities=body, locations=["北京"])
        out = _dedupe_candidate_dicts([a, b])
        assert len(out) == 1  # title-substring clustering collapses city variant


class TestEdgeCases:
    def test_empty_returns_empty(self) -> None:
        assert _dedupe_candidate_dicts([]) == []

    def test_non_dict_items_skipped(self) -> None:
        out = _dedupe_candidate_dicts([{"title": "x", "idempotency_key": "k"}, "noise", None, 42])
        assert len(out) == 1
        assert out[0]["title"] == "x"

    def test_unparseable_dict_kept_verbatim(self) -> None:
        # A dict that cannot be coerced to NormalizedJobCandidate (bad field type)
        # is kept as-is rather than dropped.
        bad = {"title": "bad", "locations": "not-a-list", "idempotency_key": "k"}
        out = _dedupe_candidate_dicts([bad])
        assert out == [bad]


class TestNormalizeAndConvert:
    def test_to_candidate_objects_returns_dataclass_instances(self) -> None:
        dicts = [_packaged("算法工程师", idempotency_key="k1"),
                 _packaged("后端工程师", idempotency_key="k2")]
        objs = _to_candidate_objects(dicts)
        assert len(objs) == 2
        assert all(isinstance(o, NormalizedJobCandidate) for o in objs)
        assert objs[0].title == "算法工程师"
        # packaging-only keys are dropped (worker recomputes them for objects)
        assert not hasattr(objs[0], "idempotency_key")

    def test_as_candidate_dicts_normalizes_objects(self) -> None:
        obj = NormalizedJobCandidate(title="感知工程师", company_name="元戎")
        dicts = _as_candidate_dicts([obj, {"title": "规划工程师"}])
        assert len(dicts) == 2
        assert dicts[0]["title"] == "感知工程师"
        assert dicts[1]["title"] == "规划工程师"

    def test_parse_agent_result_tool_only_returns_objects(self) -> None:
        # Simulate a supervisor raw result whose only output is a
        # run_web_navigation tool message with packaged candidate dicts (the
        # common case when the LLM does not emit a structured_response shell).
        nav_payload = {
            "evidence_pages": [{"evidence_type": "page_text", "content_hash": "h1"}],
            "candidates": [
                _packaged("岗位A", idempotency_key="nav-a"),
                _packaged("岗位A", idempotency_key="pkg-a"),  # re-packaged dup
                _packaged("岗位B", idempotency_key="nav-b"),
            ],
        }

        class _Msg:
            name = "run_web_navigation"
            content = json.dumps(nav_payload, ensure_ascii=False)

        result = parse_agent_result({"messages": [_Msg()]})
        assert result.status == "succeeded"
        assert len(result.candidates) == 2  # 岗位A dup collapsed
        assert all(isinstance(c, NormalizedJobCandidate) for c in result.candidates)
        titles = sorted(c.title for c in result.candidates)
        assert titles == ["岗位A", "岗位B"]
