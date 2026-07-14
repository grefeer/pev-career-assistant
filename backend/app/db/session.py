from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config import Settings, get_settings


settings = get_settings()


def build_engine(settings: Settings) -> Engine:
    options: dict[str, Any] = {"pool_pre_ping": True, "pool_recycle": 1800}
    return create_engine(settings.database_url, **options)


def build_readiness_engine(settings: Settings) -> Engine:
    options: dict[str, Any] = {"pool_pre_ping": True, "pool_recycle": 1800}
    if make_url(settings.database_url).get_backend_name() == "mysql":
        timeout = settings.readiness_timeout_seconds
        options["connect_args"] = {
            "connect_timeout": timeout,
            "read_timeout": timeout,
            "write_timeout": timeout,
        }
    return create_engine(settings.database_url, **options)


engine = build_engine(settings)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db


@contextmanager
def session_scope() -> Iterator[Session]:
    with SessionLocal.begin() as db:
        yield db
