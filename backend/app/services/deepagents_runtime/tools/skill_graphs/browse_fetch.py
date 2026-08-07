"""browse.py-backed fetch layer for the job-discovery subgraph (Task 7).

Implements the SKILL.md Phase 2 classification table + the fallback chain:
site classification -> mode selection -> ``browse`` orchestration with hard
per-URL caps -> URL-keyed cache passthrough (list mode only) -> terminal
evidence.  Every browse call goes through the allowlisted
``run_skill_script("browse", ...)`` channel (subprocess_runner.py); the
``runner`` seam keeps unit tests deterministic without Playwright.

Output contract mirrors the real ``skill/job-discovery/scripts/browse.py``:
- status ``ok`` / ``error`` (with ``error`` str) / ``blocked`` (reason under
  ``reason`` or ``error``) / ``empty`` (treated as an error here);
- thin-result markers under ``used_path``:
  ``spa_shell_empty_no_evidence`` (blocked-status 0-char SPA shell) and
  ``click_fallback_fetch_error ...`` (parallel-fetch serial-click fallback
  failed) both route to the single ``search-interact`` fallback;
- cache hits carry ``cached: true`` and omit ``mode``/``page_files``;
- page files are hashed as full ``sha256(file bytes).hexdigest()`` — the
  manifest/evidence hash.  browse's own ``sha256_<16>`` ``content_hash`` is
  never used as evidence hash.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner import (
    SKILL_DIR,
    run_skill_script,
)

#: Stable evidence directory used when no run-specific out_dir is supplied
#: (the graph's fetch seam has no run_id; a future wiring can pass a
#: ``output/evidence/run-<run_id>`` dir explicitly).
_DEFAULT_OUT_DIR = str(SKILL_DIR / "output" / "evidence" / "run-0")

#: SKILL.md Phase 2: a probe preview under 4096 chars is a thin listing that
#: warrants the single search-interact fallback.
_THIN_LIST_TEXT_LENGTH = 4096

#: site-catalog.md: jobs.feishu.cn is fully covered by 3 list pages.
_FEISHU_MAX_PAGES = 3

#: Modes with a hard per-URL cap of ONE invocation across the whole chain.
_CAP_ONCE_MODES = frozenset({"parallel-fetch", "search-interact"})

#: browse.py marks a 0-char SPA shell (blocked status) and a failed parallel
#: fetch's serial-click fallback via these used_path markers; both mean the
#: URL needs the search-interact fallback, not a hard block.
_THIN_USED_PATHS = ("spa_shell_empty_no_evidence", "click_fallback_fetch_error")

#: browse.py cache-hit results carry no mode/page_files/page_count keys; the
#: ``cached`` flag alone marks them, and a hit is a terminal success.


class SiteClass(str, Enum):
    """Phase 2 classification of a career URL (SKILL.md table + catalog)."""

    WECHAT = "wechat"
    PARALLEL_FETCH = "parallel_fetch"
    LIST = "list"
    SEARCH_INTERACT = "search_interact"
    PROBE = "probe"


@dataclass
class PageFile:
    """One page file on disk with its full-sha256 evidence identity."""

    path: str
    content_hash: str
    text_length: int


@dataclass
class UrlFetchResult:
    """Per-URL outcome of the browse fallback chain."""

    url: str
    site_class: str
    mode: str | None
    status: str
    used_path: str | None = None
    page_files: list[PageFile] = field(default_factory=list)
    terminal_evidence: list[str] = field(default_factory=list)
    cached: bool = False
    title: str | None = None
    blocked_reason: str | None = None
    error_code: str | None = None


#: Sentinel treated exactly like a browse ``status=error`` output: browse
#: stdout that is not a JSON object is a broken invocation.
_ERROR_OUTPUT: dict[str, Any] = {"status": "error", "error": "unparsable browse output"}


def classify_url(url: str) -> SiteClass:
    """Classify a career URL by host (SKILL.md Phase 2 table, pure)."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "weixin.qq.com" or host.endswith(".weixin.qq.com"):
        return SiteClass.WECHAT
    if (
        host.endswith(".mokahr.com")
        or host.endswith(".bytedance.com")
        or host.endswith(".mioffice.cn")
    ):
        return SiteClass.PARALLEL_FETCH
    if host == "jobs.feishu.cn" or host.endswith(".jobs.feishu.cn"):
        return SiteClass.LIST
    if host.endswith(".zhipin.com") or host.endswith(".zhiye.com"):
        return SiteClass.SEARCH_INTERACT
    return SiteClass.PROBE


