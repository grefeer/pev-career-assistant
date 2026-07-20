"""Unit tests for trajectory_annotator."""
from __future__ import annotations

import pytest
from backend.app.services.job_discovery.strategy.trajectory_annotator import TrajectoryAnnotator


class TestTrajectoryAnnotator:
    def test_build_annotation_prompt(self):
        annotator = TrajectoryAnnotator()
        steps = [
            {"tool": "open_url", "status": "ok", "params": {"url": "x"}, "result": "html"},
            {"tool": "open_url", "status": "failed", "params": {"url": "x"}, "error": "timeout"},
            {"tool": "open_url", "status": "ok", "params": {"url": "x"}, "result": "html"},
            {"tool": "extract_jd", "status": "ok", "result": [{"title": "Engineer"}]},
        ]
        prompt = annotator._build_prompt(steps, "https://x.com/job", "mp.weixin.qq.com/s/*")
        assert "open_url" in prompt
        assert "extract_jd" in prompt
        assert "retry_loops" in prompt  # annotation instruction
        assert "reusability_score" in prompt
        assert "https://x.com/job" in prompt

    def test_fallback_annotation(self):
        annotator = TrajectoryAnnotator()
        steps = [
            {"tool": "open_url", "status": "ok", "result": "html"},
            {"tool": "extract_jd", "status": "ok", "result": [{"title": "Engineer"}]},
        ]
        fallback = annotator._fallback_annotation(steps)
        assert "clean_path" in fallback
        assert "errors" in fallback
        assert "retry_loops" in fallback
        assert "key_decisions" in fallback
        assert "reusability_score" in fallback
        assert "reusability_reason" in fallback
        assert fallback["reusability_score"] == 0.5  # all steps ok

    def test_fallback_annotation_no_ok_steps(self):
        annotator = TrajectoryAnnotator()
        steps = [
            {"tool": "open_url", "status": "failed", "error": "timeout"},
        ]
        fallback = annotator._fallback_annotation(steps)
        assert fallback["reusability_score"] == 0.0  # no ok steps

    def test_parse_response_strips_markdown_fences(self):
        annotator = TrajectoryAnnotator()
        raw = """```json
{"retry_loops": [], "errors": [], "clean_path": [], "key_decisions": [], "reusability_score": 0.8, "reusability_reason": "good"}
```"""
        parsed = annotator._parse_response(raw)
        assert parsed["reusability_score"] == 0.8
        assert parsed["reusability_reason"] == "good"

    def test_parse_response_plain_json(self):
        annotator = TrajectoryAnnotator()
        raw = '{"retry_loops": [], "errors": [], "clean_path": [], "key_decisions": [], "reusability_score": 0.5, "reusability_reason": "average"}'
        parsed = annotator._parse_response(raw)
        assert parsed["reusability_score"] == 0.5
