"""Round 5 regression tests for killable Playwright fallback workers."""

from __future__ import annotations

import subprocess

from backend.app.services.career_skills import job_discovery


def test_playwright_worker_command_isolated_from_parent_runtime() -> None:
    command = job_discovery._playwright_worker_command(
        "https://jobs.example/detail", collect_links=True
    )

    assert "skill.job_discovery.runtime.playwright_worker" in command
    assert "--url" in command
    assert "https://jobs.example/detail" in command
    assert "--collect-links" in command


def test_timeout_cleanup_terminates_the_owned_process_tree(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(job_discovery.subprocess, "run", fake_run)
    job_discovery._terminate_process_tree(4242)

    assert calls
    command, kwargs = calls[0]
    assert command[:2] == ["taskkill", "/PID"]
    assert "4242" in command
    assert "/T" in command and "/F" in command
    assert kwargs["check"] is False

