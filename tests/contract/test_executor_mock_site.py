from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from executor.mock_site.app import app, telemetry


@pytest.fixture(autouse=True)
def reset_telemetry() -> None:
    telemetry.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_simulation_routes_expose_explicit_topology_and_action_evidence(
    client,
) -> None:
    single = client.get("/single-page")
    first = client.get("/multi-step/1")
    final = client.get("/multi-step/2")
    ambiguous = client.get("/ambiguous")
    assert 'data-topology="single"' in single.text
    assert 'data-topology="multi"' in first.text
    assert 'data-step-index="1"' in first.text
    assert 'data-action-kind="next"' in first.text
    assert 'data-step-index="2"' in final.text
    assert 'data-action-kind="final"' in final.text
    assert 'data-action-kind="combined"' in ambiguous.text
    assert 'data-submission-result="success"' in client.get("/submission-success").text
    assert 'data-submission-result="failed"' in client.get("/submission-failed").text
    assert 'data-submission-result="unknown"' in client.get("/submission-unknown").text


def test_reset_clears_all_click_and_field_counters(client) -> None:
    assert client.post("/reset").json() == {"status": "reset"}
    assert client.get("/telemetry").json() == {
        "field_events": {},
        "intermediate_clicks": 0,
        "final_clicks": 0,
        "ambiguous_clicks": 0,
    }
