"""Unit tests for DomainAdapter base and AlibabaSPAAdapter."""
from __future__ import annotations

import pytest
from backend.app.services.job_discovery.adapters.base import DomainAdapter
from backend.app.services.job_discovery.adapters.alibaba_spa import AlibabaSPAAdapter
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput, DiscoveryRunResult


class TestDomainAdapterBase:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            DomainAdapter()  # abstract

    def test_concrete_subclass_works(self):
        class TestAdapter(DomainAdapter):
            url_pattern = "test.com/*"

            def execute(self, task, strategy, trajectory):
                return DiscoveryRunResult(status="succeeded")

            def validate(self, url):
                return True

        adapter = TestAdapter()
        task = DiscoveryTaskInput(
            source_id="s1", raw_record_id="r1", external_record_id="e1",
            source_key="test", source_url="https://test.com/job",
            url_hash="abc", record_fields=[],
        )
        result = adapter.execute(task, None, None)
        assert result.status == "succeeded"


class TestAlibabaSPAAdapter:
    def test_url_pattern(self):
        adapter = AlibabaSPAAdapter()
        assert "alibaba.com" in adapter.url_pattern

    def test_validate_valid_url(self):
        adapter = AlibabaSPAAdapter()
        assert adapter.validate("https://campus-talent.alibaba.com/search") is True

    def test_validate_invalid_url(self):
        adapter = AlibabaSPAAdapter()
        assert adapter.validate("https://google.com") is False

    def test_execute_api_failure_returns_failed(self, monkeypatch):
        """When the underlying API call raises, adapter returns a failed result."""

        def _fail(*args, **kwargs):
            raise RuntimeError("API unreachable")

        monkeypatch.setattr(
            "backend.app.services.job_discovery.deepagents_runner._fetch_alibaba_search_api",
            _fail,
        )

        adapter = AlibabaSPAAdapter()
        task = DiscoveryTaskInput(
            source_id="s1", raw_record_id="r1", external_record_id="e1",
            source_key="alibaba", source_url="https://campus-talent.alibaba.com/search?q=java",
            url_hash="abc123", record_fields=[],
        )
        # Build a minimal mock trajectory
        from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
        traj = TrajectoryBuffer("task-1", None, "adapter")

        result = adapter.execute(task, None, traj)
        assert result.status == "failed"
        assert "API unreachable" in result.summary

    def test_execute_success_path(self, monkeypatch):
        """Full success path with mocked dependencies."""

        def _mock_fetch_alibaba(url):
            return {
                "content": {
                    "datas": [
                        {
                            "id": "199903220038",
                            "name": "AI应用研发工程师",
                            "workLocations": ["北京", "杭州", "上海"],
                            "description": "1、负责 AI 应用工程研发。",
                            "requirement": "1、熟悉 Python 和 Agent 框架。",
                            "categoryName": "技术类",
                            "categoryType": "internship",
                            "batchName": "阿里巴巴2027届实习生",
                            "circleNames": ["淘宝闪购"],
                        },
                    ]
                }
            }

        def _mock_extract_jd(*args, **kwargs):
            import json
            return json.dumps([{
                "title": "AI应用研发工程师",
                "company_name": "阿里巴巴",
                "description_text": "负责AI应用工程研发",
            }])

        monkeypatch.setattr(
            "backend.app.services.job_discovery.deepagents_runner._fetch_alibaba_search_api",
            _mock_fetch_alibaba,
        )
        monkeypatch.setattr(
            "backend.app.services.job_discovery.adapters.alibaba_spa._run_extraction",
            _mock_extract_jd,
        )
        monkeypatch.setattr(
            "backend.app.services.job_discovery.deepagents_runner.verify_evidence",
            lambda c, e: c,
        )
        monkeypatch.setattr(
            "backend.app.services.job_discovery.deepagents_runner.package_candidates",
            lambda c, h, k: c,
        )

        adapter = AlibabaSPAAdapter()
        task = DiscoveryTaskInput(
            source_id="s1", raw_record_id="r1", external_record_id="e1",
            source_key="alibaba", source_url="https://campus-talent.alibaba.com/search",
            url_hash="abc123", record_fields=[],
        )
        from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer
        traj = TrajectoryBuffer("task-1", None, "adapter")

        result = adapter.execute(task, None, traj)
        assert result.status == "succeeded"
        assert len(result.candidates) > 0
        assert len(traj.steps) == 3  # alibaba_api_fetch, alibaba_evidence_extract, extract_verify_package
