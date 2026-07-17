"""MatchService — core orchestration for evidence-based matching.

Implements the 7-step matching flow:
1. Hash request + check idempotency
2. Resolve/generate AnalysisSession
3. Load verified job snapshot
4. Load confirmed profile snapshot
5. Create pending MatchReport -> commit
6. Call LangGraph (no DB tx held)
7. Validate + score + finalize
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.models import AnalysisSession, MatchReport
from backend.app.repositories import matches as match_repo
from backend.app.services.idempotency import check_idempotency, compute_request_hash
from backend.app.services.job_snapshot_service import build_verified_job_snapshot
from backend.app.services.match_scoring import SCORING_RULE_VERSION, compute_score
from backend.app.services.match_validators import (
    MatchValidationError,
    validate_match_output,
)
from backend.app.services.profile_snapshot_service import (
    build_confirmed_profile_snapshot,
)

MATCH_MODEL_VERSION = "1.0"
MATCH_PROMPT_VERSION = "1.0"
MATCH_OUTPUT_SCHEMA_VERSION = "1.0"
STALE_TIMEOUT_MINUTES = 10


class MatchService:
    """Orchestrates the full evidence-matching workflow."""

    repo = match_repo  # Module reference for route-level access (e.g. match_service.repo.get_by_id)

    def __init__(self, match_graph, model_version: str = MATCH_MODEL_VERSION):
        self.graph = match_graph
        self.model_version = model_version

    def create_match(
        self,
        db: Session,
        user_id: str,
        job_id: str,
        profile_version_id: str,
        idempotency_key: str,
        analysis_session_id: str | None = None,
    ) -> MatchReport:
        """Execute the 7-step matching flow.

        Args:
            db: Database session.
            user_id: Owning user.
            job_id: Target job posting ID (must be verified).
            profile_version_id: ConfirmedProfileVersion ID.
            idempotency_key: Client-provided idempotency key.
            analysis_session_id: Optional existing session; auto-created if None.

        Returns:
            Finalized MatchReport.

        Raises:
            ValueError: On invalid inputs, idempotency conflicts, or missing entities.
        """
        # --- Step 1: Resolve or create AnalysisSession ---
        session = self._resolve_session(db, user_id, analysis_session_id)

        # --- Step 2: Build request hash and check idempotency ---
        request_data = {
            "job_id": job_id,
            "profile_version_id": profile_version_id,
            "analysis_session_id": session.id,
        }
        request_hash = compute_request_hash(request_data)
        existing, is_dup = check_idempotency(
            db, MatchReport, user_id, idempotency_key, request_hash
        )
        if is_dup:
            return existing

        # --- Step 3: Load and freeze verified job snapshot ---
        try:
            job_snapshot = build_verified_job_snapshot(db, job_id)
        except ValueError as e:
            raise ValueError(str(e))

        # --- Step 4: Load and freeze confirmed profile snapshot ---
        try:
            profile_snapshot = build_confirmed_profile_snapshot(
                db, profile_version_id, user_id
            )
        except ValueError:
            raise ValueError("match_no_confirmed_profile")

        # --- Step 5: Create pending MatchReport and commit ---
        match_id = str(uuid.uuid4())
        try:
            match_report = match_repo.create(
                db,
                id=match_id,
                user_id=user_id,
                analysis_session_id=session.id,
                job_id=job_id,
                job_verification_id=job_snapshot.job_verification_id,
                job_snapshot={
                    "job_id": job_snapshot.job_id,
                    "company_name": job_snapshot.company_name,
                    "title": job_snapshot.title,
                    "description_text": job_snapshot.description_text,
                    "locations": job_snapshot.locations,
                    "recruitment_types": job_snapshot.recruitment_types,
                    "industries": job_snapshot.industries,
                    "apply_url": job_snapshot.apply_url,
                    "gui_eligible": job_snapshot.gui_eligible,
                    "verified_at": str(job_snapshot.verified_at),
                    "review_version": job_snapshot.review_version,
                    "source_links": job_snapshot.source_links,
                },
                profile_version_id=profile_version_id,
                request_idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="pending",
                scoring_rule_version=SCORING_RULE_VERSION,
                model_version=self.model_version,
                prompt_version=MATCH_PROMPT_VERSION,
                output_schema_version=MATCH_OUTPUT_SCHEMA_VERSION,
            )
            db.commit()
            report_id = match_report.id
        except IntegrityError:
            db.rollback()
            existing, is_dup = check_idempotency(
                db, MatchReport, user_id, idempotency_key, request_hash
            )
            if is_dup:
                return existing
            raise

        # --- Step 6: Transition to "running" and call LangGraph ---
        match_report = match_repo.get_by_id_raw(db, report_id)
        match_report.status = "running"
        match_report.started_at = datetime.now(timezone.utc)
        db.commit()

        try:
            raw_result = self.graph.arun_sync(
                job_snapshot=job_snapshot.__dict__,
                profile_snapshot=profile_snapshot.__dict__,
            )
        except Exception:
            return match_repo.finalize(
                db, report_id, "failed", error_code="match_execution_interrupted"
            )

        # --- Step 7: Validate and score ---
        computation_output = raw_result.get("result")
        if computation_output is None:
            return match_repo.finalize(
                db, report_id, "failed", error_code="match_model_validation_failed"
            )

        # Convert to dicts for validation (Pydantic -> dict if needed)
        def _to_dict(val):
            return val.model_dump() if hasattr(val, "model_dump") else val

        output_dict = {
            "strengths": [
                _to_dict(a) for a in computation_output.strengths
            ],
            "gaps": [_to_dict(a) for a in computation_output.gaps],
            "unknowns": [_to_dict(a) for a in computation_output.unknowns],
            "risks": [_to_dict(a) for a in computation_output.risks],
            "recommendation": _to_dict(computation_output.recommendation),
        }

        try:
            validated = validate_match_output(
                output_dict, job_snapshot, profile_snapshot
            )
        except MatchValidationError as e:
            return match_repo.finalize(
                db, report_id, "failed", error_code=e.error_code
            )

        # Score
        score, components, priority = compute_score(computation_output)

        return match_repo.finalize(
            db,
            report_id,
            "completed",
            score=score,
            score_components=[c.__dict__ for c in components],
            strengths=validated["strengths"],
            gaps=validated["gaps"],
            unknowns=validated["unknowns"],
            risks=validated["risks"],
            recommendation=validated["recommendation"],
            application_priority=priority,
        )

    def recover_stale(self, db: Session) -> int:
        """Recover stale (pending/running beyond timeout) matches."""
        return match_repo.recover_stale(db, timeout_minutes=STALE_TIMEOUT_MINUTES)

    def _resolve_session(
        self, db: Session, user_id: str, session_id: str | None
    ) -> AnalysisSession:
        """Resolve an existing session or auto-create one."""
        if session_id:
            session = (
                db.query(AnalysisSession)
                .filter(
                    AnalysisSession.id == session_id,
                    AnalysisSession.user_id == user_id,
                )
                .first()
            )
            if session is None:
                raise ValueError("not_found")
            return session
        # Auto-create session
        sid = str(uuid.uuid4())
        session = AnalysisSession(
            id=sid, user_id=user_id, thread_id=sid, label="Match Session"
        )
        db.add(session)
        db.flush()
        return session
