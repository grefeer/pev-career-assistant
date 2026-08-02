from __future__ import annotations

import importlib.util
from pathlib import Path


def _browse_module():
    path = Path(__file__).resolve().parents[2] / "skill" / "job-discovery" / "scripts" / "browse.py"
    spec = importlib.util.spec_from_file_location("skill_browse_url_policy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_browse_rejects_private_and_metadata_literal_urls() -> None:
    browse = _browse_module()

    assert not browse.is_safe_public_url("http://127.0.0.1/admin")
    assert not browse.is_safe_public_url("http://169.254.169.254/latest/meta-data")
    assert not browse.is_safe_public_url("http://10.0.0.1/jobs")
    assert not browse.is_safe_public_url("http://[::1]/jobs")


def test_browse_rejects_dns_names_that_resolve_to_private_networks(monkeypatch) -> None:
    browse = _browse_module()
    monkeypatch.setattr(
        browse.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("10.0.0.8", 0))],
    )

    assert not browse.is_safe_public_url("https://internal.example/jobs")


def test_browse_accepts_dns_name_only_when_all_addresses_are_public(monkeypatch) -> None:
    browse = _browse_module()
    monkeypatch.setattr(
        browse.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 0)),
        ],
    )

    assert browse.is_safe_public_url("https://public.example/jobs")


def test_network_guard_aborts_unsafe_redirects(monkeypatch) -> None:
    browse = _browse_module()
    monkeypatch.setattr(browse, "is_safe_public_url", lambda _url: False)

    class Route:
        class Request:
            url = "http://127.0.0.1/redirected"

        request = Request()
        aborted = False
        continued = False

        def abort(self) -> None:
            self.aborted = True

        def continue_(self) -> None:
            self.continued = True

    class Context:
        handler = None

        def route(self, _pattern, handler) -> None:
            self.handler = handler

    context = Context()
    browse.install_public_network_guard(context)
    route = Route()
    context.handler(route)

    assert route.aborted
    assert not route.continued
