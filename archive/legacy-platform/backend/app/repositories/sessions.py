from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import AnalysisSession
from src.session_service import generate_thread_id


def list_for_user(db: Session, user_id: str) -> list[AnalysisSession]:
    statement = (
        select(AnalysisSession)
        .where(AnalysisSession.user_id == user_id)
        .order_by(
            AnalysisSession.activated_at.desc(), AnalysisSession.updated_at.desc()
        )
    )
    return list(db.scalars(statement))


def get_owned(db: Session, user_id: str, thread_id: str) -> AnalysisSession | None:
    return db.scalar(
        select(AnalysisSession).where(
            AnalysisSession.user_id == user_id,
            AnalysisSession.thread_id == thread_id,
        )
    )


def create_for_user(db: Session, user_id: str) -> AnalysisSession:
    count = len(list_for_user(db, user_id))
    item = AnalysisSession(
        user_id=user_id,
        thread_id=generate_thread_id(),
        label=f"分析会话 {count + 1}",
    )
    db.add(item)
    db.flush()
    return item


def activate(db: Session, item: AnalysisSession) -> None:
    now = datetime.now(timezone.utc)
    item.activated_at = now
    item.updated_at = now
    db.flush()
