from sqlalchemy import create_engine, inspect

from backend.app.db.base import Base


def test_profile_schema_has_version_and_append_only_tables() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    assert {
        "profiles",
        "resume_assets",
        "resume_imports",
        "profile_field_evidence",
        "profile_field_decisions",
        "confirmed_profile_versions",
    } <= set(inspector.get_table_names())
    assert {"version", "local_sensitive_references"} <= {
        column["name"] for column in inspector.get_columns("profiles")
    }
    engine.dispose()
