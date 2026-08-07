"""Unit tests for the browse-backed fetch layer (Task 7).

The chain under test never launches Playwright: every test fakes the
``browse`` script through ``browse_fetch_urls(runner=...)`` (the same
``(script, *, cli_args, stdin)`` seam the skill tests use) or through a
monkeypatched ``run_skill_script``.  The JSON contracts returned by the
fakes mirror the real ``skill/job-discovery/scripts/browse.py`` output
(status keys ``ok``/``error``/``blocked``, reason under ``reason``/``error``,
``used_path`` markers ``spa_shell_empty_no_evidence`` /
``click_fallback_fetch_error (...)``, cache hits carrying ``cached: true``).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pytest

from backend.app.services.deepagents_runtime.tools.skill_graphs.browse_fetch import (
    SiteClass,
    browse_fetch_urls,
    classify_url,
    mode_for_class,
    page_file_hash,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mp.weixin.qq.com/s/AbC123", SiteClass.WECHAT),
        ("https://weixin.qq.com/s/xyz", SiteClass.WECHAT),
        ("https://job.mokahr.com/abc", SiteClass.PARALLEL_FETCH),
        ("https://jobs.bytedance.com/experienced", SiteClass.PARALLEL_FETCH),
        ("https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY", SiteClass.PARALLEL_FETCH),
        ("https://jobs.feishu.cn/abc", SiteClass.LIST),
        ("https://xiaopeng.jobs.feishu.cn/s/xyz", SiteClass.LIST),
        ("https://www.zhipin.com/job/1", SiteClass.SEARCH_INTERACT),
        ("https://norincogroupzhaopin.zhiye.com/campus/jobs", SiteClass.SEARCH_INTERACT),
        ("https://unknown.example.com/list", SiteClass.PROBE),
    ],
)
def test_classify_url_table(url: str, expected: SiteClass) -> None:
    assert classify_url(url) == expected


def test_page_file_hash_is_sha256_of_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "page_01.txt")
        Path(path).write_text("职位描述", encoding="utf-8")
        digest, size = page_file_hash(path, out_dir=tmp)
        assert digest == hashlib.sha256(Path(path).read_bytes()).hexdigest()
        assert size == len("职位描述".encode("utf-8"))


def test_page_file_hash_resolves_relative_paths_against_out_dir(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "page_01.txt").write_text("hello", encoding="utf-8")
    digest, size = page_file_hash("pages/page_01.txt", out_dir=str(tmp_path))
    assert digest == hashlib.sha256(b"hello").hexdigest()
    assert size == 5


def test_mode_for_class_mapping() -> None:
    assert mode_for_class(SiteClass.PARALLEL_FETCH) == "parallel-fetch"
    assert mode_for_class(SiteClass.LIST) == "list"
    assert mode_for_class(SiteClass.SEARCH_INTERACT) == "search-interact"
    assert mode_for_class(SiteClass.PROBE) == "list"
    assert mode_for_class(SiteClass.WECHAT) is None
    # probe dict is accepted (SKILL.md Phase 2 probe decision seam) but a
    # PROBE class still starts with list mode
    assert mode_for_class(SiteClass.PROBE, probe={"signal": "search_box"}) == "list"


def test_parallel_fetch_success_contract(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "page_01.txt").write_text("职位 A", encoding="utf-8")
    (tmp_path / "pages" / "page_02.txt").write_text("职位 B", encoding="utf-8")

    def fake_runner(script, *, cli_args="", stdin=""):
        assert script == "browse"
        assert "--mode parallel-fetch" in cli_args
        return json.dumps({
            "status": "ok", "url": "https://job.mokahr.com/abc", "mode": "parallel-fetch",
            "title": "公司职位", "content_hash": "sha256_abcd1234",
            "text_path": "output/evidence/run-x/页_01.txt",
            "page_files": ["pages/page_01.txt", "pages/page_02.txt"],
            "page_count": 2, "used_path": "parallel", "text_length": 5000,
        })

    results = browse_fetch_urls(
        ["https://job.mokahr.com/abc"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(results) == 1
    result = results[0]
    assert result.status == "succeeded"
    assert result.mode == "parallel-fetch"
    assert result.used_path == "parallel"
    assert result.terminal_evidence == []
    assert result.cached is False
    assert len(result.page_files) == 2
    # evidence hash is the full sha256 of the file bytes, never browse's
    # short sha256_<16> content_hash
    assert all(pf.content_hash.startswith("sha256_") is False for pf in result.page_files)
    assert result.page_files[0].content_hash == hashlib.sha256(
        (tmp_path / "pages" / "page_01.txt").read_bytes()
    ).hexdigest()
    assert result.page_files[1].text_length == len("职位 B".encode("utf-8"))


def test_spa_shell_empty_falls_back_to_search_interact_once(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        assert script == "browse"
        calls.append(cli_args)
        if "--mode parallel-fetch" in cli_args:
            # exact browse.py blocked contract for a 0-char SPA shell
            return json.dumps({
                "status": "blocked", "url": "https://job.mokahr.com/abc",
                "mode": "parallel-fetch", "used_path": "spa_shell_empty_no_evidence",
                "reason": "page rendered 0 chars of body text and no public job JSON evidence",
                "title": "", "content_hash": "sha256_abcd", "text_path": "",
                "screenshot_path": "", "text_length": 0, "page_count": 0,
                "page_files": [],
            })
        (tmp_path / "pages").mkdir(exist_ok=True)
        page = tmp_path / "pages" / "page_01.txt"
        page.write_text("职位列表", encoding="utf-8")
        return json.dumps({
            "status": "ok", "url": "https://job.mokahr.com/abc", "mode": "search-interact",
            "title": "职位", "content_hash": "sha256_zzz",
            "text_path": "", "page_files": ["pages/page_01.txt"],
            "page_count": 1, "text_length": 4,
        })

    results = browse_fetch_urls(
        ["https://job.mokahr.com/abc"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(calls) == 2
    assert sum("--mode search-interact" in c for c in calls) == 1
    result = results[0]
    assert result.status == "succeeded"
    assert result.mode == "search-interact"


def test_blocked_never_retried(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        return json.dumps({
            "status": "blocked", "url": "https://job.mokahr.com/abc",
            "mode": "parallel-fetch", "reason": "captcha wall",
        })

    results = browse_fetch_urls(
        ["https://job.mokahr.com/abc"], runner=fake_runner, out_dir=str(tmp_path)
    )
    # a login/captcha/anti-bot block is terminal: never retried, never falls
    # back to search-interact
    assert len(calls) == 1
    result = results[0]
    assert result.status == "blocked"
    assert result.error_code == "blocked"
    assert result.blocked_reason == "captcha wall"
    assert result.page_files == []


def test_error_retried_once_with_wait_5000(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        if "--wait 5000" in cli_args:  # the retried list-mode invocation
            (tmp_path / "pages").mkdir(exist_ok=True)
            (tmp_path / "pages" / "page_01.txt").write_text("职位 甲", encoding="utf-8")
            return json.dumps({
                "status": "ok", "url": "https://jobs.feishu.cn/abc", "mode": "list",
                "title": "职位", "content_hash": "sha256_1",
                "page_files": ["pages/page_01.txt"], "page_count": 1,
                "text_length": 100, "terminal_evidence": "finite_list_exhausted",
            })
        return json.dumps({
            "status": "error", "url": "https://jobs.feishu.cn/abc",
            "error": "Timeout loading page (30s)",
        })

    results = browse_fetch_urls(
        ["https://jobs.feishu.cn/abc"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(calls) == 2
    assert "--wait 5000" not in calls[0]
    assert "--wait 5000" in calls[1]
    # LIST class browsed with list mode and the feishu max-pages ceiling
    assert "--mode list" in calls[0]
    assert "--max-pages 3" in calls[0]
    assert results[0].status == "succeeded"


def test_list_mode_terminal_evidence_and_cache_passthrough(tmp_path) -> None:
    recorded: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        assert script == "browse"
        recorded.append(cli_args)
        (tmp_path / "pages").mkdir(exist_ok=True)
        (tmp_path / "pages" / "page_01.txt").write_text("职位 乙", encoding="utf-8")
        return json.dumps({
            "status": "ok", "url": "https://jobs.feishu.cn/abc", "mode": "list",
            "title": "公司", "content_hash": "sha256_2",
            "page_files": ["pages/page_01.txt"], "page_count": 2,
            "text_length": 2000, "terminal_evidence": "page_content_repeated",
        })

    results = browse_fetch_urls(
        ["https://jobs.feishu.cn/abc"],
        runner=fake_runner, out_dir=str(tmp_path), cache_mode="use",
    )
    assert "--cache-mode use" in recorded[0]
    assert "--mode list" in recorded[0]
    result = results[0]
    assert result.status == "succeeded"
    assert result.terminal_evidence == ["page_content_repeated"]
    assert result.cached is False


def test_wechat_urls_not_browsed(tmp_path) -> None:
    def fake_runner(script, *, cli_args="", stdin=""):
        raise AssertionError("wechat URLs must never invoke browse")

    results = browse_fetch_urls(
        ["https://mp.weixin.qq.com/s/AbC123", "https://weixin.qq.com/s/xyz"],
        runner=fake_runner, out_dir=str(tmp_path),
    )
    assert len(results) == 2
    for result in results:
        assert result.status == "wechat_pending"
        assert result.mode is None
        assert result.page_files == []
        assert result.site_class == SiteClass.WECHAT.value


def test_parallel_and_search_interact_hard_limits(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        return json.dumps({
            "status": "error", "url": "https://job.mokahr.com/abc", "error": "boom",
        })

    results = browse_fetch_urls(
        ["https://job.mokahr.com/abc"], runner=fake_runner, out_dir=str(tmp_path)
    )
    parallel = [c for c in calls if "--mode parallel-fetch" in c]
    search = [c for c in calls if "--mode search-interact" in c]
    # hard per-URL caps: at most one invocation of each capped mode across the
    # whole chain, even under repeated errors (no --wait retries for them)
    assert len(parallel) == 1
    assert len(search) == 1
    assert len(calls) == 2
    result = results[0]
    assert result.status == "failed"
    assert result.error_code == "browse_error"


def test_missing_page_file_dropped_without_crash(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    existing = tmp_path / "pages" / "page_01.txt"
    existing.write_text("职位 丙", encoding="utf-8")

    def fake_runner(script, *, cli_args="", stdin=""):
        return json.dumps({
            "status": "ok", "url": "https://job.mokahr.com/abc", "mode": "parallel-fetch",
            "content_hash": "sha256_x",
            "page_files": [
                str(existing),
                str(tmp_path / "pages" / "missing_abs.txt"),
                "pages/missing_rel.txt",
            ],
            "page_count": 1, "used_path": "parallel", "text_length": 100,
        })

    results = browse_fetch_urls(
        ["https://job.mokahr.com/abc"], runner=fake_runner, out_dir=str(tmp_path)
    )
    result = results[0]
    assert result.status == "succeeded"
    # files missing on disk are dropped from page_files, never crash
    assert len(result.page_files) == 1
    assert result.page_files[0].path == str(existing)


def test_unparsable_output_treated_as_error_and_retried(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        if len(calls) == 1:
            return "not json at all"
        return json.dumps({
            "status": "error", "url": "https://unknown.example.com/x", "error": "still failing",
        })

    results = browse_fetch_urls(
        ["https://unknown.example.com/x"], runner=fake_runner, out_dir=str(tmp_path)
    )
    # list-mode error -> one retry with --wait 5000 -> second error -> fallback
    # to search-interact -> cap -> failed
    assert len(calls) == 3
    assert "--wait 5000" in calls[1]
    assert sum("--mode search-interact" in c for c in calls) == 1
    assert results[0].status == "failed"
    assert results[0].error_code == "browse_error"


def test_non_dict_output_treated_as_error(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        return "[]"

    results = browse_fetch_urls(
        ["https://unknown.example.com/y"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(calls) == 3  # list x2 (retry once), search-interact x1 (capped)
    assert "--wait 5000" in calls[1]
    assert "--wait 5000" not in calls[2]
    assert results[0].status == "failed"


def test_cached_list_result_is_terminal_success(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        # exact browse.py cache-hit contract: no mode/page_files/page_count
        return json.dumps({
            "status": "ok", "url": "https://jobs.feishu.cn/abc",
            "content_hash": "sha256_cached", "text_path": "output/evidence/run-0/xyz.txt",
            "text_length": 300, "cached": True,
        })

    results = browse_fetch_urls(
        ["https://jobs.feishu.cn/abc"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(calls) == 1  # a cache hit is a completed prior list render, not thin
    result = results[0]
    assert result.cached is True
    assert result.status == "succeeded"
    assert result.page_files == []
    assert result.mode == "list"


def test_parallel_fetch_page_count_zero_falls_back(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        if "--mode parallel-fetch" in cli_args:
            return json.dumps({
                "status": "ok", "url": "https://jobs.bytedance.com/x", "mode": "parallel-fetch",
                "content_hash": "sha256_1", "page_files": [], "page_count": 0,
                "used_path": "parallel", "text_length": 0,
            })
        (tmp_path / "pages").mkdir(exist_ok=True)
        (tmp_path / "pages" / "page_01.txt").write_text("职位 丁", encoding="utf-8")
        return json.dumps({
            "status": "ok", "url": "https://jobs.bytedance.com/x", "mode": "search-interact",
            "content_hash": "sha256_2", "page_files": ["pages/page_01.txt"],
            "page_count": 1, "text_length": 100,
        })

    results = browse_fetch_urls(
        ["https://jobs.bytedance.com/x"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(calls) == 2
    assert results[0].mode == "search-interact"
    assert results[0].status == "succeeded"


def test_parallel_fetch_click_fallback_used_path_falls_back(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        if "--mode parallel-fetch" in cli_args:
            return json.dumps({
                "status": "ok", "url": "https://job.mokahr.com/abc", "mode": "parallel-fetch",
                "content_hash": "sha256_1", "page_files": [], "page_count": 1,
                "used_path": "click_fallback_fetch_error (TimeoutError)", "text_length": 10,
            })
        (tmp_path / "pages").mkdir(exist_ok=True)
        (tmp_path / "pages" / "page_01.txt").write_text("职位 戊", encoding="utf-8")
        return json.dumps({
            "status": "ok", "url": "https://job.mokahr.com/abc", "mode": "search-interact",
            "content_hash": "sha256_2", "page_files": ["pages/page_01.txt"],
            "page_count": 1, "text_length": 100,
        })

    results = browse_fetch_urls(
        ["https://job.mokahr.com/abc"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(calls) == 2
    assert results[0].mode == "search-interact"
    assert results[0].status == "succeeded"


def test_list_thin_page_count_zero_falls_back(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        if "--mode list" in cli_args:
            return json.dumps({
                "status": "ok", "url": "https://xiaopeng.jobs.feishu.cn/s/xyz", "mode": "list",
                "content_hash": "sha256_1", "page_files": [], "page_count": 0,
                "text_length": 0,
            })
        (tmp_path / "pages").mkdir(exist_ok=True)
        (tmp_path / "pages" / "page_01.txt").write_text("职位 辛", encoding="utf-8")
        return json.dumps({
            "status": "ok", "url": "https://xiaopeng.jobs.feishu.cn/s/xyz", "mode": "search-interact",
            "content_hash": "sha256_2", "page_files": ["pages/page_01.txt"],
            "page_count": 1, "text_length": 100,
        })

    results = browse_fetch_urls(
        ["https://xiaopeng.jobs.feishu.cn/s/xyz"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(calls) == 2
    assert results[0].mode == "search-interact"
    assert results[0].status == "succeeded"


def test_probe_thin_list_falls_back_to_search_interact(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        if "--mode list" in cli_args:
            return json.dumps({
                "status": "ok", "url": "https://unknown.example.com/list", "mode": "list",
                "content_hash": "sha256_1", "page_files": [], "page_count": 1,
                "text_length": 200,  # < 4096 -> thin probe
            })
        (tmp_path / "pages").mkdir(exist_ok=True)
        (tmp_path / "pages" / "page_01.txt").write_text("职位 己", encoding="utf-8")
        return json.dumps({
            "status": "ok", "url": "https://unknown.example.com/list", "mode": "search-interact",
            "content_hash": "sha256_2", "page_files": ["pages/page_01.txt"],
            "page_count": 1, "text_length": 5000,
        })

    results = browse_fetch_urls(
        ["https://unknown.example.com/list"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(calls) == 2
    assert results[0].mode == "search-interact"
    assert results[0].status == "succeeded"


def test_probe_rich_list_succeeds_without_fallback(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        (tmp_path / "pages").mkdir(exist_ok=True)
        (tmp_path / "pages" / "page_01.txt").write_text("x" * 5000, encoding="utf-8")
        return json.dumps({
            "status": "ok", "url": "https://unknown.example.com/careers", "mode": "list",
            "content_hash": "sha256_1", "page_files": ["pages/page_01.txt"],
            "page_count": 1, "text_length": 5000,
        })

    results = browse_fetch_urls(
        ["https://unknown.example.com/careers"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(calls) == 1
    result = results[0]
    assert result.status == "succeeded"
    assert result.mode == "list"
    assert "--cache-mode use" in calls[0]


def test_search_interact_class_success(tmp_path) -> None:
    calls: list[str] = []

    def fake_runner(script, *, cli_args="", stdin=""):
        calls.append(cli_args)
        (tmp_path / "pages").mkdir(exist_ok=True)
        (tmp_path / "pages" / "page_01.txt").write_text("职位 庚", encoding="utf-8")
        return json.dumps({
            "status": "ok", "url": "https://www.zhipin.com/job/1", "mode": "search-interact",
            "content_hash": "sha256_1", "page_files": ["pages/page_01.txt"],
            "page_count": 1, "text_length": 100,
            "terminal_evidence": "detail_links_exhausted",
        })

    results = browse_fetch_urls(
        ["https://www.zhipin.com/job/1"], runner=fake_runner, out_dir=str(tmp_path)
    )
    assert len(calls) == 1
    result = results[0]
    assert result.status == "succeeded"
    assert result.terminal_evidence == ["detail_links_exhausted"]
    assert "--cache-mode" not in calls[0]  # cache-mode is passed only for list mode


def test_default_runner_used_when_none(monkeypatch, tmp_path) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.browse_fetch as bf

    recorded: list[str] = []

    def fake_run_skill_script(script, *, cli_args="", stdin="", runner=None):
        assert script == "browse"
        assert runner is None  # no custom runner -> the allowlisted default
        recorded.append(cli_args)
        return json.dumps({
            "status": "error", "url": "https://unknown.example.com/z", "error": "no",
        })

    monkeypatch.setattr(bf, "run_skill_script", fake_run_skill_script)
    results = browse_fetch_urls(["https://unknown.example.com/z"], out_dir=str(tmp_path))
    # PROBE: list error -> retried once with --wait 5000 -> second error ->
    # single search-interact fallback -> cap -> failed
    assert len(recorded) == 3
    assert "--wait 5000" in recorded[1]
    assert "--wait 5000" not in recorded[2]
    assert sum("--mode search-interact" in c for c in recorded) == 1
    assert results[0].status == "failed"
