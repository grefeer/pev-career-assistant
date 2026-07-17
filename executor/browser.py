from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from typing import Callable

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from executor.protocol import ExecutorField, FieldConfidence
from executor.safety import (
    PageTopology,
    SafetyDecision,
    classify_topology,
    decide_action,
)


@dataclass(frozen=True)
class PageObservation:
    topology: PageTopology
    page_index: int | None
    page_count: int | None
    fingerprint: str
    human_required: str | None
    action_label: str
    action_kind: str
    has_verified_next_step: bool


@dataclass(frozen=True)
class FillReport:
    confirmed_keys: list[str]
    missing_keys: list[str]
    low_confidence_keys: list[str]
    readback_mismatch_keys: list[str]
    defaulted_keys: list[str]


class UnsafeActionError(RuntimeError):
    pass


class IntermediateActionUncertainError(RuntimeError):
    pass


class BrowserSession:
    """Playwright-based browser automation for executor simulation."""

    def __init__(
        self,
        user_data_dir: str | Path | None = None,
        *,
        headless: bool = False,
        channel: str | None = "chrome",
        before_write: Callable[[str], None] | None = None,
        after_verified: Callable[[str], None] | None = None,
    ) -> None:
        self._headless = headless
        self._channel = channel
        self._before_write = before_write
        self._after_verified = after_verified
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._user_data_dir = str(user_data_dir) if user_data_dir else None

    def open(self, url: str) -> None:
        if self._context is None:
            self._playwright = sync_playwright().start()
            launch_options: dict[str, object] = {"headless": self._headless}
            if self._channel is not None:
                launch_options["channel"] = self._channel
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=self._user_data_dir or "",
                **launch_options,
            )
        self._page = self._context.new_page()
        self._page.goto(url, wait_until="domcontentloaded")

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("no page open, call open() first")
        return self._page

    def _extract_attribute(self, selector: str, attr: str) -> str | None:
        try:
            el = self.page.locator(selector).first
            if el.count() == 0:
                return None
            return el.get_attribute(attr)
        except Exception:
            return None

    def _build_fingerprint(self, topology: PageTopology, page_index: int | None,
                           page_count: int | None, field_keys: list[str],
                           action_kind: str) -> str:
        path = urlsplit(self.page.url).path
        raw = json.dumps({
            "path": path,
            "topology": topology.value,
            "page_index": page_index,
            "page_count": page_count,
            "field_keys": sorted(field_keys),
            "action_kind": action_kind,
        }, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

    def observe(self) -> PageObservation:
        declared_topology = self._extract_attribute("main", "data-topology")
        step_index_str = self._extract_attribute("main", "data-step-index")
        step_count_str = self._extract_attribute("main", "data-step-count")
        step_nav_str = self._extract_attribute("main", "data-step-nav")
        human_required = self._extract_attribute("main", "data-human-required")

        step_index = int(step_index_str) if step_index_str else None
        step_count = int(step_count_str) if step_count_str else None
        has_step_navigation = step_nav_str == "true" if step_nav_str else False

        topology = classify_topology(
            declared_topology=declared_topology,
            step_index=step_index,
            step_count=step_count,
            has_step_navigation=has_step_navigation,
        )

        # Find action button
        action_candidates = self.page.locator("button[data-action-kind]")
        if action_candidates.count() == 1:
            action_el = action_candidates.first
            action_kind = action_el.get_attribute("data-action-kind") or "unknown"
            action_label = action_el.text_content() or ""
        else:
            action_kind = "ambiguous"
            action_label = ""

        # Collect field keys
        field_keys: list[str] = []
        for el in self.page.locator("[data-field-key]").all():
            key = el.get_attribute("data-field-key")
            if key:
                field_keys.append(key)

        fingerprint = self._build_fingerprint(topology, step_index, step_count, field_keys, action_kind)

        has_verified_next_step = topology is PageTopology.MULTI_STEP_INTERMEDIATE and step_index is not None and step_count is not None and step_index < step_count

        return PageObservation(
            topology=topology,
            page_index=step_index,
            page_count=step_count,
            fingerprint=fingerprint,
            human_required=human_required,
            action_label=action_label,
            action_kind=action_kind,
            has_verified_next_step=has_verified_next_step,
        )

    def action_decision(self, observation: PageObservation) -> SafetyDecision:
        return decide_action(
            topology=observation.topology,
            label=observation.action_label,
            action_kind=observation.action_kind,
            is_bottom_action=True,
            has_verified_next_step=observation.has_verified_next_step,
        )

    def set_checkpoint_callbacks(
        self,
        *,
        before_write: Callable[[str], None] | None,
        after_verified: Callable[[str], None] | None,
    ) -> None:
        self._before_write = before_write
        self._after_verified = after_verified

    def field_value(self, field_key: str) -> str | None:
        locator = self.page.locator(
            f"[data-field-key={json.dumps(field_key)}]"
        )
        if locator.count() != 1:
            return None
        return locator.input_value()

    def fill_confirmed(self, fields: list[ExecutorField]) -> FillReport:
        confirmed_keys: list[str] = []
        missing_keys: list[str] = []
        low_confidence_keys: list[str] = []
        readback_mismatch_keys: list[str] = []
        defaulted_keys: list[str] = []

        for field in fields:
            if field.confidence is FieldConfidence.MISSING or field.value is None:
                if field.confidence is FieldConfidence.MISSING:
                    missing_keys.append(field.field_key)
                elif field.confidence is FieldConfidence.LOW:
                    low_confidence_keys.append(field.field_key)
                continue

            if field.confidence is FieldConfidence.LOW:
                low_confidence_keys.append(field.field_key)
                continue

            # Confirmed field with value
            locator = self.page.locator(
                f"[data-field-key={json.dumps(field.field_key)}]"
            )
            if locator.count() == 0:
                if field.required:
                    defaulted_keys.append(field.field_key)
                continue

            # Check current value
            current_value = locator.input_value() if locator.count() > 0 else ""
            if current_value == field.value and current_value:
                confirmed_keys.append(field.field_key)
                continue

            # Fill
            if self._before_write:
                self._before_write(field.field_key)
            locator.fill(field.value or "")

            # Readback
            readback = locator.input_value()
            if readback != field.value:
                # Retry once
                locator.fill(field.value or "")
                readback = locator.input_value()
                if readback != field.value:
                    readback_mismatch_keys.append(field.field_key)
                    continue

            if self._after_verified:
                self._after_verified(field.field_key)
            confirmed_keys.append(field.field_key)

        return FillReport(
            confirmed_keys=confirmed_keys,
            missing_keys=missing_keys,
            low_confidence_keys=low_confidence_keys,
            readback_mismatch_keys=readback_mismatch_keys,
            defaulted_keys=defaulted_keys,
        )

    def click_safe_intermediate(self, observation: PageObservation) -> None:
        decision = self.action_decision(observation)
        if not decision.allowed:
            raise UnsafeActionError(decision.reason_code)

        btn = self.page.locator('[data-action-kind="next"]')
        if btn.count() != 1:
            raise UnsafeActionError(f"expected 1 next button, found {btn.count()}")

        try:
            btn.click()
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            # Check if fingerprint changed (navigated)
            new_obs = self.observe()
            if new_obs.fingerprint == observation.fingerprint:
                raise IntermediateActionUncertainError("page fingerprint did not change after click")
        except PlaywrightTimeoutError:
            raise IntermediateActionUncertainError("timeout waiting for navigation after click")

    def observe_submission_result(self) -> str:
        result_el = self.page.locator("[data-submission-result]")
        if result_el.count() != 1:
            return "result_unknown"
        return result_el.get_attribute("data-submission-result") or "result_unknown"

    def close(self) -> None:
        if self._context:
            self._context.close()
        if self._playwright:
            self._playwright.stop()
