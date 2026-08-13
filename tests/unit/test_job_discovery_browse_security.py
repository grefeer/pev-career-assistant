from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_browse_module():
    path = Path("skill/job-discovery/scripts/browse.py").resolve()
    spec = importlib.util.spec_from_file_location("job_discovery_browse", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Request:
    def __init__(self, url: str) -> None:
        self.url = url


class _Route:
    def __init__(self, url: str) -> None:
        self.request = _Request(url)
        self.aborted = False
        self.continued = False

    def abort(self) -> None:
        self.aborted = True

    def continue_(self) -> None:
        self.continued = True


class _Context:
    def __init__(self) -> None:
        self.guard = None

    def route(self, _pattern: str, guard) -> None:
        self.guard = guard


def test_public_network_guard_blocks_non_web_schemes() -> None:
    module = _load_browse_module()
    context = _Context()
    module.install_public_network_guard(context)
    assert context.guard is not None

    file_route = _Route("file:///C:/secret.txt")
    data_route = _Route("data:text/plain,inline")
    public_route = _Route("https://example.com/jobs")

    context.guard(file_route)
    context.guard(data_route)
    context.guard(public_route)

    assert file_route.aborted and not file_route.continued
    assert data_route.continued and not data_route.aborted
    assert public_route.continued and not public_route.aborted


def test_cache_rejects_path_injection_hash(tmp_path: Path) -> None:
    module = _load_browse_module()
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        '{"https://example.com": "../../outside"}', encoding="utf-8"
    )

    assert module._check_cache(tmp_path, "https://example.com", "use") is None
