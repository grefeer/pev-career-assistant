from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.exc import ArgumentError
from sqlalchemy.engine import make_url


DESTRUCTIVE_MYSQL_OPT_IN_ENV = "ALLOW_DESTRUCTIVE_MYSQL_TESTS"
MYSQL_TEST_URL_ENV = "TEST_MYSQL_URL"


def require_dedicated_mysql_test_database(environ: Mapping[str, str]) -> str:
    if environ.get(DESTRUCTIVE_MYSQL_OPT_IN_ENV) != "1":
        raise ValueError(
            f"destructive integration gate requires "
            f"{DESTRUCTIVE_MYSQL_OPT_IN_ENV}=1"
        )
    database_url = environ.get(MYSQL_TEST_URL_ENV)
    if database_url is None or not database_url.strip():
        raise ValueError(
            f"destructive integration gate requires non-empty {MYSQL_TEST_URL_ENV}"
        )
    try:
        parsed = make_url(database_url)
    except (ArgumentError, ValueError) as error:
        raise ValueError(
            "integration gate requires a MySQL database whose name ends with _test"
        ) from error
    database_name = parsed.database
    if (
        parsed.get_backend_name() != "mysql"
        or not database_name
        or not database_name.casefold().endswith("_test")
    ):
        raise ValueError(
            "integration gate requires a MySQL database whose name ends with _test"
        )
    return database_url
