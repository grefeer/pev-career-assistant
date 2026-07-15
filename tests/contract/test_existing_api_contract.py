from collections.abc import Iterator
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.api import dependencies
from backend.app.config import Settings
from backend.app.db.base import Base

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_AUTH_SECRET", "test-secret-with-at-least-32-characters")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault(
    "OBJECT_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)

from backend.app.main import create_app
from src.models import InternshipAgentState


class FakeSnapshot:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {}
        self.created_at = "2026-07-14T08:00:00+00:00"
        self.next = ()
        self.metadata = {"step": 1, "source": "loop"}
        self.config = {"configurable": {"checkpoint_id": "checkpoint-1"}}


class FakeGraph:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _thread(config: dict[str, Any]) -> str:
        return config["configurable"]["thread_id"]

    def get_state(self, config: dict[str, Any]) -> FakeSnapshot:
        return FakeSnapshot(self.values.get(self._thread(config), {}))

    def get_state_history(
        self, config: dict[str, Any], limit: int = 10
    ) -> list[FakeSnapshot]:
        values = self.values.get(self._thread(config), {})
        return [FakeSnapshot(values)] if values and limit else []

    def invoke(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._thread(config)
        result = {**payload, "final_report": "完成"}
        self.values[thread_id] = result
        return result


def build_test_compiled_graph() -> Any:
    def finish_without_llm(state: InternshipAgentState) -> dict[str, str]:
        run_kind = "continued" if len(state.get("messages", [])) > 1 else "new"
        return {"final_report": f"compiled {run_kind} report"}

    builder = StateGraph(InternshipAgentState)
    builder.add_node("finish_without_llm", finish_without_llm)
    builder.add_edge(START, "finish_without_llm")
    builder.add_edge("finish_without_llm", END)
    return builder.compile(checkpointer=MemorySaver())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = Settings(
        app_env="test",
        app_auth_secret="test-secret-with-at-least-32-characters",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        checkpoint_backend="sqlite",
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db() -> Iterator[Session]:
        with session_factory() as db:
            yield db

    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    app = create_app(settings)
    app.state.graph = FakeGraph()
    app.dependency_overrides[dependencies._get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client


def register(client: TestClient, account: str) -> tuple[str, str]:
    body = client.post(
        "/api/auth/register",
        json={"account": account, "nickname": account.title(), "password": "secret12"},
    ).json()
    return body["token"], body["profile"]["active_thread_id"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_contract(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_cors_and_jobs_path_keep_frontend_contract(client: TestClient) -> None:
    preflight = client.options(
        "/api/auth/login",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"

    token, _ = register(client, "alice")
    jobs = client.get("/api/jobs", headers=auth(token))
    assert jobs.status_code == 200
    assert jobs.json()["total"] == len(jobs.json()["jobs"])


def test_user_cannot_read_or_activate_another_users_session(client: TestClient) -> None:
    alice_token, _ = register(client, "alice")
    _, bob_session_id = register(client, "bob")

    assert (
        client.get(
            f"/api/sessions/{bob_session_id}", headers=auth(alice_token)
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/sessions/{bob_session_id}/activate", headers=auth(alice_token)
        ).status_code
        == 404
    )


def test_create_list_activate_and_label_keep_existing_contract(
    client: TestClient,
) -> None:
    token, old_session_id = register(client, "alice")
    created = client.post("/api/sessions", headers=auth(token))
    assert created.status_code == 200
    new_session_id = created.json()["active_thread_id"]
    assert created.json() == {"ok": True, "active_thread_id": new_session_id}

    listing = client.get("/api/sessions", headers=auth(token))
    assert listing.status_code == 200
    assert listing.json()["active_thread_id"] == new_session_id
    assert [item["label"] for item in listing.json()["sessions"]] == [
        "分析会话 2",
        "分析会话 1",
    ]

    response = client.post(
        f"/api/sessions/{old_session_id}/activate", headers=auth(token)
    )
    assert response.status_code == 200
    listing = client.get("/api/sessions", headers=auth(token)).json()
    assert listing["active_thread_id"] == old_session_id
    assert listing["sessions"][0]["thread_id"] == old_session_id

    label = client.get(f"/api/sessions/{old_session_id}/label", headers=auth(token))
    assert label.status_code == 200
    assert label.json() == {"label": "分析会话 1"}


def test_state_and_history_keep_existing_response_shape(client: TestClient) -> None:
    token, thread_id = register(client, "alice")

    state = client.get(f"/api/sessions/{thread_id}", headers=auth(token))
    assert state.status_code == 200
    assert state.json() == {
        "thread_id": thread_id,
        "values": {},
        "summary": {
            "user_goal": "",
            "jobs_count": 0,
            "analyses_count": 0,
            "matches_count": 0,
            "optimization_round": 0,
            "has_final_report": False,
            "shortlist": [],
            "revision_notes": [],
        },
    }
    history = client.get(
        f"/api/sessions/{thread_id}/history?limit=5", headers=auth(token)
    )
    assert history.status_code == 200
    assert history.json() == []


def test_analysis_requires_owned_session_and_moves_it_active(
    client: TestClient,
) -> None:
    alice_token, alice_old_session_id = register(client, "alice")
    alice_new_session_id = client.post(
        "/api/sessions", headers=auth(alice_token)
    ).json()["active_thread_id"]
    bob_token, bob_session_id = register(client, "bob")

    forbidden = client.post(
        "/api/analysis/run",
        headers=auth(bob_token),
        data={"thread_id": alice_new_session_id},
    )
    assert forbidden.status_code == 404

    response = client.post(
        "/api/analysis/run",
        headers=auth(alice_token),
        data={
            "thread_id": alice_old_session_id,
            "user_goal": "后端实习",
            "resume_text": "Python",
        },
    )
    assert response.status_code == 200
    assert response.json()["thread_id"] == alice_old_session_id
    assert response.json()["summary"]["has_final_report"] is True
    listing = client.get("/api/sessions", headers=auth(alice_token)).json()
    assert listing["active_thread_id"] == alice_old_session_id
    assert listing["sessions"][0]["thread_id"] == alice_old_session_id
    assert bob_session_id != alice_old_session_id


def test_continue_without_checkpoint_returns_404(client: TestClient) -> None:
    token, thread_id = register(client, "alice")
    response = client.post(
        "/api/analysis/run",
        headers=auth(token),
        data={"thread_id": thread_id, "continue_session": "true"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "当前会话没有已保存状态，无法继续。"


def test_api_contract_with_real_compiled_state_graph(client: TestClient) -> None:
    client.app.state.graph = build_test_compiled_graph()
    token, thread_id = register(client, "compiled-user")

    new_run = client.post(
        "/api/analysis/run",
        headers=auth(token),
        data={
            "thread_id": thread_id,
            "user_goal": "测试真实图",
            "resume_text": "Python",
        },
    )
    assert new_run.status_code == 200
    assert new_run.json()["result"]["final_report"] == "compiled new report"

    continued = client.post(
        "/api/analysis/run",
        headers=auth(token),
        data={"thread_id": thread_id, "continue_session": "true"},
    )
    assert continued.status_code == 200
    assert continued.json()["result"]["final_report"] == "compiled continued report"

    state = client.get(f"/api/sessions/{thread_id}", headers=auth(token))
    assert state.status_code == 200
    assert state.json()["values"]["final_report"] == "compiled continued report"

    history = client.get(
        f"/api/sessions/{thread_id}/history?limit=20", headers=auth(token)
    )
    assert history.status_code == 200
    assert len(history.json()) >= 2
    assert all(item["checkpoint_id"] for item in history.json())
