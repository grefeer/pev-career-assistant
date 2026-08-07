from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.app.db.models import (
    ConfirmedProfileVersion,
    Profile,
    ResumeAsset,
    ResumeAssetStatus,
    ResumeImport,
    ResumeImportStatus,
)
from backend.app.domain.profiles import (
    EvidenceDecisionAction,
    JsonValue,
    UnsupportedResumeTypeError,
)
from backend.app.repositories import profiles as profile_repository
from backend.app.services.profile_parser import (
    extract_evidence_candidates,
    extract_resume_document,
)
from backend.app.services.storage import (
    EncryptedObjectStore,
    StoredObject,
)


MAX_RESUME_BYTES = 10 * 1024 * 1024
PARSER_VERSION = "profile-parser-v1"
ALLOWED_SUFFIX_CONTENT_TYPES: dict[str, set[str]] = {
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/plain"},
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    },
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceDecisionInput:
    evidence_id: str
    action: EvidenceDecisionAction
    corrected_value: JsonValue | None = None


class StaleProfileVersionError(RuntimeError):
    error_code = "stale_profile_version"


class OwnedProfileResourceNotFound(LookupError):
    error_code = "profile_resource_not_found"


class ProfileValidationError(ValueError):
    error_code = "invalid_profile_operation"


class ProfileDependencyUnavailable(RuntimeError):
    error_code = "profile_dependency_unavailable"


class ResumeTooLargeError(ProfileValidationError):
    error_code = "resume_too_large"


class ObjectStoreUnavailableError(ProfileDependencyUnavailable):
    error_code = "object_store_unavailable"


class ResumeAssetStateConflict(RuntimeError):
    error_code = "resume_asset_state_conflict"


class ResumeAssetService:
    def __init__(self, object_store: EncryptedObjectStore) -> None:
        self._object_store = object_store

    def create_pending_asset(
        self,
        db: Any,
        *,
        user_id: str,
        filename: str,
        content_type: str,
        raw: bytes,
    ) -> ResumeAsset:
        suffix = Path(filename).suffix.lower()
        allowed = ALLOWED_SUFFIX_CONTENT_TYPES.get(suffix)
        if allowed is None:
            raise UnsupportedResumeTypeError(suffix)
        if content_type not in allowed:
            raise ProfileValidationError("unsupported content type")
        if len(raw) > MAX_RESUME_BYTES:
            raise ResumeTooLargeError("resume exceeds 10 MiB limit")

        profile = profile_repository.ensure_profile(db, user_id)
        asset_id = str(uuid.uuid4())
        object_key = f"users/{user_id}/resume-assets/{asset_id}"
        plaintext_sha256 = hashlib.sha256(raw).hexdigest()

        asset = ResumeAsset(
            id=asset_id,
            profile_id=profile.id,
            object_key=object_key,
            original_filename=filename,
            content_type=content_type,
            plaintext_size=len(raw),
            plaintext_sha256=plaintext_sha256,
            encryption_version="v1-aes-256-gcm",
            status=ResumeAssetStatus.PENDING_UPLOAD,
        )
        db.add(asset)
        db.flush()
        return asset

    def write_encrypted_object(self, asset: ResumeAsset, raw: bytes) -> StoredObject:
        try:
            return self._object_store.put(
                key=asset.object_key,
                plaintext=raw,
                content_type=asset.content_type,
            )
        except Exception:
            raise ObjectStoreUnavailableError("object store write failed")

    def mark_ready(
        self, db: Any, *, asset: ResumeAsset
    ) -> ResumeAsset:
        asset.status = ResumeAssetStatus.READY
        asset.error_code = None
        db.flush()
        return asset

    def mark_upload_failed(
        self, db: Any, *, asset: ResumeAsset, error_code: str
    ) -> ResumeAsset:
        asset.status = ResumeAssetStatus.UPLOAD_FAILED
        asset.error_code = error_code
        db.flush()
        return asset

    def reconcile(
        self, db: Any, *, user_id: str, asset_id: str
    ) -> ResumeAsset:
        asset = profile_repository.get_owned_asset(db, user_id, asset_id)
        if asset is None:
            raise OwnedProfileResourceNotFound("asset not found")
        if asset.status is ResumeAssetStatus.READY:
            return asset
        try:
            metadata = self._object_store.inspect(key=asset.object_key)
            if (
                metadata.encryption == "v1-aes-256-gcm"
                and metadata.content_type == asset.content_type
            ):
                return self.mark_ready(db, asset=asset)
        except Exception:
            pass
        raise ResumeAssetStateConflict("cannot reconcile asset")

    def delete_asset(
        self, db: Any, *, user_id: str, asset_id: str
    ) -> str:
        """Delete a resume asset and its imports/evidence/decisions.

        Returns the object_key of the deleted asset so the caller can purge the
        encrypted object *after* the DB transaction commits. The object delete
        is best-effort and must run post-commit so a commit failure cannot leave
        a dangling asset row pointing at a deleted object.
        """
        asset = profile_repository.get_owned_asset(db, user_id, asset_id)
        if asset is None:
            raise OwnedProfileResourceNotFound("asset not found")
        object_key = asset.object_key
        profile = profile_repository.get_profile_for_update(db, user_id)
        profile_repository.delete_asset(db, asset)
        profile_repository.update_profile_version(db, profile)
        return object_key

    def purge_object(self, object_key: str) -> None:
        """Best-effort delete of the encrypted object; never raises.

        A failure here leaves an orphan encrypted object, which is harmless
        (no row references it) and preferable to failing an already-committed
        DB delete.
        """
        try:
            self._object_store.delete(object_key)
        except Exception:
            logger.warning("object store delete failed for key=%s", object_key)


