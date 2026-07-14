from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import User


def normalize_account(account: str) -> str:
    return account.strip().lower()


def get_by_account(db: Session, account: str) -> User | None:
    return db.scalar(select(User).where(User.account == normalize_account(account)))


def get_by_id(db: Session, user_id: str) -> User | None:
    return db.get(User, user_id)
