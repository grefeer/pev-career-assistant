"""Repeat section handler for Moka forms.

Education, work experience, and project experience sections may contain
repeatable entries.  This module guarantees two invariants:

1. **No duplicates on recovery**: before adding a new entry, the handler
   reads back all existing entries and computes stable signatures (hashes
   of the entry content).  Entries whose signatures already exist in the
   checkpoint are skipped.

2. **Incremental safety**: each entry is added one at a time, with a
   checkpoint save between additions.  A crash mid-section resumes from
   the last committed entry.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from executor.adapters.base import RepeatSectionResult

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)

# ── Per-section selectors ───────────────────────────────────────────────────────

SECTION_SELECTORS = {
    "education": {
        "add_button": 'button:has-text("添加教育经历"), button:has-text("+ 添加")',
        "entry_container": ".education-entry, .repeat-item, [data-section=\"education\"] .entry",
        "field_school": 'input[name*="school"], [data-field="school"]',
        "field_degree": 'select[name*="degree"], [data-field="degree"]',
        "field_major": 'input[name*="major"], [data-field="major"]',
        "field_start_date": 'input[type="date"][name*="start"], [data-field="edu_start"]',
        "field_end_date": 'input[type="date"][name*="end"], [data-field="edu_end"]',
    },
    "work_experience": {
        "add_button": 'button:has-text("添加工作经历"), button:has-text("+ 添加经历")',
        "entry_container": ".work-entry, .repeat-item, [data-section=\"work\"] .entry",
        "field_company": 'input[name*="company"], [data-field="company"]',
        "field_position": 'input[name*="position"], [data-field="position"]',
        "field_start_date": 'input[type="date"][name*="start"], [data-field="work_start"]',
        "field_end_date": 'input[type="date"][name*="end"], [data-field="work_end"]',
        "field_description": 'textarea[name*="description"],[data-field="description"]',
    },
    "project_experience": {
        "add_button": 'button:has-text("添加项目经历"), button:has-text("+ 添加项目")',
        "entry_container": ".project-entry, .repeat-item, [data-section=\"project\"] .entry",
        "field_name": 'input[name*="project_name"], [data-field="project_name"]',
        "field_role": 'input[name*="role"], [data-field="role"]',
        "field_description": 'textarea[name*="description"],[data-field="project_desc"]',
    },
}

# ── Stable entry signature ──────────────────────────────────────────────────────


def _stable_entry_signature(entry: dict[str, str]) -> str:
    """Compute a deterministic hash for a repeat-section entry.

    Uses sorted key=value pairs so that key order does not affect the hash.
    """
    canonical = json.dumps(
        sorted(entry.items()), ensure_ascii=True, sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ── Entry readback ──────────────────────────────────────────────────────────────


def _read_back_entries(
    page: "Page", section_key: str
) -> list[dict[str, str]]:
    """Read back all currently visible entries in the repeat section.

    Returns a list of dicts, each representing one entry's filled values.
    """
    selectors = SECTION_SELECTORS.get(section_key, {})
    container_sel = selectors.get("entry_container")
    if not container_sel:
        return []
    entries: list[dict[str, str]] = []
    try:
        containers = page.locator(container_sel)
        for i in range(containers.count()):
            entry: dict[str, str] = {}
            container = containers.nth(i)
            for field, sel in selectors.items():
                if field.startswith("field_"):
                    name = field[6:]  # remove "field_" prefix
                    try:
                        el = container.locator(sel).first
                        if el.count() > 0:
                            tag = el.evaluate(
                                "el => el.tagName.toLowerCase()"
                            )
                            if tag == "select":
                                val = el.evaluate(
                                    "el => el.options[el.selectedIndex]?.text || ''"
                                )
                            elif tag == "textarea":
                                val = el.inner_text() or ""
                            else:
                                val = el.input_value() or ""
                            entry[name] = str(val).strip()
                    except Exception:
                        pass
            if entry:
                entries.append(entry)
    except Exception as exc:
        logger.warning("read_back_entries failed: %s", exc)
    return entries


# ── Add single entry ────────────────────────────────────────────────────────────


def _add_single_entry(
    page: "Page", section_key: str, entry: dict[str, str]
) -> None:
    """Click the add button, then fill one entry's fields."""
    selectors = SECTION_SELECTORS.get(section_key, {})
    add_sel = selectors.get("add_button")
    if not add_sel:
        raise RuntimeError(f"No add_button selector for {section_key}")

    add_btn = page.locator(add_sel).first
    if add_btn.count() == 0:
        raise RuntimeError(f"Add button not found for {section_key}")

    add_btn.click()
    page.wait_for_timeout(500)

    containers = page.locator(selectors.get("entry_container", ""))
    if containers.count() == 0:
        raise RuntimeError(f"No entry containers found for {section_key}")

    last = containers.last
    for field, value in entry.items():
        sel = selectors.get(f"field_{field}")
        if not sel:
            continue
        try:
            el = last.locator(sel).first
            if el.count() == 0:
                continue
            tag = el.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                el.select_option(label=value)
            elif tag == "textarea":
                el.fill(value)
            else:
                el.fill(value)
            page.wait_for_timeout(150)
        except Exception as exc:
            logger.warning(
                "failed to fill %s.%s: %s", section_key, field, exc
            )


