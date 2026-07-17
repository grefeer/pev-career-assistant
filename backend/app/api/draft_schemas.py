"""Pydantic schemas for Resume Draft API endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CreateDraftRequest(BaseModel):
    match_report_id: str


class ResumeDiffOpResponse(BaseModel):
    op: Literal["reorder", "rephrase", "summarize", "omit", "highlight"]
    section: str
    before: str | None
    after: str | None
    fact_ref: str
    evidence_ids: list[str]


class ResumeDraftResponse(BaseModel):
    id: str
    match_report_id: str
    job_title: str
    company_name: str
    diffs: list[ResumeDiffOpResponse] | None
    status: str
    error_code: str | None
    state_version: int
    created_at: str
    approved_at: str | None


class ResumeDraftListResponse(BaseModel):
    items: list[ResumeDraftResponse]
    total: int


class ApproveDraftRequest(BaseModel):
    expected_version: int


class RejectDraftRequest(BaseModel):
    expected_version: int


class AttachmentResponse(BaseModel):
    id: str
    format: str
    content_type: str
    plaintext_size: int


class ApprovedResumeVersionResponse(BaseModel):
    id: str
    draft_id: str
    approved_at: str
    attachments: list[AttachmentResponse]
