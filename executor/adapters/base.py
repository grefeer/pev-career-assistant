"""SiteAdapter Protocol and supporting data types.

Each recruitment site has its own adapter implementing this Protocol.
The executor engine delegates field fill, page classification, repeat
section handling, attachment upload, and blocker detection to the adapter.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from playwright.sync_api import Page


# ── Page classification constants ────────────────────────────────────────────────


class PageClass:
    SINGLE_PAGE = "single_page"
    MULTI_PAGE_FIRST = "multi_page_first"
    MULTI_PAGE_MIDDLE = "multi_page_middle"
    MULTI_PAGE_LAST = "multi_page_last"
    LOGIN_GATE = "login_gate"
    CAPTCHA_GATE = "captcha_gate"
    UNKNOWN = "unknown"


# ── Dataclasses returned by adapter methods ──────────────────────────────────────


@dataclass
class PageFingerprint:
    """Stable, redact-safe page identifier used for topology classification
    and checkpoint validation.  The dom_hash MUST be computed from a
    simplified DOM tree (tags, stable attributes, control types, layout)
    and MUST NOT include user input values, resume text, cookies, or the
    full DOM."""

    url_pattern: str
    dom_hash: str  # SHA-256 hex of simplified DOM structure
    page_index: int | None = None  # None = unknown
    total_pages: int | None = None
    has_submit_button: bool = False
    has_ambiguous_button: bool = False
    fields_detected: list[str] = field(default_factory=list)


@dataclass
class FillResult:
    """Result of filling a single field on a recruitment page."""

    field_key: str
    strategy: str  # "direct_input" | "select" | "date_picker" | "rich_text" | "deferred" | ...
    value_written: str
    readback_match: bool
    readback_value: str | None = None
    confidence: float = 1.0  # 0.0–1.0


@dataclass
class RepeatSectionResult:
    """Result of handling a repeatable section (education history, work
    experience, project experience).  Must guarantee no duplicate entries
    after recovery from crash or network interruption."""

    section_key: str
    entries_before: int  # entries present before this fill
    entries_after: int   # entries present after this fill
    entries_added: int
    dedup_verified: bool  # readback confirmed no duplicates


@dataclass
class UploadResult:
    """Result of uploading a file (resume, portfolio) to a recruitment site."""

    field_key: str
    file_name: str
    success: bool
    server_response_indicator: str | None = None


@dataclass
class BlockerInfo:
    """Describes a page state that prevents automated filling.

    The engine reports this as waiting_for_human and the user resolves
    it manually (login, captcha, etc.)."""

    blocker_type: str  # "login" | "captcha" | "risk_warning" | "page_changed" | "unknown_button"
    detail: str
    requires_human: bool = True


# ── SiteAdapter Protocol ─────────────────────────────────────────────────────────


@runtime_checkable
class SiteAdapter(Protocol):
    """Protocol that every recruitment-site adapter must implement.

    The adapter is responsible for understanding a specific site's DOM
    structure, control conventions, and navigation flow.  The executor
    engine calls these methods and delegates all site-specific logic.

    Safety invariants (enforced by the engine, not the adapter):
      - Final submit buttons are NEVER auto-clicked.
      - Combined save-and-submit buttons are NEVER auto-clicked.
      - Ambiguous buttons on unknown pages are NEVER auto-clicked.
    """

    adapter_id: str
    supported_domains: list[str]
    version: str  # semver, e.g. "1.0.0"

    def fingerprint_page(self, page: Page) -> PageFingerprint:
        """Build a stable, redacted fingerprint of the current page.

        The dom_hash must be computed from a simplified DOM tree — tags,
        stable attributes, control types, and page layout only.  Never
        include user input, resume text, cookies, or the full HTML body.
        """
        ...

    def classify_topology(self, fp: PageFingerprint) -> str:
        """Classify the page into one of the PageClass constants.

        Used by the engine to decide whether to fill fields, navigate to
        the next page, or stop at the final preview.
        """
        ...

    def fill_field(self, page: Page, field_key: str, value: str) -> FillResult:
        """Fill a single field and read back the written value.

        The adapter selects the appropriate strategy (direct input, select,
        date picker, rich text, etc.) based on the field_key and the
        current page's DOM.
        """
        ...

    def handle_repeat_section(
        self, page: Page, section_key: str, entries: list[dict[str, str]],
    ) -> RepeatSectionResult:
        """Add entries to a repeatable section (e.g. education history).

        Must count existing entries before adding and verify no duplicates
        after writing.  Checkpoint-aware: entries whose stable signatures
        already appear in the checkpoint are not re-added.
        """
        ...

    def upload_attachment(
        self, page: Page, field_key: str, file_path: str,
    ) -> UploadResult:
        """Upload a file to a file-input field on the current page.

        Uses Playwright's set_input_files() — never opens a system dialog.
        Waits for server acknowledgement before reporting success.
        """
        ...

    def detect_blocker(self, page: Page) -> BlockerInfo | None:
        """Check whether the page shows a login gate, captcha, risk warning,
        or other blocker that requires human intervention.

        Returns None if the page is fillable.
        """
        ...

    def save_page_progress(self, page: Page) -> bool:
        """Click the site's save/draft button if present.

        Returns True if a save action was triggered, False if no save
        button was found on the current page.
        """
        ...


# ── ObservationSiteAdapter Protocol ──────────────────────────────────────────────


@runtime_checkable
class ObservationSiteAdapter(Protocol):
    """Read-only protocol for sites under observation.

    Observation adapters can classify pages and extract field candidates
    but CANNOT fill fields, handle repeat sections, or upload attachments.
    They are NOT registered as executable adapters and cannot be used to
    create application tasks.
    """

    adapter_id: str
    supported_domains: list[str]
    version: str

    def fingerprint_page(self, page: Page) -> PageFingerprint:
        ...

    def classify_topology(self, fp: PageFingerprint) -> str:
        ...

    def detect_blocker(self, page: Page) -> BlockerInfo | None:
        ...

    def extract_field_candidates(self, page: Page) -> list[dict[str, str]]:
        """Return a list of detected fields with their types and selectors.

        Each dict has keys: field_key, label, control_type, selector, required.
        """
        ...


# ── Shared fingerprint helpers ───────────────────────────────────────────────────


def _dom_structure_hash(page: Page) -> str:
    """Compute a SHA-256 hash of a simplified DOM structure.

    Collects tag names, stable attributes (id, name, data-*, aria-*),
    and control types, but NOT text content or attribute values that
    could contain PII.
    """
    elements = page.locator(
        "input, select, textarea, button, [data-field-key], [contenteditable]"
    )
    parts: list[str] = []
    for i in range(elements.count()):
        el = elements.nth(i)
        tag = el.evaluate("el => el.tagName.toLowerCase()")
        elem_id = el.evaluate("el => el.id || ''")
        name = el.evaluate("el => el.getAttribute('name') || ''")
        field_type = el.evaluate("el => el.type || ''")
        data_field_key = el.evaluate(
            "el => el.getAttribute('data-field-key') || ''"
        )
        action_kind = el.evaluate(
            "el => el.getAttribute('data-action-kind') || ''"
        )
        parts.append(
            f"{tag}|id={elem_id}|name={name}|type={field_type}"
            f"|dfk={data_field_key}|ak={action_kind}"
        )
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
