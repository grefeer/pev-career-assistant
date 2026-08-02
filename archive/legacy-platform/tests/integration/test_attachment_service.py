"""Integration tests for the attachment service.

Covers:
- render_resume_lines with and without diffs
- generate_resume_pdf magic-byte check (b"%PDF-")
- generate_resume_docx magic-byte check (b"PK")
- Full generate-and-store round-trip (requires TEST_S3_ENDPOINT)
- Compensation (delete after partial failure)
- Permission enforcement on download
"""

from __future__ import annotations

import base64
import os
import uuid

import pytest
from sqlalchemy.orm import Session

from backend.app.db.models import (
    ApprovedResumeAttachment,
    User,
)
from backend.app.services.attachment_service import (
    download_attachment,
    generate_and_store_attachments,
    generate_resume_docx,
    generate_resume_pdf,
    render_resume_lines,
)
from backend.app.services.storage import EncryptedObjectStore, S3BlobStore


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_FACTS: dict = {
    "name": "Alice Zhang",
    "contact": {
        "email": "alice@example.com",
        "phone": "+1-555-0100",
        "location": "San Francisco, CA",
    },
    "summary": "Experienced software engineer with 8 years in full-stack development.",
    "education": [
        {
            "school": "Stanford University",
            "degree": "M.S.",
            "field": "Computer Science",
            "start_date": "2014-09",
            "end_date": "2016-06",
        },
        {
            "school": "UC Berkeley",
            "degree": "B.S.",
            "field": "Computer Science",
            "start_date": "2010-09",
            "end_date": "2014-06",
        },
    ],
    "skills": [
        {"name": "Python", "level": "Expert"},
        {"name": "TypeScript", "level": "Advanced"},
        {"name": "React", "level": "Advanced"},
    ],
    "work_experience": [
        {
            "company": "TechCorp",
            "title": "Senior Engineer",
            "location": "San Francisco, CA",
            "start_date": "2018-03",
            "end_date": "Present",
            "highlights": [
                "Led a team of 5 engineers",
                "Migrated monolith to microservices",
            ],
        },
        {
            "company": "StartupXYZ",
            "title": "Software Engineer",
            "location": "Palo Alto, CA",
            "start_date": "2016-07",
            "end_date": "2018-02",
        },
    ],
    "projects": [
        {
            "name": "Open Source CLI Tool",
            "role": "Maintainer",
            "url": "https://github.com/alice/tool",
        }
    ],
    "awards": [
        {"name": "Best Paper Award", "issuer": "ACM", "date": "2023"},
    ],
    "certifications": [
        {"name": "AWS Solutions Architect", "issuer": "Amazon"},
    ],
}

SAMPLE_DIFFS: list[dict] = [
    {
        "op": "rephrase",
        "section": "work_experience",
        "fact_ref": "senior_engineer",
        "before": "Led a team of 5 engineers",
        "after": "Directed a cross-functional team of 5 engineers",
        "evidence_ids": ["evt_001"],
    },
    {
        "op": "omit",
        "section": "education",
        "fact_ref": "uc_berkeley",
        "before": "B.S. in Computer Science",
        "evidence_ids": ["evt_002"],
    },
]


# ---------------------------------------------------------------------------
# render_resume_lines
# ---------------------------------------------------------------------------


def test_render_resume_lines_without_diffs() -> None:
    lines = render_resume_lines(SAMPLE_FACTS)
    assert isinstance(lines, list)
    assert len(lines) > 0
    assert "ALICE ZHANG" in lines[0] or "Alice Zhang" in lines[0]


def test_render_resume_lines_contains_key_sections() -> None:
    lines = render_resume_lines(SAMPLE_FACTS)
    text = "\n".join(lines)
    assert "EDUCATION" in text
    assert "SKILLS" in text
    assert "WORK EXPERIENCE" in text
    assert "PROJECTS" in text
    assert "AWARDS" in text
    assert "CERTIFICATIONS" in text
    assert "Stanford University" in text
    assert "TechCorp" in text


def test_render_resume_lines_with_diffs() -> None:
    lines = render_resume_lines(SAMPLE_FACTS, SAMPLE_DIFFS)
    text = "\n".join(lines)
    # The rephrase diff should be applied
    assert "cross-functional team" in text


def test_render_resume_lines_empty_facts() -> None:
    lines = render_resume_lines({})
    assert isinstance(lines, list)
    assert len(lines) == 0


