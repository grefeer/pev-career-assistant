"""TrajectoryAnnotator -- LLM-based semantic annotation of execution trajectories.

Triggered on-demand for supervisor-only and partial_fallback trajectories.
Uses a small model to annotate retry loops, errors, clean paths, key decisions,
and reusability scores.
"""
from __future__ import annotations

import json
from typing import Any

from backend.app.config import Settings


class TrajectoryAnnotator:
    """Annotates execution trajectories with semantic metadata using a small LLM.

    The annotation is called **on-demand** by the worker only for
    ``executor_type='supervisor'`` or ``status='partial_fallback'`` cases
    in the current plan.  Other trajectories are persisted without annotation.
    """

    ANNOTATION_SYSTEM_PROMPT = """You are a trajectory analyst. Given a tool execution trace from a job discovery agent, produce a JSON annotation with these fields:

1. retry_loops: list of {start_step, end_step, tool, reason} -- identify retry patterns where the same tool was called multiple times after failures
2. errors: list of {step, tool, error, category} -- each error step, with category one of: network_timeout, http_blocked, captcha, wechat_blocked, site_changed, empty_text, parse_error, ocr_failed, unknown
3. clean_path: list of {tool, status} -- the successful execution path with retries and errors removed
4. key_decisions: list of strings -- summarize important decision points (e.g. "chose OCR over text extraction because page was image-only")
5. reusability_score: float 0.0-1.0 -- how suitable this trajectory is for extracting a reusable strategy. High score = straightforward, repeatable flow with no unusual decisions.
6. reusability_reason: string -- one sentence explaining the score.

Return ONLY valid JSON. No markdown, no commentary."""

    def annotate(
        self,
        steps: list[dict[str, Any]],
        url: str,
        url_pattern: str | None,
        settings: Settings,
    ) -> dict[str, Any]:
        """Annotate a trajectory's steps. Returns the annotation dict.

        If the LLM call fails or the response can't be parsed, returns a
        minimal annotation dict so the caller can proceed.

        Args:
            steps: List of recorded step dicts from the trajectory buffer.
            url: The source URL that was navigated.
            url_pattern: The URL pattern matched (may be None).
            settings: Application settings for LLM configuration.
        """
        prompt = self._build_prompt(steps, url, url_pattern)
        try:
            raw = self._call_llm(prompt, settings)
            return self._parse_response(raw)
        except Exception:
            return self._fallback_annotation(steps)

    def _build_prompt(
        self,
        steps: list[dict[str, Any]],
        url: str,
        url_pattern: str | None,
    ) -> str:
        """Build the annotation prompt from trajectory steps."""
        simplified = []
        for i, s in enumerate(steps):
            simplified.append({
                "step": i + 1,
                "tool": s.get("tool", ""),
                "status": s.get("status", ""),
                "error": s.get("error"),
                "duration_ms": s.get("duration_ms", 0),
            })
        return f"""Analyze this job discovery agent execution trace:

URL: {url}
Pattern: {url_pattern or "unknown"}

Steps:
{json.dumps(simplified, indent=2, ensure_ascii=False)}

Return the JSON annotation per the specified format. The annotation must include:
- retry_loops: list of {{start_step, end_step, tool, reason}}
- errors: list of {{step, tool, error, category}}
- clean_path: list of {{tool, status}}
- key_decisions: list of strings
- reusability_score: float 0.0-1.0
- reusability_reason: string"""

    def _call_llm(self, prompt: str, settings: Settings) -> str:
        """Call a small LLM for annotation. Uses the same model as the agent."""
        from backend.app.services.job_discovery.deepagents_runner import (
            _build_job_discovery_llm,
        )
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = _build_job_discovery_llm(settings)
        messages = [
            SystemMessage(content=self.ANNOTATION_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        response = llm.invoke(messages)
        content = response.content if hasattr(response, "content") else str(response)
        return content if isinstance(content, str) else str(content)

    def _parse_response(self, raw: str) -> dict[str, Any]:
        """Parse the LLM JSON response, handling common formatting issues."""
        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
        return json.loads(cleaned.strip())

    def _fallback_annotation(self, steps: list[dict[str, Any]]) -> dict[str, Any]:
        """Minimal annotation when LLM call fails."""
        ok_steps = [s for s in steps if s.get("status") == "ok"]
        return {
            "retry_loops": [],
            "errors": [],
            "clean_path": [
                {"tool": s.get("tool", ""), "status": s.get("status", "")}
                for s in ok_steps
            ],
            "key_decisions": [],
            "reusability_score": 0.5 if ok_steps else 0.0,
            "reusability_reason": "Auto-generated fallback (LLM annotation failed)",
        }