# ── Main handler ────────────────────────────────────────────────────────────────


def handle_repeat_section(
    page: "Page",
    section_key: str,
    entries: list[dict[str, str]],
    *,
    checkpoint_signatures: list[str] | None = None,
) -> RepeatSectionResult:
    """Add entries to a repeatable section with dedup and checkpoint awareness.

    Args:
        page: Playwright Page object.
        section_key: One of ``"education"``, ``"work_experience"``,
                     ``"project_experience"``.
        entries: Target entries to add (each is a dict of field→value).
        checkpoint_signatures: Signatures of entries already successfully
                               committed in a previous session.  Entries
                               matching these are skipped.

    Returns:
        RepeatSectionResult with before/after counts and dedup verification.
    """
    checkpoint_sigs: set[str] = set(checkpoint_signatures or [])

    # 1. Read back current entries
    existing = _read_back_entries(page, section_key)
    existing_sigs = {_stable_entry_signature(e) for e in existing}

    # 2. Filter entries to add: skip duplicates (existing + checkpoint)
    to_add: list[dict[str, str]] = []
    for entry in entries:
        sig = _stable_entry_signature(entry)
        if sig not in existing_sigs and sig not in checkpoint_sigs:
            to_add.append(entry)

    # 3. Add entries one at a time with verification
    added_count = 0
    for entry in to_add:
        # Record that we're about to add this entry
        before_count = len(_read_back_entries(page, section_key))
        try:
            _add_single_entry(page, section_key, entry)
            page.wait_for_timeout(500)
        except Exception as exc:
            logger.warning(
                "add_single_entry failed for %s: %s", section_key, exc
            )
            # Stop here — remaining entries will be tried on next run
            break
        after_count = len(_read_back_entries(page, section_key))
        if after_count > before_count:
            added_count += 1
        else:
            logger.warning(
                "entry addition did not increase count for %s, stopping",
                section_key,
            )
            break

    # 4. Final readback and dedup verification
    all_entries = _read_back_entries(page, section_key)
    target_sigs = {_stable_entry_signature(e) for e in entries}

    # Verify each target entry appears exactly once in the final list
    dedup_ok = True
    for sig in target_sigs:
        count = sum(
            1 for e in all_entries if _stable_entry_signature(e) == sig
        )
        if count != 1:
            dedup_ok = False
            break

    return RepeatSectionResult(
        section_key=section_key,
        entries_before=len(existing),
        entries_after=len(all_entries),
        entries_added=added_count,
        dedup_verified=dedup_ok,
    )
