"""Pre-check the iguopin seed URLs in the PEV eval seed bank.

Phase C (P1): iguopin seeds moved from the SPA homepage shell to keyword
search pages (https://www.iguopin.com/job/list?keyword=<kw>). This script
probes every iguopin seed through the SAME pipeline the eval uses --
``fetch_public_job_page`` (playwright fallback enabled in --render mode) then
``extract_jd_candidates`` -- and reports reachability plus per-job card
counts, so a broken or empty seed is caught before an eval round instead of
surfacing as a waiting_user.

The seed bank lives in tests/question/eval_runner.py (``SEED_URLS``); this
script imports it so it always validates what the eval will actually inject.

Usage::

    python tests/manual/iguopin_seed_precheck.py              # requests fast path only
    python tests/manual/iguopin_seed_precheck.py --render     # full pipeline (playwright fallback)
    python tests/manual/iguopin_seed_precheck.py --render --ids Q034 R015

Exit code 1 when any seed fails or renders zero job cards.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

# Allow direct invocation: ``python tests/manual/iguopin_seed_precheck.py``
# puts tests/manual on sys.path; the repo root must lead so ``backend`` and
# ``tests`` packages resolve.
ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills import job_discovery as jd_skill
from backend.app.services.career_skills.job_discovery import (
    FetchPublicJobPageInput,
    PublicJobFetchError,
)
from backend.app.services.job_discovery.tools.jd_extraction import extract_jd_candidates
from tests.question.eval_runner import SEED_URLS

PREVIEW_LIMIT = 3


def _fast_path(url: str) -> str:
    """requests fast path only; returns an outcome string (ok / error code)."""
    try:
        result = jd_skill.fetch_public_job_page(
            ToolContext(user_id="iguopin-precheck", run_id="iguopin-precheck"),
            FetchPublicJobPageInput(url=url),
        )
        return f"ok ({len(result.visible_text)} chars, no render)"
    except PublicJobFetchError as error:
        return error.code


def _render_path(url: str) -> tuple[str, int, list[str]]:
    """Full pipeline with playwright fallback; returns (outcome, cards, titles)."""
    try:
        result = jd_skill.fetch_public_job_page(
            ToolContext(user_id="iguopin-precheck", run_id="iguopin-precheck"),
            FetchPublicJobPageInput(url=url),
        )
    except PublicJobFetchError as error:
        return f"render failed: {error.code}", 0, []
    candidates = extract_jd_candidates(result.visible_text, url)
    titles = [
        candidate.title or candidate.company_name or "(untitled)"
        for candidate in candidates[:PREVIEW_LIMIT]
    ]
    return f"ok ({len(result.visible_text)} chars)", len(candidates), titles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render", action="store_true", help="enable playwright fallback (full pipeline)"
    )
    parser.add_argument(
        "--ids",
        nargs="+",
        default=None,
        help="only check these question ids (default: all iguopin-seeded ids)",
    )
    args = parser.parse_args()

    iguopin_urls: dict[str, list[str]] = {}
    for qid, (urls, _note) in SEED_URLS.items():
        if "iguopin.com" in " ".join(urls):
            iguopin_urls[qid] = urls
    if args.ids:
        iguopin_urls = {
            qid: urls for qid, urls in iguopin_urls.items() if qid in args.ids
        }
    if not iguopin_urls:
        print("no iguopin-seeded questions found in SEED_URLS")
        sys.exit(1)

    if args.render:
        jd_skill.enable_playwright_fallback(True)
    print(f"pre-checking {len(iguopin_urls)} iguopin-seeded questions "
          f"({'full pipeline' if args.render else 'requests fast path only'})")

    # Render each distinct URL once; per-id rows just reference the result.
    render_cache: dict[str, tuple[str, int, list[str]]] = {}
    failures: list[str] = []
    for qid, urls in sorted(iguopin_urls.items()):
        for url in urls:
            if url in render_cache:
                outcome, cards, titles = render_cache[url]
            else:
                if args.render:
                    outcome, cards, titles = _render_path(url)
                else:
                    outcome, cards, titles = _fast_path(url), -1, []
                render_cache[url] = (outcome, cards, titles)
            if args.render:
                # Full-pipeline gate: the seed must render usable job cards.
                verdict = "PASS" if cards > 0 else "FAIL"
            else:
                # Fast path only classifies: iguopin is a CRA SPA, so an
                # empty-shell error here is expected (render mode is the actual
                # gate); anything else is a real connectivity failure.
                verdict = (
                    "SHELL"
                    if outcome.startswith("public_") or outcome.startswith("empty")
                    else "FAIL"
                )
            if verdict == "FAIL":
                failures.append(f"{qid}: {url}")
            print(f"{verdict} {qid}: {outcome}"
                  + (f", {cards} job cards" if cards >= 0 else "")
                  + (" | " + " / ".join(titles) if titles else ""))
            print(f"    {url}")

    if failures:
        print(f"\n{len(failures)} seed(s) failed:")
        for line in failures:
            print(f"  - {line}")
        sys.exit(1)
    print("\nall iguopin seeds pass")


if __name__ == "__main__":
    main()
