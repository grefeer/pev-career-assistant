"""Business logic for the Interview Prep skill.

Sits between the API layer (which owns HTTP and the transaction) and the
repository (SQL only) + LLM generator.  All methods take an open ``Session``
and flush (never commit) - except :meth:`create_kit`, which commits the
``generating`` row up front (mirroring :meth:`ResumeDraftService.create_draft`)
so a slow LLM call or a crash does not strand a half-written row.

The POST flow is synchronous for the MVP:

1. Load the user's completed :class:`MatchReport` (it carries the target job
   snapshot, the confirmed profile version, and the match analysis - the three
   grounding inputs for a tailored kit).
2. Insert a ``generating`` kit row and commit (short transaction).
3. Load the confirmed profile facts + preferences + match analysis.
4. Call the LLM generator (no DB transaction held across the network call).
5. Finalize the kit as ``ready`` (with content) or ``failed`` (with an error
   code / last error).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import ConfirmedProfileVersion, InterviewPrepKit, MatchReport, User
from backend.app.domain.interview_prep import (
    ERROR_CODE_MAX_LENGTH,
    LAST_ERROR_MAX_LENGTH,
    InterviewPrepKitStatus,
)
from backend.app.repositories import interview_prep as repo
from backend.app.repositories.preferences import to_summary as preferences_summary
from backend.app.repositories.preferences import get_for_user as get_preference_for_user
from backend.app.services.interview_prep.generator import (
    InterviewPrepGenerationError,
)

logger = logging.getLogger(__name__)


class InterviewPrepError(Exception):
    """Base class for interview-prep service errors."""


class InterviewPrepNotFoundError(InterviewPrepError):
    """Raised when a kit does not exist or is not owned by the caller."""


class InterviewPrepInputError(InterviewPrepError):
    """Raised when create input references an unusable match report.

    Carries a stable ``code`` (``not_found`` / ``match_not_completed`` /
    ``match_failed``) so the route can map it to a meaningful HTTP status.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class InterviewPrepGenerator(Protocol):
    """Interface for the kit-generation component (LLM or mock).

    The optional ``preferences`` and ``match_analysis`` inputs let an
    agent-driven generator tailor the kit to the user's stated preferences and
    to the match report's strengths/gaps. They default to ``None`` so a
    generator that ignores them (and unit-test mocks) remain compatible.
    """

    def generate_prep(
        self,
        *,
        job_snapshot: dict[str, Any],
        profile_facts: dict[str, Any] | None = None,
        preferences: dict[str, Any] | None = None,
        match_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


def _truncate(text: str, limit: int) -> str:
    """Clamp ``text`` to ``limit`` chars, appending an ellipsis when trimmed."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)] + "..."


class InterviewPrepService:
    """Orchestrates create / read for interview-prep kits."""

    def __init__(
        self,
        settings: Settings,
        *,
        generator: InterviewPrepGenerator | None = None,
    ) -> None:
        self.settings = settings
        self.generator = generator
        self.agent_version = settings.interview_prep_agent_version

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def create_kit(
        self,
        db: Session,
        *,
        user: User,
        match_report_id: str,
    ) -> InterviewPrepKit:
        """Generate an interview-prep kit from a completed MatchReport.

        The kit is anchored to the match report's target job + confirmed
        profile version, and tailored with the user's preferences + the
        match analysis (strengths/gaps). Raises
        :class:`InterviewPrepInputError` when the match report is missing, not
        completed, or carries an error.
        """
        # --- 1. Load completed MatchReport -----------------------------------
        match_report = (
            db.query(MatchReport)
            .filter(
                MatchReport.id == match_report_id,
                MatchReport.user_id == user.id,
            )
            .first()
        )
        if match_report is None:
            raise InterviewPrepInputError("not_found", "match report not found")
        if match_report.status != "completed":
            raise InterviewPrepInputError(
                "match_not_completed", "match report is not completed"
            )
        if match_report.error_code is not None:
            raise InterviewPrepInputError(
                "match_failed", "match report carries an error"
            )

        job_snapshot: dict[str, Any] = match_report.job_snapshot or {}

        # --- 2. Load preferences + match analysis (no profile needed) ---------
        # Stored on the kit row for audit/reproducibility, and passed to the
        # generator so it can tailor the kit to the user's preferences and to
        # the match report's strengths/gaps.
        preferences = preferences_summary(get_preference_for_user(db, user.id))
        match_analysis: dict[str, Any] = {
            "strengths": match_report.strengths or [],
            "gaps": match_report.gaps or [],
            "unknowns": match_report.unknowns or [],
            "risks": match_report.risks or [],
        }

        # --- 3. Create ``generating`` kit (short tx, then commit) ------------
        kit = repo.create_kit(
            db,
            user_id=user.id,
            job_snapshot=job_snapshot,
            agent_version=self.agent_version,
            target_job_id=match_report.job_id,
            profile_version_id=match_report.profile_version_id,
            preferences_summary_json=preferences,
            match_analysis_json=match_analysis,
        )
        db.commit()

        # --- 4. Load profile facts -------------------------------------------
        profile_version = (
            db.query(ConfirmedProfileVersion)
            .filter(ConfirmedProfileVersion.id == match_report.profile_version_id)
            .first()
        )
        if profile_version is None:
            return self._finalize_failed(
                db, kit, code="interview_prep_profile_version_missing"
            )
        facts: dict[str, Any] = profile_version.facts_snapshot or {}

        # --- 5. Call generator (no DB tx held) --------------------------------
        if self.generator is None:
            # No generator wired (LLM unavailable at boot): finalize failed
            # so the kit row records why nothing was produced.
            return self._finalize_failed(
                db, kit, code="interview_prep_generator_unavailable"
            )

        try:
            result = self.generator.generate_prep(
                job_snapshot=job_snapshot,
                profile_facts=facts,
                preferences=preferences,
                match_analysis=match_analysis,
            )
            content: dict[str, Any] = result.get("content", {}) or {}
        except InterviewPrepGenerationError as exc:
            logger.warning("interview-prep generation parse error: %s", exc.code)
            return self._finalize_failed(
                db, kit, code=exc.code, last_error=str(exc)
            )
        except Exception:
            logger.exception("interview-prep generation failed")
            return self._finalize_failed(
                db, kit, code="interview_prep_generation_interrupted"
            )

        # --- 6. Finalize as ``ready`` ----------------------------------------
        # The kit row already carries ``agent_version``; store the content
        # dict directly (the five normalized sections).
        updated = repo.complete_kit(
            db,
            str(kit.id),
            status=InterviewPrepKitStatus.ready,
            content_json=content,
        )
        return updated if updated is not None else kit

    def _finalize_failed(
        self,
        db: Session,
        kit: InterviewPrepKit,
        *,
        code: str,
        last_error: str | None = None,
    ) -> InterviewPrepKit:
        """Stamp a terminal ``failed`` outcome onto ``kit`` and commit."""
        error_code = _truncate(code, ERROR_CODE_MAX_LENGTH)
        trimmed_error = (
            _truncate(last_error, LAST_ERROR_MAX_LENGTH) if last_error else None
        )
        updated = repo.complete_kit(
            db,
            str(kit.id),
            status=InterviewPrepKitStatus.failed,
            error_code=error_code,
            last_error=trimmed_error,
        )
        db.commit()
        return updated if updated is not None else kit

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def get_kit(
        self,
        db: Session,
        kit_id: str,
        *,
        user: User,
    ) -> InterviewPrepKit:
        kit = repo.get_kit_for_owner(db, kit_id, user.id)
        if kit is None:
            raise InterviewPrepNotFoundError(kit_id)
        return kit

    def list_kits(
        self,
        db: Session,
        *,
        user: User,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InterviewPrepKit]:
        return repo.list_kits(db, user.id, limit=limit, offset=offset)