class ResumeImportService:
    def __init__(self, object_store: EncryptedObjectStore) -> None:
        self._object_store = object_store

    def start(
        self, db: Any, *, user_id: str, asset_id: str
    ) -> ResumeImport:
        asset = profile_repository.get_owned_asset(db, user_id, asset_id)
        if asset is None:
            raise OwnedProfileResourceNotFound("asset not found")
        if asset.status is not ResumeAssetStatus.READY:
            raise ResumeAssetStateConflict("asset is not ready")
        import_row = profile_repository.create_import(
            db, asset=asset, parser_version=PARSER_VERSION
        )
        return import_row

    def process(
        self, db: Any, *, user_id: str, import_id: str
    ) -> ResumeImport:
        import_row = profile_repository.get_owned_import(db, user_id, import_id)
        if import_row is None:
            raise OwnedProfileResourceNotFound("import not found")

        profile_repository.update_import_status(
            db, import_row, status=ResumeImportStatus.PARSING
        )
        db.flush()

        asset = db.get(ResumeAsset, import_row.asset_id)
        if asset is None:
            profile_repository.update_import_status(
                db, import_row, status=ResumeImportStatus.FAILED,
                error_code="resume_asset_read_failed",
            )
            return import_row

        try:
            encrypted_bytes = self._object_store.get(key=asset.object_key)
        except Exception:
            profile_repository.update_import_status(
                db, import_row, status=ResumeImportStatus.FAILED,
                error_code="resume_asset_read_failed",
            )
            return import_row

        try:
            parsed = extract_resume_document(asset.original_filename, encrypted_bytes)
        except UnsupportedResumeTypeError:
            profile_repository.update_import_status(
                db, import_row, status=ResumeImportStatus.FAILED,
                error_code="unsupported_resume_type",
            )
            return import_row

        if parsed.needs_manual_entry:
            profile_repository.update_import_status(
                db, import_row,
                status=ResumeImportStatus.NEEDS_MANUAL_ENTRY,
                error_code=parsed.error_code,
            )
            return import_row

        candidates = extract_evidence_candidates(parsed.text)
        if candidates:
            profile_repository.append_evidence(
                db,
                profile_id=import_row.profile_id,
                import_id=import_id,
                candidates=tuple(candidates),
            )

        profile_repository.update_import_status(
            db, import_row, status=ResumeImportStatus.AWAITING_CONFIRMATION
        )
        return import_row


