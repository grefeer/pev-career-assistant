"""Live, no-LLM probe for the job-discovery browser path.

This is the middle tier of the skill evaluation ladder: it verifies that a
site's pagination is fetched, the expected path is used, and all returned page
artifacts are nonempty. It never constructs an agent or reads an API key.

Run one site::

    $env:RUN_SKILL_BROWSE_PROBE='1'
    $env:SKILL_BROWSE_PROBE_ONLY='bytedance'
    .\\.venv\\Scripts\\python.exe tests/manual/run_skill_browse_probe.py

It is still a live browser operation, so it is explicitly gated. The saved
manifest contains hashes rather than page text and is evidence of browser
behavior only; it is not an extraction-quality PASS.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RUNNER_PATH = Path(__file__).with_name("run_skill_ten_url_eval.py")
_SKILL_DIR = _PROJECT_ROOT / "skill" / "job-discovery"
_OUT_DIR = Path(__file__).resolve().parent


def _load_urls() -> list[tuple[str, str, str, int | None]]:
    """Use the same URL catalog as the paid runner without duplicating it."""
    spec = importlib.util.spec_from_file_location("skill_ten_url_catalog", _RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load URL catalog from {_RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.URLS


def _parse_json_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("browse.py did not emit a JSON result")


def _validate_probe_result(result: dict[str, Any], *, required_path: str) -> list[str]:
    """Validate browser evidence without interpreting any job descriptions."""
    errors: list[str] = []
    if result.get("status") != "ok":
        errors.append(f"status must be 'ok', got {result.get('status')!r}")
    if required_path and result.get("used_path") != required_path:
        errors.append(
            f"used_path must be {required_path!r}, got {result.get('used_path')!r}"
        )
    page_files = result.get("page_files")
    if not isinstance(page_files, list) or not page_files:
        return [*errors, "page_files must be a nonempty list"]
    if result.get("page_count") != len(page_files):
        errors.append("page_count must equal the number of page_files")
    for page_file in page_files:
        path = Path(str(page_file))
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"page file is missing or empty: {path}")
    return errors


def _page_hashes(page_files: list[Any]) -> list[str]:
    return [
        hashlib.sha256(Path(str(page_file)).read_bytes()).hexdigest()
        for page_file in page_files
    ]


def _probe_one(
    slug: str,
    company: str,
    url: str,
    *,
    required_path: str,
) -> dict[str, Any]:
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="skill-browse-probe-") as temp_dir:
        cmd = [
            sys.executable,
            str(_SKILL_DIR / "scripts" / "browse.py"),
            url,
            "--mode", "parallel-fetch",
            "--max-pages", "20",
            "--concurrency", "4",
            "--out", temp_dir,
        ]
        proc = subprocess.run(
            cmd,
            cwd=_SKILL_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        try:
            result = _parse_json_result(proc.stdout)
            errors = _validate_probe_result(result, required_path=required_path)
            hashes = _page_hashes(result["page_files"]) if not errors else []
        except (OSError, ValueError, KeyError) as exc:
            result = {"status": "probe_error", "reason": str(exc)}
            errors = [str(exc)]
            hashes = []
    record = {
        "slug": slug,
        "company": company,
        "url": url,
        "evaluation_mode": "live_browser_no_llm",
        "required_path": required_path,
        "status": "passed" if proc.returncode == 0 and not errors else "failed",
        "browse_status": result.get("status"),
        "used_path": result.get("used_path"),
        "page_count": result.get("page_count"),
        "page_hashes": hashes,
        "errors": errors,
        "elapsed_sec": round(time.monotonic() - t0, 1),
    }
    (_OUT_DIR / f"_skill_browse_probe_{slug}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return record


def _main() -> int:
    if not os.environ.get("RUN_SKILL_BROWSE_PROBE"):
        print("SKIP: RUN_SKILL_BROWSE_PROBE is not set (no browser calls).")
        return 0
    only = {value.strip() for value in os.environ.get("SKILL_BROWSE_PROBE_ONLY", "").split(",") if value.strip()}
    limit = int(os.environ.get("SKILL_BROWSE_PROBE_LIMIT", "1"))
    required_path = os.environ.get("SKILL_BROWSE_PROBE_REQUIRED_PATH", "parallel")
    urls = _load_urls()
    if only:
        urls = [row for row in urls if row[0] in only]
    urls = urls[:limit]
    if not urls:
        print("ERROR: no URLs selected")
        return 2
    failures = 0
    for slug, company, url, _ in urls:
        record = _probe_one(slug, company, url, required_path=required_path)
        print(json.dumps(record, ensure_ascii=False))
        failures += record["status"] != "passed"
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
