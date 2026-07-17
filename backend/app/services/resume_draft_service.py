"""ResumeDraftService — core orchestration for resume drafting and approval.

Implements the draft -> approve/reject pipeline:

- **create_draft**: Load completed MatchReport -> create ``generating`` draft in
  a short transaction -> call draft generator (no DB tx held) -> validate diff
  ops -> finalize as ``draft`` or ``failed``.

- **approve_draft**: Lock owned draft with ``expected_version`` -> reserve
  pending PDF/DOCX attachment rows -> generate + encrypt + store attachments
  (no DB tx held during IO) -> short transaction: create
  ``ApprovedResumeVersion``, backfill attachment FKs, mark ready, approve draft
  -> on unique-constraint race: rollback, re-read same-key result -> on
  generation/storage failure: compensate (delete written objects, mark
  attachments failed), draft stays ``draft``.

- **reject_draft**: Lock draft with ``expected_version`` -> set
  ``status='rejected'``, ``rejected_at=now``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.models import (
    ApprovedResumeVersion,
    ConfirmedProfileVersion,
    MatchReport,
    ResumeDraft,
)
from backend.app.repositories import drafts as drafts_repo
from backend.app.repositories.attachments import (
    reserve_or_reset_pending,
)
from backend.app.repositories.drafts import StaleDraftVersionError
from backend.app.services.attachment_service import (
    TEXT_FORMATS,
    compensate_attachments,
    generate_resume_docx,
    generate_resume_pdf,
)
from backend.app.services.draft_validators import (
    DraftValidationError,
    validate_draft_diffs,
)
from backend.app.services.idempotency import check_idempotency, compute_request_hash
from backend.app.services.storage import ENCRYPTION_VERSION, EncryptedObjectStore

logger = logging.getLogger(__name__)


class DraftGenerator(Protocol):
    """Interface for the draft-generation component (LangGraph or mock)."""

    def generate_diffs(
        self,
        *,
        job_snapshot: dict[str, Any],
        profile_facts: dict[str, Any],
    ) -> dict[str, Any]:
        ...


class ResumeDraftService:
    """Orchestrates the resume draft -> approve / reject pipeline."""

    def __init__(self, draft_generator: DraftGenerator | None = None) -> None:
        self.draft_generator = draft_generator
        self.repo = drafts_repo

    # ------------------------------------------------------------------
    # create_draft
    # ------------------------------------------------------------------

    def create_draft(
        self,
        db: Session,
        user_id: str,
        match_report_id: str,
        idempotency_key: str,
    ) -> ResumeDraft:
        """Create a resume draft from a completed MatchReport.

        Raises:
            ValueError: If the match report is not found, not completed,
                or has an error code; or on idempotency key conflict.
        """
        # --- 1. Load completed MatchReport -----------------------------------
        match_report = (
            db.query(MatchReport)
            .filter(
                MatchReport.id == match_report_id,
                MatchReport.user_id == user_id,
            )
            .first()
        )
        if match_report is None:
            raise ValueError("not_found")
        if match_report.status != "completed":
            raise ValueError("draft_match_not_completed")
        if match_report.error_code is not None:
            raise ValueError("draft_match_failed")

        # --- 2. Request hash + idempotency check ----------------------------
        request_data: dict[str, Any] = {
            "match_report_id": match_report_id,
            "profile_version_id": match_report.profile_version_id,
        }
        request_hash = compute_request_hash(request_data)
        existing, is_dup = check_idempotency(
            db, ResumeDraft, user_id, idempotency_key, request_hash,
        )
        if is_dup:
            return existing

        # --- 3. Create ``generating`` draft (short tx, then commit) ----------
        draft_id = str(uuid.uuid4())
        try:
            _draft = drafts_repo.create(
                db,
                id=draft_id,
                user_id=user_id,
                match_report_id=match_report_id,
                profile_version_id=match_report.profile_version_id,
                target_job_id=match_report.job_id,
                request_idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="generating",
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            existing, is_dup = check_idempotency(
                db, ResumeDraft, user_id, idempotency_key, request_hash,
            )
            if is_dup:
                return existing
            raise

        # --- 4. Load profile facts for validation ----------------------------
        profile_version = (
            db.query(ConfirmedProfileVersion)
            .filter(
                ConfirmedProfileVersion.id == match_report.profile_version_id,
            )
            .first()
        )
        if profile_version is None:
            return drafts_repo.finalize(
                db, draft_id, "failed", "draft_profile_version_missing",
            )
        facts: dict[str, Any] = profile_version.facts_snapshot
        evidence_refs: dict[str, list[str]] = profile_version.evidence_refs

        # --- 5. Call draft generator (no DB tx held) -------------------------
        try:
            result = self.draft_generator.generate_diffs(
                job_snapshot=match_report.job_snapshot,
                profile_facts=facts,
            )
            diffs: list[dict[str, Any]] = result.get("diffs", [])
        except Exception:
            logger.exception("draft generation failed")
            return drafts_repo.finalize(
                db, draft_id, "failed", "draft_generation_interrupted",
            )

        # --- 6. Validate diff operations ------------------------------------
        try:
            validate_draft_diffs(diffs, facts, evidence_refs)
        except DraftValidationError as exc:
            return drafts_repo.finalize(db, draft_id, "failed", exc.error_code)

        # --- 7. Finalize as ``draft`` ---------------------------------------
        return drafts_repo.finalize(db, draft_id, "draft", diffs)

    # ------------------------------------------------------------------
    # approve_draft
    # ------------------------------------------------------------------

    def approve_draft(
        self,
        db: Session,
        user_id: str,
        draft_id: str,
        expected_version: int,
        object_store: EncryptedObjectStore,
        idempotency_key: str,
    ) -> ApprovedResumeVersion:
        """Approve a draft, generate PDF/DOCX attachments, persist version.

        Returns the newly created ``ApprovedResumeVersion``.

        Raises:
            ValueError: If the draft is not found, or not in a draftable state.
            StaleDraftVersionError: If ``expected_version`` does not match.
        """
        # --- 1. Fetch and validate draft ------------------------------------
        draft = drafts_repo.get_by_id(db, draft_id, user_id)
        if draft is None:
            raise ValueError("not_found")

        # Already approved -> return existing version (idempotency)
        if draft.status == "approved":
            existing_arv = (
                db.query(ApprovedResumeVersion)
                .filter(ApprovedResumeVersion.draft_id == draft_id)
                .first()
            )
            if existing_arv is not None:
                return existing_arv
            raise ValueError("draft_inconsistent_state")

        if draft.status != "draft":
            raise ValueError(f"draft_cannot_approve_status_{draft.status}")

        # --- 2. Optimistic-lock check ---------------------------------------
        if draft.state_version != expected_version:
            raise StaleDraftVersionError(draft_id)

        # --- 3. Load profile facts + diffs ----------------------------------
        profile_version = (
            db.query(ConfirmedProfileVersion)
            .filter(
                ConfirmedProfileVersion.id == draft.profile_version_id,
            )
            .first()
        )
        if profile_version is None:
            raise ValueError("draft_profile_version_missing")
        facts: dict[str, Any] = profile_version.facts_snapshot
        diffs: list[dict[str, Any]] = draft.diffs or []

        # --- 4. Reserve pending rows + generate + store attachments ---------
        generators: dict[str, Any] = {
            "pdf": generate_resume_pdf,
            "docx": generate_resume_docx,
        }

        pending_att_ids: list[str] = []  # IDs of all reserved attachment rows
        stored: list[tuple[str, int]] = []  # (object_key, plaintext_size)

        for fmt, gen_fn in generators.items():
            object_key = f"resumes/{user_id}/{draft_id}/{fmt}"
            content_type = TEXT_FORMATS.get(fmt, "application/octet-stream")

            # Reserve or reset a pending row for this format slot
            att = reserve_or_reset_pending(
                db=db,
                draft_id=draft_id,
                user_id=user_id,
                format=fmt,
                object_key=object_key,
                content_type=content_type,
                encryption_version=ENCRYPTION_VERSION,
            )
            db.flush()

            # Track the attachment ID immediately so compensation covers it
            pending_att_ids.append(att.id)

            try:
                body = gen_fn(facts, diffs)
                result = object_store.put(
                    key=object_key,
                    plaintext=body,
                    content_type=content_type,
                )
            except Exception:
                logger.exception(
                    "attachment generation/storage failed for format %s", fmt,
                )
                # Compensate all tracked formats
                compensate_attachments(db, pending_att_ids, object_store)
                raise

            stored.append((object_key, result.plaintext_size))

        # --- 5. Short transaction: create version + backfill + approve -------
        arv_id = str(uuid.uuid4())
        try:
            arv = ApprovedResumeVersion(
                id=arv_id,
                draft_id=draft_id,
                profile_version_id=draft.profile_version_id,
                target_job_id=draft.target_job_id,
                approved_facts=facts,
                approved_diffs=diffs,
                approval_idempotency_key=idempotency_key,
                approved_by=user_id,
            )
            db.add(arv)
            db.flush()

            # Backfill: mark each attachment as ready and link to version
            for att_id, (key, size) in zip(pending_att_ids, stored):
                from backend.app.repositories.attachments import mark_ready

                mark_ready(db, att_id, arv.id, size)

            # Approve the draft (optimistic lock)
            drafts_repo.approve(db, draft_id, expected_version)
            db.commit()
        except IntegrityError:
            db.rollback()
            # Delete stored objects that are now orphaned
            for key, _ in stored:
                try:
                    object_store.delete(key)
                except Exception:
                    logger.warning(
                        "failed to delete orphaned object %s", key,
                    )
            # Race: another request already created this version
            existing_arv = (
                db.query(ApprovedResumeVersion)
                .filter(ApprovedResumeVersion.draft_id == draft_id)
                .first()
            )
            if existing_arv is not None:
                return existing_arv
            raise

        return arv

    # ------------------------------------------------------------------
    # reject_draft
    # ------------------------------------------------------------------

    def reject_draft(
        self,
        db: Session,
        user_id: str,
        draft_id: str,
        expected_version: int,
    ) -> ResumeDraft:
        """Reject a draft with optimistic locking.

        Raises:
            ValueError: If the draft is not found.
            StaleDraftVersionError: If ``expected_version`` does not match.
        """
        draft = drafts_repo.get_by_id(db, draft_id, user_id)
        if draft is None:
            raise ValueError("not_found")
        if draft.status in ("approved", "rejected"):
            raise ValueError(f"draft_cannot_reject_status_{draft.status}")

        return drafts_repo.reject(db, draft_id, expected_version)