class ProfileService:
    def apply_decisions(
        self,
        db: Any,
        *,
        user_id: str,
        expected_version: int,
        decisions: tuple[EvidenceDecisionInput, ...],
    ) -> Profile:
        profile = profile_repository.get_profile_for_update(db, user_id)
        if profile.version != expected_version:
            raise StaleProfileVersionError(
                f"expected version {expected_version}, got {profile.version}"
            )

        for decision_input in decisions:
            if (
                decision_input.action == EvidenceDecisionAction.CORRECT
                and decision_input.corrected_value is None
            ) or (
                decision_input.action != EvidenceDecisionAction.CORRECT
                and decision_input.corrected_value is not None
            ):
                raise ProfileValidationError("invalid corrected value for decision action")

            evidence = profile_repository.get_profile_evidence_by_id(
                db, profile.id, decision_input.evidence_id
            )
            if evidence is None:
                raise OwnedProfileResourceNotFound(
                    f"evidence {decision_input.evidence_id} not found"
                )

            resolved = None
            if decision_input.action == EvidenceDecisionAction.CORRECT:
                resolved = decision_input.corrected_value

            profile_repository.append_decision(
                db,
                profile_id=profile.id,
                evidence_id=decision_input.evidence_id,
                actor_user_id=user_id,
                action=decision_input.action,
                resolved_value=resolved,
            )

        profile_repository.update_profile_version(db, profile)
        return profile

    def create_confirmed_version(
        self,
        db: Any,
        *,
        user_id: str,
        expected_version: int,
        resume_import_id: str,
    ) -> ConfirmedProfileVersion:
        import_row = profile_repository.get_owned_import(db, user_id, resume_import_id)
        if import_row is None:
            raise OwnedProfileResourceNotFound("import not found")

        evidence_rows = profile_repository.list_import_evidence(db, resume_import_id)
        evidence_ids = {ev.id for ev in evidence_rows}

        profile = profile_repository.get_profile_for_update(db, user_id)
        if profile.version != expected_version:
            raise StaleProfileVersionError(
                f"expected version {expected_version}, got {profile.version}"
            )

        decisions = profile_repository.latest_decisions_by_evidence(db, profile.id)
        undecided = evidence_ids - set(decisions.keys())
        if undecided:
            raise ProfileValidationError(
                f"evidence {undecided} have no decision"
            )

        facts_snapshot: dict[str, Any] = {}
        evidence_refs: dict[str, Any] = {}
        for evidence in evidence_rows:
            decision = decisions[evidence.id]
            if decision.action == EvidenceDecisionAction.IGNORE:
                continue
            if decision.action == EvidenceDecisionAction.CORRECT:
                facts_snapshot[evidence.field_path] = decision.resolved_value
            else:
                facts_snapshot[evidence.field_path] = evidence.candidate_value
            evidence_refs[evidence.id] = {
                "action": decision.action.value,
                "field_path": evidence.field_path,
            }

        version_number = profile_repository.next_confirmed_version_number(
            db, profile.id
        )

        confirmed = profile_repository.create_confirmed_version(
            db,
            profile_id=profile.id,
            version_number=version_number,
            aggregate_version=profile.version + 1,
            facts_snapshot=facts_snapshot,
            evidence_refs=evidence_refs,
            local_sensitive_references=profile.local_sensitive_references,
        )

        # A newly confirmed version becomes the active version the runtime
        # consumes; latest-by-created_at remains the fallback when this is null.
        profile_repository.set_active_version(db, profile, confirmed.id)
        profile_repository.update_profile_version(db, profile)
        return confirmed

    def activate_version(
        self, db: Any, *, user_id: str, version_id: str
    ) -> str:
        """Select the confirmed version the runtime should consume.

        Does not bump ``profile.version`` -- this is a selection, not a facts
        mutation, so it does not interact with the optimistic-lock guard.
        """
        version = profile_repository.get_owned_version(db, user_id, version_id)
        if version is None:
            raise OwnedProfileResourceNotFound("version not found")
        profile = profile_repository.get_profile_for_update(db, user_id)
        profile_repository.set_active_version(db, profile, version_id)
        return version_id

    def update_local_sensitive_reference(
        self,
        db: Any,
        *,
        user_id: str,
        expected_version: int,
        category: str,
        reference: str,
    ) -> Profile:
        from backend.app.domain.profiles import (
            validate_local_sensitive_reference,
        )

        validate_local_sensitive_reference(category, reference)

        profile = profile_repository.get_profile_for_update(db, user_id)
        if profile.version != expected_version:
            raise StaleProfileVersionError(
                f"expected version {expected_version}, got {profile.version}"
            )

        from datetime import datetime, timezone

        refs = dict(profile.local_sensitive_references)
        refs[category] = {
            "reference": reference,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        profile.local_sensitive_references = refs
        profile_repository.update_profile_version(db, profile)
        return profile
