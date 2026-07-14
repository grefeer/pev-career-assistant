from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from backend.app.api.dependencies import _get_db, get_current_user
from backend.app.api.routes.sessions import require_owned, summarize_session
from backend.app.db.models import User
from backend.app.repositories.sessions import activate
from backend.app.schemas import AnalysisResponse, JobListResponse
from src.resume_parser import parse_resume_file
from src.session_service import (
    DEFAULT_MESSAGE,
    DEFAULT_USER_GOAL,
    build_config,
    get_session_values,
    run_continue_analysis,
    run_new_analysis,
)
from src.utils import load_jobs, load_sample_resume


router = APIRouter(tags=["analysis"])


@router.get("/jobs", response_model=JobListResponse)
def jobs(
    current_user: Annotated[User, Depends(get_current_user)],
) -> JobListResponse:
    del current_user
    items = load_jobs()
    return JobListResponse(total=len(items), jobs=items)


@router.post("/analysis/run", response_model=AnalysisResponse)
async def run_analysis(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(_get_db)],
    thread_id: str = Form(...),
    continue_session: bool = Form(False),
    user_goal: str | None = Form(None),
    message: str = Form(DEFAULT_MESSAGE),
    resume_text: str | None = Form(None),
    max_optimization_rounds: int = Form(1),
    resume_file: UploadFile | None = File(None),
) -> AnalysisResponse:
    session = require_owned(db, current_user, thread_id)

    resolved_resume_text = resume_text.strip() if resume_text else None
    if resume_file is not None:
        raw = await resume_file.read()
        resolved_resume_text = parse_resume_file(resume_file.filename, raw)

    graph = request.app.state.graph
    if continue_session:
        result = run_continue_analysis(
            graph,
            thread_id=thread_id,
            user_goal=(user_goal or "").strip() or None,
            resume_text=resolved_resume_text or None,
            jobs=load_jobs(),
            message=(message or DEFAULT_MESSAGE).strip() or DEFAULT_MESSAGE,
            max_optimization_rounds=max_optimization_rounds,
        )
        if result is None:
            raise HTTPException(
                status_code=404,
                detail="当前会话没有已保存状态，无法继续。",
            )
    else:
        result = run_new_analysis(
            graph,
            thread_id=thread_id,
            user_goal=(user_goal or "").strip() or DEFAULT_USER_GOAL,
            resume_text=resolved_resume_text or load_sample_resume(),
            jobs=load_jobs(),
            message=(message or DEFAULT_MESSAGE).strip() or DEFAULT_MESSAGE,
            max_optimization_rounds=max_optimization_rounds,
        )

    activate(db, session)
    db.commit()
    values = get_session_values(graph, build_config(thread_id))
    return AnalysisResponse(
        thread_id=thread_id,
        result=result,
        summary=summarize_session(values),
    )
