from __future__ import annotations

from sqlalchemy.engine import make_url


def require_dedicated_mysql_test_database(database_url: str) -> None:
    parsed = make_url(database_url)
    database_name = parsed.database
    if (
        parsed.get_backend_name() != "mysql"
        or not database_name
        or not database_name.endswith("_test")
    ):
        raise ValueError(
            "integration gate requires a MySQL database whose name ends with _test"
        )
