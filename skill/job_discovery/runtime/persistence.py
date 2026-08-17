"""Incremental persistence primitives for the job-discovery subgraph (Task 10).

Maps 1:1 to the skill's state/normalize scripts through the allowlisted
``run_skill_script`` seam: ``state_check``/``state_mark`` drive
``state.py check/mark`` (the stable ``output/state.json`` master index) and
``normalize_candidates`` drives ``normalize.py --json`` for comparison
keys.  ``load_prior_candidates`` reads the cumulative
``output/candidates/merged_final.json`` store and ``append_errors_jsonl``
accumulates recoverable hand-off entries (needs_deep_crawl /
needs_manual_review) at ``<state_dir>/output/errors.jsonl``.  All stores
live under a stable state dir (the P1-P9 incremental contract) — never in
temp dirs.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

from skill.job_discovery.runtime.subprocess_runner import (
    run_skill_script,
)


def _state_invoke(runner, state_dir: str):
    """Bind the state script to the run-scoped cwd (never the shared skill
    cwd) so each run's ``output/state.json`` lives under its own state dir.

    The graph always passes ``runner=script_runner``, which in production
    resolves to the module-level ``run_skill_script`` itself (the factory
    binds it into the graph closure — Task 11 review M1-1) — so the binding
    applies when the provided runner is None OR is exactly that function
    (identity match); any other runner (test fakes) keeps the plain 3-arg
    call.  Only state.py check/mark get the run-scoped cwd — coverage_gate's
    evidence-root containment must keep resolving against the shared skill
    cwd (``SKILL_DIR/output/evidence``).
    """
    if runner is None or runner is run_skill_script:
        return functools.partial(run_skill_script, cwd=state_dir)
    return runner


def state_check(url: str, update_time: str, *, runner=None, state_dir: str) -> bool:
    """True when the URL is already extracted at this update time (skip).

    ``state.py check`` exits 0 = skip / 1 = needs extraction; the runner
    seam's JSON carries ``exit_code`` (test seam) or the real script's
    ``action`` (``skip``/``extract``).  Unparsable or non-object output
    folds to False (extract) so a broken state never silently skips a URL.
    The real script runs with ``cwd=state_dir`` (run-scoped
    ``output/state.json``); test runners keep the plain 3-arg call.
    """
    out = _state_invoke(runner, state_dir)("state", cli_args=f"check {url} {update_time}")
    try:
        parsed = json.JSONDecoder().raw_decode(out, 0)[0]
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    exit_code = parsed.get("exit_code")
    if exit_code is not None:
        return exit_code == 0
    return parsed.get("action") == "skip"


def state_mark(
    url: str,
    content_hashes: list[str],
    *,
    runner=None,
    state_dir: str,
    file_id: str,
    sheet_id: str,
    update_time: str,
) -> None:
    """Mark the URL as processed — one ``mark`` call per content hash.

    Emits the real ``state.py mark`` form with three positionals —
    ``mark <content_hash> <url> <update_time> --file-id <f> --sheet-id <s>``.
    The script derives one entry_id per (content_hash, url) itself —
    ``entry_id = content_hash[:16]_url_hash8`` (its verified format) — so
    hashes are passed whole, never pre-truncated.  A blank ``file_id``,
    ``sheet_id`` or ``update_time`` raises ValueError (the real script marks
    the flags required, and a blank update_time collapses the positional —
    the mark would silently fail).  The real script runs with
    ``cwd=state_dir`` (run-scoped ``output/state.json``); test runners keep
    the plain 3-arg call.
    """
    if not file_id or not sheet_id:
        raise ValueError("state mark requires both file_id and sheet_id")
    if not update_time:
        raise ValueError("state mark requires update_time")
    for content_hash in content_hashes:
        _state_invoke(runner, state_dir)(
            "state",
            cli_args=(
                f"mark {content_hash} {url} {update_time} "
                f"--file-id {file_id} --sheet-id {sheet_id}"
            ),
        )


def load_prior_candidates(*, state_dir: str) -> list[dict]:
    """Load the cumulative ``output/candidates/merged_final.json`` store.

    Missing file, unparsable JSON, or an unexpected shape fold to [] (the
    dedup node then merges only the run's own candidates).
    """
    path = Path(state_dir) / "output" / "candidates" / "merged_final.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        candidates = raw.get("candidates")
        if isinstance(candidates, list):
            return candidates
    return []


def append_errors_jsonl(entry: dict, *, runner=None, state_dir: str) -> None:
    """Append one line to ``<state_dir>/output/errors.jsonl`` (idempotent).

    ``runner`` is accepted for interface parity with the skill-script seam;
    the file is written directly (shared helper ``_append_errors_jsonl_at``
    also serves the WeChat slice's single-shot out_dir mode).
    """
    _append_errors_jsonl_at(Path(state_dir) / "output" / "errors.jsonl", entry)


def _append_errors_jsonl_at(path: Path, entry: dict) -> None:
    """Idempotent JSONL append at an explicit path (shared with the slice).

    A duplicate (same ``url`` AND same ``cause``) is skipped, so retried
    runs never grow the file; unparsable or non-dict existing lines are
    ignored.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                existing = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(existing, dict)
                and existing.get("url") == entry.get("url")
                and existing.get("cause") == entry.get("cause")
            ):
                return
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def normalize_candidates(candidates: list[dict], *, runner=None) -> dict[str, str]:
    """Comparison-key map via ``normalize.py --json`` (keys only, per title).

    One ``normalize`` invocation per candidate title; the returned map
    merges the seam's ``normalized_title``/``key`` fields and — for the
    real script's ``{"input", "normalized"}`` contract — maps each
    original title to its normalized comparison key.  Stored titles are
    never altered (the script's contract: keys only).

    Titles are quoted so multi-word titles stay a single token for both
    the Windows (posix=False) and POSIX runners; a title containing a
    literal ``"`` degrades to a skipped key (the runner returns the parse
    ERROR string, raw_decode fails -> ``continue``) — never crashes.
    """
    runner = runner or run_skill_script
    keys: dict[str, str] = {}
    for candidate in candidates:
        title = candidate.get("title")
        if not title:
            continue
        out = runner("normalize", cli_args=f'--title "{title}" --json')
        try:
            parsed = json.JSONDecoder().raw_decode(out, 0)[0]
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        for field in ("normalized_title", "key"):
            value = parsed.get(field)
            if isinstance(value, str) and value:
                keys[field] = value
        normalized = parsed.get("normalized")
        if isinstance(normalized, str) and normalized:
            keys[title] = normalized
    return keys