def mode_for_class(site_class: SiteClass, *, probe: dict | None = None) -> str | None:
    """Primary browse mode for a site class.

    ``probe`` is the SKILL.md Phase 2 probe-decision seam; today every
    non-WeChat class maps straight to its table mode (PROBE starts with
    ``list`` and lets the text-length rule trigger the fallback).
    """
    if site_class is SiteClass.WECHAT:
        return None  # WeChat articles are never browsed here (ReadGZH domain)
    if site_class is SiteClass.PARALLEL_FETCH:
        return "parallel-fetch"
    if site_class is SiteClass.SEARCH_INTERACT:
        return "search-interact"
    return "list"  # LIST and PROBE both probe with list mode


def page_file_hash(path: str, *, out_dir: str) -> tuple[str, int]:
    """Full sha256 of a page file's bytes + its byte length.

    This is the manifest/evidence hash: browse.py's own short
    ``sha256_<16>`` content_hash is never used as evidence identity.
    """
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path(out_dir) / resolved
    data = resolved.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _chain_modes(site_class: SiteClass) -> list[str]:
    """Ordered candidate modes for the fallback chain of a site class."""
    if site_class is SiteClass.SEARCH_INTERACT:
        return ["search-interact"]
    if site_class is SiteClass.PARALLEL_FETCH:
        return ["parallel-fetch", "search-interact"]
    return ["list", "search-interact"]  # LIST and PROBE


def _wrap_runner(runner: Callable[..., str] | None) -> Callable[..., str] | None:
    """Adapt the simple ``(script, *, cli_args, stdin)`` seam to the
    ``run_skill_script`` runner contract (script_path, parts, cwd, ...)."""
    if runner is None:
        return None

    def adapted(
        script_path: Path,
        parts: list[str],
        *,
        cwd: Path,
        stdin: str | None,
        timeout: int,
    ) -> str:
        # the simple seam addresses scripts by allowlisted name ("browse")
        return runner(script_path.stem, cli_args=shlex.join(parts), stdin=stdin or "")

    return adapted


def _build_cli(
    url: str,
    mode: str,
    *,
    out_dir: str,
    cache_mode: str,
    wait_ms: int | None,
    max_pages: int | None,
) -> str:
    """Assemble the allowlisted ``browse`` CLI args (--cache-mode is list-only)."""
    parts = [url, "--mode", mode, "--out", out_dir]
    if max_pages is not None:
        parts += ["--max-pages", str(max_pages)]
    if mode == "list":
        parts += ["--cache-mode", cache_mode]
    if wait_ms is not None:
        parts += ["--wait", str(wait_ms)]
    return shlex.join(parts)


def _run_browse(
    url: str,
    mode: str,
    *,
    runner: Callable[..., str] | None,
    out_dir: str,
    cache_mode: str,
    wait_ms: int | None,
    max_pages: int | None,
) -> dict[str, Any]:
    """Run one browse invocation; never raises, never trusts bad stdout."""
    cli = _build_cli(
        url, mode, out_dir=out_dir, cache_mode=cache_mode,
        wait_ms=wait_ms, max_pages=max_pages,
    )
    raw = run_skill_script("browse", cli_args=cli, runner=_wrap_runner(runner))
    try:
        parsed = json.loads(raw)
    except ValueError:
        return dict(_ERROR_OUTPUT)
    if not isinstance(parsed, dict):
        return dict(_ERROR_OUTPUT)
    return parsed


def _collect_page_files(output: dict[str, Any], out_dir: str) -> list[PageFile]:
    """Resolve browse's page_files against out_dir, hashing each on disk.

    A page file missing on disk is dropped (never crashes); browse's short
    ``sha256_<16>`` content_hash is not used.
    """
    page_files: list[PageFile] = []
    for relative in output.get("page_files") or []:
        path = Path(relative)
        if not path.is_absolute():
            path = Path(out_dir) / path
        if path.exists():
            digest, size = page_file_hash(str(path), out_dir=out_dir)
            page_files.append(PageFile(path=str(path), content_hash=digest, text_length=size))
    return page_files


def _is_thin(site_class: SiteClass, mode: str, output: dict[str, Any]) -> bool:
    """Whether a successful browse produced no usable listing evidence."""
    if mode == "list":
        if site_class is SiteClass.PROBE:
            return int(output.get("text_length") or 0) < _THIN_LIST_TEXT_LENGTH
        return int(output.get("page_count") or 0) == 0
    if mode == "parallel-fetch":
        return int(output.get("page_count") or 0) == 0
    return False  # search-interact is the terminal mode; its result is final


