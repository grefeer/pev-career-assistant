from __future__ import annotations

from pathlib import Path
import threading

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


PAGES_DIR = Path(__file__).parent / "pages"


class Telemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.field_events: dict[str, int] = {}
        self.final_clicks: int = 0
        self.intermediate_clicks: int = 0
        self.ambiguous_clicks: int = 0

    def record_field(self, key: str) -> None:
        with self._lock:
            self.field_events[key] = self.field_events.get(key, 0) + 1

    def record_intermediate(self) -> None:
        with self._lock:
            self.intermediate_clicks += 1

    def record_final(self) -> None:
        with self._lock:
            self.final_clicks += 1

    def record_ambiguous(self) -> None:
        with self._lock:
            self.ambiguous_clicks += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "field_events": dict(self.field_events),
                "intermediate_clicks": self.intermediate_clicks,
                "final_clicks": self.final_clicks,
                "ambiguous_clicks": self.ambiguous_clicks,
            }

    def reset(self) -> None:
        with self._lock:
            self.field_events.clear()
            self.final_clicks = 0
            self.intermediate_clicks = 0
            self.ambiguous_clicks = 0


telemetry = Telemetry()


class BrowserEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["field", "intermediate", "final", "ambiguous"]
    key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,79}$")


def _load_page(name: str) -> HTMLResponse:
    path = PAGES_DIR / name
    if not path.exists():
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"))


app = FastAPI(title="Mock Recruitment Site")


@app.get("/single-page")
def single_page() -> HTMLResponse:
    return _load_page("single-page.html")


@app.get("/multi-step/1")
def multi_step_1() -> HTMLResponse:
    return _load_page("multi-step-1.html")


@app.get("/multi-step/2")
def multi_step_2() -> HTMLResponse:
    return _load_page("multi-step-2.html")


@app.get("/ambiguous")
def ambiguous() -> HTMLResponse:
    return _load_page("ambiguous.html")


@app.get("/human-gate")
def human_gate() -> HTMLResponse:
    return _load_page("human-gate.html")


@app.get("/readback-mismatch")
def readback_mismatch() -> HTMLResponse:
    return _load_page("readback-mismatch.html")


@app.get("/submission-success")
def submission_success() -> HTMLResponse:
    return _load_page("submission-success.html")


@app.get("/submission-failed")
def submission_failed() -> HTMLResponse:
    return _load_page("submission-failed.html")


@app.get("/submission-unknown")
def submission_unknown() -> HTMLResponse:
    return _load_page("submission-unknown.html")


@app.post("/event")
def record_event(event: BrowserEvent) -> JSONResponse:
    if event.kind == "field" and event.key:
        telemetry.record_field(event.key)
    elif event.kind == "intermediate":
        telemetry.record_intermediate()
    elif event.kind == "final":
        telemetry.record_final()
    elif event.kind == "ambiguous":
        telemetry.record_ambiguous()
    return JSONResponse({"status": "recorded"})


@app.get("/telemetry")
def get_telemetry() -> JSONResponse:
    return JSONResponse(telemetry.snapshot())


@app.post("/reset")
def reset() -> JSONResponse:
    telemetry.reset()
    return JSONResponse({"status": "reset"})