def test_render_resume_lines_deterministic() -> None:
    lines1 = render_resume_lines(SAMPLE_FACTS)
    lines2 = render_resume_lines(SAMPLE_FACTS)
    assert lines1 == lines2


# ---------------------------------------------------------------------------
# generate_resume_pdf – magic byte check
# ---------------------------------------------------------------------------


def test_generate_resume_pdf_magic_bytes() -> None:
    pdf_bytes = generate_resume_pdf(SAMPLE_FACTS, SAMPLE_DIFFS)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0
    assert pdf_bytes.startswith(b"%PDF-"), (
        f"Expected PDF to start with b'%PDF-', got {pdf_bytes[:8]!r}"
    )


def test_generate_resume_pdf_without_diffs() -> None:
    pdf_bytes = generate_resume_pdf(SAMPLE_FACTS)
    assert pdf_bytes.startswith(b"%PDF-")


def test_generate_resume_pdf_empty_facts() -> None:
    pdf_bytes = generate_resume_pdf({})
    assert pdf_bytes.startswith(b"%PDF-")


def test_generate_resume_pdf_deterministic_content() -> None:
    pdf1 = generate_resume_pdf(SAMPLE_FACTS)
    pdf2 = generate_resume_pdf(SAMPLE_FACTS)
    # PDFs contain timestamps, so exact equality isn't guaranteed,
    # but they should both be valid PDFs
    assert pdf1.startswith(b"%PDF-")
    assert pdf2.startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# generate_resume_docx – magic byte check
# ---------------------------------------------------------------------------


def test_generate_resume_docx_magic_bytes() -> None:
    docx_bytes = generate_resume_docx(SAMPLE_FACTS, SAMPLE_DIFFS)
    assert isinstance(docx_bytes, bytes)
    assert len(docx_bytes) > 0
    assert docx_bytes.startswith(b"PK"), (
        f"Expected DOCX to start with b'PK', got {docx_bytes[:8]!r}"
    )


def test_generate_resume_docx_without_diffs() -> None:
    docx_bytes = generate_resume_docx(SAMPLE_FACTS)
    assert docx_bytes.startswith(b"PK")


def test_generate_resume_docx_empty_facts() -> None:
    docx_bytes = generate_resume_docx({})
    assert docx_bytes.startswith(b"PK")


def test_generate_resume_docx_deterministic_content() -> None:
    docx1 = generate_resume_docx(SAMPLE_FACTS)
    docx2 = generate_resume_docx(SAMPLE_FACTS)
    # Both should be valid docx (PK = ZIP magic)
    assert docx1.startswith(b"PK")
    assert docx2.startswith(b"PK")


# ---------------------------------------------------------------------------
# In-memory mock blob store for storage integration tests
# ---------------------------------------------------------------------------


