"""Shared idempotency key management for creation endpoints."""
import hashlib
import json
from sqlalchemy.orm import Session

IDEMPOTENCY_KEY_MAX_LENGTH = 96


def compute_request_hash(request_data: dict) -> str:
    """Stable SHA-256 hash of canonical JSON request body."""
    canonical = json.dumps(request_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def check_idempotency(
    db: Session,
    model_class,
    user_id: str,
    idempotency_key: str,
    request_hash: str,
) -> tuple[object | None, bool]:
    """Returns (existing_record, is_duplicate).

    - No existing record -> (None, False)
    - Same key + same hash -> (record, True)
    - Same key + different hash -> raises ValueError('idempotency_key_conflict')
    """
    existing = (
        db.query(model_class)
        .filter(
            model_class.user_id == user_id,
            model_class.request_idempotency_key == idempotency_key,
        )
        .first()
    )
    if existing is None:
        return None, False
    if existing.request_hash == request_hash:
        return existing, True
    raise ValueError("idempotency_key_conflict")