def _to_result(
    url: str,
    site_class: SiteClass,
    mode: str,
    output: dict[str, Any],
    *,
    status: str,
    page_files: list[PageFile] | None = None,
    cached: bool = False,
    blocked_reason: str | None = None,
    error_code: str | None = None,
) -> UrlFetchResult:
    terminal = output.get("terminal_evidence")
    return UrlFetchResult(
        url=url,
        site_class=site_class.value,
        mode=mode,
        status=status,
        used_path=output.get("used_path"),
        page_files=page_files if page_files is not None else [],
        terminal_evidence=[str(terminal)] if terminal else [],
        cached=cached,
        title=output.get("title"),
        blocked_reason=blocked_reason,
        error_code=error_code,
    )


def _browse_one_url(
    url: str,
    site_class: SiteClass,
    *,
    runner: Callable[..., str] | None,
    out_dir: str,
    cache_mode: str,
) -> UrlFetchResult:
    """Run the fallback chain for one URL with hard per-URL invocation caps.

    Chain rules (per URL):
    - ``parallel-fetch`` and ``search-interact`` are each invoked at most ONCE
      (error/empty results do NOT get the --wait retry — the cap is a hard
      ceiling, enforced by the invocation counter map);
    - ``list`` errors are retried once with ``--wait 5000``;
    - ``blocked`` is never retried and never falls back, except the
      parallel-fetch ``spa_shell_empty_no_evidence`` marker which is a
      thin-result trigger for the single search-interact fallback;
    - cache hits are terminal successes (browse only caches completed list
      renders, so a hit is never "thin").
    """
    invocations: dict[str, int] = {}
    for mode in _chain_modes(site_class):
        while True:
            count = invocations.get(mode, 0)
            if count >= 1 and mode in _CAP_ONCE_MODES:
                break  # hard cap: the capped modes are never invoked twice
            invocations[mode] = count + 1
            max_pages = _FEISHU_MAX_PAGES if (mode == "list" and site_class is SiteClass.LIST) else None
            output = _run_browse(
                url, mode, runner=runner, out_dir=out_dir, cache_mode=cache_mode,
                wait_ms=5000 if count >= 1 else None, max_pages=max_pages,
            )
            used_path = str(output.get("used_path") or "")
            if mode == "parallel-fetch" and any(
                used_path.startswith(marker) for marker in _THIN_USED_PATHS
            ):
                break  # thin parallel-fetch -> the single search-interact fallback
            status = str(output.get("status") or "error")
            if status == "blocked":
                return _to_result(
                    url, site_class, mode, output,
                    status="blocked",
                    blocked_reason=str(output.get("reason") or output.get("error") or "blocked"),
                    error_code="blocked",
                )
            if status == "ok":
                cached = bool(output.get("cached", False))
                page_files = _collect_page_files(output, out_dir)
                if not cached and _is_thin(site_class, mode, output):
                    break  # thin result -> next candidate mode
                return _to_result(
                    url, site_class, mode, output,
                    status="succeeded", page_files=page_files, cached=cached,
                )
            # status error / empty / unknown -> one retry for list mode; the
            # capped modes stop at the counter map on the next loop pass
            if count >= 1:
                break
    # every candidate exhausted without evidence
    return UrlFetchResult(
        url=url,
        site_class=site_class.value,
        mode=None,
        status="failed",
        error_code="browse_error",
    )


def browse_fetch_urls(
    urls: list[str],
    *,
    runner: Callable[..., str] | None = None,
    out_dir: str | None = None,
    cache_mode: str = "use",
) -> list[UrlFetchResult]:
    """Browse a batch of URLs through the classify -> mode -> fallback chain.

    ``runner=None`` runs the real allowlisted ``browse`` script;
    ``out_dir=None`` resolves page evidence under the stable
    ``output/evidence/run-0`` directory; ``cache_mode`` is forwarded to
    ``browse`` for list mode only (URL-keyed caching is list-only in
    browse.py).  WeChat URLs are reported ``wechat_pending`` without any
    browse call.
    """
    resolved_out_dir = out_dir or _DEFAULT_OUT_DIR
    results: list[UrlFetchResult] = []
    for url in urls:
        site_class = classify_url(url)
        mode = mode_for_class(site_class)
        if mode is None:  # WECHAT: never browsed here, carried for Task 9
            results.append(
                UrlFetchResult(
                    url=url,
                    site_class=site_class.value,
                    mode=None,
                    status="wechat_pending",
                )
            )
            continue
        results.append(
            _browse_one_url(
                url, site_class, runner=runner,
                out_dir=resolved_out_dir, cache_mode=cache_mode,
            )
        )
    return results
