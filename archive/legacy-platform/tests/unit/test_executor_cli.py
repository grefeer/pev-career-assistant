from types import SimpleNamespace

from executor import cli
from executor.browser import BrowserSession
from executor.protocol import ExecutorTaskPayload


def test_browser_defaults_to_visible_installed_chrome(tmp_path) -> None:
    browser = BrowserSession(user_data_dir=tmp_path / "profile")

    assert browser._headless is False
    assert browser._channel == "chrome"


def test_run_simulation_cli_does_not_force_headless(
    monkeypatch, tmp_path
) -> None:
    payload = ExecutorTaskPayload(
        task_id="11111111-1111-4111-8111-111111111111",
        state_version=0,
        target_url="http://127.0.0.1:8765/single-page",
        fields=[],
    )
    browser_options: dict[str, object] = {}

    class FakeBrowser:
        def __init__(self, **kwargs) -> None:
            browser_options.update(kwargs)

        def close(self) -> None:
            pass

    class FakeEngine:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *, payload):
            return SimpleNamespace(kind="ready_for_review", reason_code="safe")

    monkeypatch.setattr(cli, "_load_fixture", lambda path: payload)
    monkeypatch.setattr(cli, "WindowsCredentialStore", lambda: object())
    monkeypatch.setattr(cli, "ExecutorApiClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "BrowserSession", FakeBrowser)
    monkeypatch.setattr(cli, "ExecutorEngine", FakeEngine)

    cli.cmd_run_simulation(
        SimpleNamespace(
            base_url="http://127.0.0.1:8000",
            fixture="unused.json",
            data_dir=str(tmp_path),
        )
    )

    assert browser_options.get("headless", False) is False
