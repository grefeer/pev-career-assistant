"""Human smoke-run entry: ``python -m adapters <company> <url>``.

Prints the fetched records as JSON, or ``blocked: <code>`` on any failure —
the same blocked semantics browse.py uses, so a smoke run either shows real
records or an explicit blocked reason, never a silent empty result.
"""
from __future__ import annotations

import json
import sys

from . import load_company_adapter


def main(argv: list[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if len(args) != 2:
        print("usage: python -m adapters <company> <url>")
        print("companies: didi, netease, baidu")
        return 2
    company, url = args
    try:
        adapter = load_company_adapter(company)
    except Exception as exc:  # noqa: BLE001 - CLI boundary: always a stable code.
        print(f"blocked: {getattr(exc, 'code', 'adapter_invalid')}")
        return 1
    try:
        result = adapter.execute(url, None, None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary: always a stable code.
        print(f"blocked: {getattr(exc, 'code', 'adapter_error')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