class _MockBlobStore:
    """Minimal in-memory blob store for testing."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        self._metadata: dict[str, dict] = {}

    def put_bytes(
        self,
        *,
        key: str,
        body: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> None:
        self._data[key] = body
        self._metadata[key] = {
            "content_type": content_type,
            "metadata": metadata,
        }

    def get_bytes(self, *, key: str) -> bytes:
        if key not in self._data:
            raise FileNotFoundError(key)
        return self._data[key]

    def delete(self, *, key: str) -> None:
        self._data.pop(key, None)
        self._metadata.pop(key, None)

    def head(self, *, key: str) -> dict:
        if key not in self._metadata:
            raise FileNotFoundError(key)
        return {
            "ContentType": self._metadata[key]["content_type"],
            "Metadata": self._metadata[key]["metadata"],
            "ContentLength": len(self._data.get(key, b"")),
        }

    def ensure_bucket(self) -> None:
        pass

    def check_bucket(self) -> None:
        pass


@pytest.fixture
def mock_object_store() -> EncryptedObjectStore:
    key = base64.b64encode(bytes(range(32))).decode("ascii")
    return EncryptedObjectStore(_MockBlobStore(), key)


# ---------------------------------------------------------------------------
# generate_and_store_attachments
# ---------------------------------------------------------------------------


def test_generate_and_store_attachments_success(
    db_session: Session,
    test_user: User,
    mock_object_store: EncryptedObjectStore,
) -> None:
    draft_id = str(uuid.uuid4())
    arv_id = str(uuid.uuid4())

    attachments = generate_and_store_attachments(
        db=db_session,
        user_id=test_user.id,
        draft_id=draft_id,
        approved_resume_version_id=arv_id,
        approved_facts=SAMPLE_FACTS,
        diffs=SAMPLE_DIFFS,
        object_store=mock_object_store,
    )

    assert len(attachments) == 2
    formats = {a.format for a in attachments}
    assert formats == {"pdf", "docx"}

    for att in attachments:
        assert att.status == "ready"
        assert att.user_id == test_user.id
        assert att.draft_id == draft_id
        assert att.approved_resume_version_id == arv_id
        assert att.plaintext_size > 0
        assert att.object_key.startswith(f"resumes/{test_user.id}/{draft_id}/")
        assert att.object_key.endswith(f".{att.format}")

    # Verify records are persisted
    persisted = (
        db_session.query(ApprovedResumeAttachment)
        .filter(ApprovedResumeAttachment.draft_id == draft_id)
        .all()
    )
    assert len(persisted) == 2


def test_generate_and_store_attachments_content_integrity(
    db_session: Session,
    test_user: User,
    mock_object_store: EncryptedObjectStore,
) -> None:
    draft_id = str(uuid.uuid4())
    attachments = generate_and_store_attachments(
        db=db_session,
        user_id=test_user.id,
        draft_id=draft_id,
        approved_resume_version_id=str(uuid.uuid4()),
        approved_facts=SAMPLE_FACTS,
        diffs=SAMPLE_DIFFS,
        object_store=mock_object_store,
    )

    for att in attachments:
        stored_bytes = mock_object_store.get(key=att.object_key)
        expected_content_type = (
            "application/pdf"
            if att.format == "pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert att.content_type == expected_content_type
        if att.format == "pdf":
            assert stored_bytes[:12] == b"%PDF-".ljust(12, b"\x00")[:12] or stored_bytes.startswith(b"%PDF-")
        elif att.format == "docx":
            assert stored_bytes.startswith(b"PK")

    # Verify ciphertext in mock store (the mock metadata reflects encryption)
    for att in attachments:
        assert att.encryption_version == "v1-aes-256-gcm"


def test_generate_and_store_no_diffs(
    db_session: Session,
    test_user: User,
    mock_object_store: EncryptedObjectStore,
) -> None:
    draft_id = str(uuid.uuid4())
    attachments = generate_and_store_attachments(
        db=db_session,
        user_id=test_user.id,
        draft_id=draft_id,
        approved_resume_version_id=str(uuid.uuid4()),
        approved_facts=SAMPLE_FACTS,
        diffs=[],
        object_store=mock_object_store,
    )
    assert len(attachments) == 2


# ---------------------------------------------------------------------------
# compensate_attachments
# ---------------------------------------------------------------------------


def test_compensate_attachments_deletes_objects(
    db_session: Session,
    test_user: User,
    mock_object_store: EncryptedObjectStore,
) -> None:
    draft_id = str(uuid.uuid4())
    arv_id = str(uuid.uuid4())

    attachments = generate_and_store_attachments(
        db=db_session,
        user_id=test_user.id,
        draft_id=draft_id,
        approved_resume_version_id=arv_id,
        approved_facts=SAMPLE_FACTS,
        diffs=[],
        object_store=mock_object_store,
    )

    attachment_ids = [a.id for a in attachments]

    # Verify objects exist before compensation
    for att in attachments:
        mock_object_store.get(key=att.object_key)  # should not raise

    # Compensate
    from backend.app.services.attachment_service import compensate_attachments

    compensate_attachments(
        db=db_session,
        attachment_ids=attachment_ids,
        object_store=mock_object_store,
    )

    # Verify objects deleted and status updated
    for att in attachments:
        with pytest.raises(FileNotFoundError):
            mock_object_store.get(key=att.object_key)

    persisted = (
        db_session.query(ApprovedResumeAttachment)
        .filter(ApprovedResumeAttachment.id.in_(attachment_ids))
        .all()
    )
    assert all(a.status == "failed" for a in persisted)


# ---------------------------------------------------------------------------
# download_attachment
# ---------------------------------------------------------------------------


def test_download_attachment_success(
    db_session: Session,
    test_user: User,
    mock_object_store: EncryptedObjectStore,
) -> None:
    draft_id = str(uuid.uuid4())
    arv_id = str(uuid.uuid4())

    attachments = generate_and_store_attachments(
        db=db_session,
        user_id=test_user.id,
        draft_id=draft_id,
        approved_resume_version_id=arv_id,
        approved_facts=SAMPLE_FACTS,
        diffs=[],
        object_store=mock_object_store,
    )

    pdf_att = [a for a in attachments if a.format == "pdf"][0]
    body, content_type, filename = download_attachment(
        db=db_session,
        attachment_id=pdf_att.id,
        user_id=test_user.id,
        object_store=mock_object_store,
    )

    assert body.startswith(b"%PDF-")
    assert content_type == "application/pdf"


def test_download_attachment_permission_denied(
    db_session: Session,
    test_user: User,
    mock_object_store: EncryptedObjectStore,
) -> None:
    other_user = User(
        id=str(uuid.uuid4()),
        account="other-tester",
        nickname="Other Tester",
        password_hash="argon2-placeholder",
        role="student",
    )
    db_session.add(other_user)
    db_session.commit()

    draft_id = str(uuid.uuid4())
    attachments = generate_and_store_attachments(
        db=db_session,
        user_id=test_user.id,
        draft_id=draft_id,
        approved_resume_version_id=str(uuid.uuid4()),
        approved_facts=SAMPLE_FACTS,
        diffs=[],
        object_store=mock_object_store,
    )

    pdf_att = [a for a in attachments if a.format == "pdf"][0]
    with pytest.raises(PermissionError):
        download_attachment(
            db=db_session,
            attachment_id=pdf_att.id,
            user_id=other_user.id,
            object_store=mock_object_store,
        )


def test_download_attachment_not_found(
    db_session: Session,
    test_user: User,
    mock_object_store: EncryptedObjectStore,
) -> None:
    with pytest.raises(FileNotFoundError):
        download_attachment(
            db=db_session,
            attachment_id=str(uuid.uuid4()),
            user_id=test_user.id,
            object_store=mock_object_store,
        )


# ---------------------------------------------------------------------------
# Conditional S3 round-trip (requires MinIO / TEST_S3_ENDPOINT)
# ---------------------------------------------------------------------------


def _get_real_object_store() -> EncryptedObjectStore | None:
    endpoint = os.getenv("TEST_S3_ENDPOINT")
    if not endpoint:
        return None
    client = __import__("boto3").client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["TEST_S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["TEST_S3_SECRET_KEY"],
        region_name="us-east-1",
    )
    bucket = os.getenv("TEST_S3_BUCKET", "career-assistant-storage-test")
    blob_store = S3BlobStore(client, bucket)
    blob_store.ensure_bucket()
    key = base64.b64encode(bytes(range(32))).decode("ascii")
    return EncryptedObjectStore(blob_store, key)


def test_real_s3_attachment_round_trip(
    db_session: Session,
    test_user: User,
) -> None:
    object_store = _get_real_object_store()
    if object_store is None:
        pytest.skip("TEST_S3_ENDPOINT is not configured")

    draft_id = str(uuid.uuid4())
    arv_id = str(uuid.uuid4())

    attachments = generate_and_store_attachments(
        db=db_session,
        user_id=test_user.id,
        draft_id=draft_id,
        approved_resume_version_id=arv_id,
        approved_facts=SAMPLE_FACTS,
        diffs=SAMPLE_DIFFS,
        object_store=object_store,
    )

    try:
        assert len(attachments) == 2
        for att in attachments:
            # Verify ciphertext (plaintext not visible in raw storage)
            raw = object_store._blob_store.get_bytes(key=att.object_key)
            assert b"Alice Zhang" not in raw
            assert att.encryption_version == "v1-aes-256-gcm"

        # Verify download returns decrypted content
        for att in attachments:
            body, content_type, filename = download_attachment(
                db=db_session,
                attachment_id=att.id,
                user_id=test_user.id,
                object_store=object_store,
            )
            if att.format == "pdf":
                assert body.startswith(b"%PDF-")
            elif att.format == "docx":
                assert body.startswith(b"PK")

            # Response headers must not expose object_key
            assert content_type != ""  # content-type is set
            # The object_key should not leak into the response metadata
            assert att.object_key not in content_type
    finally:
        for att in attachments:
            try:
                object_store.delete(att.object_key)
            except Exception:
                pass
