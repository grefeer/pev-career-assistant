from io import BytesIO
from pathlib import Path

from docx import Document
from pypdf import PdfWriter

from backend.app.services.profile_parser import (
    extract_evidence_candidates,
    extract_resume_document,
)


def _docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_text_and_docx_extract_reviewable_text() -> None:
    plain = extract_resume_document("resume.txt", "张三\n技能\nPython".encode())
    docx = extract_resume_document("resume.docx", _docx_bytes("张三\n技能\nPython"))
    assert plain.needs_manual_entry is False
    assert docx.needs_manual_entry is False
    assert "Python" in plain.text
    assert "Python" in docx.text


def test_text_pdf_extracts_reviewable_text_and_candidates() -> None:
    parsed = extract_resume_document(
        "sample_resume.pdf",
        Path("data/sample_resume.pdf").read_bytes(),
    )
    assert parsed.needs_manual_entry is False
    assert parsed.text
    assert extract_evidence_candidates(parsed.text)


def test_image_only_pdf_and_broken_docx_need_manual_entry() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = BytesIO()
    writer.write(buffer)
    blank = extract_resume_document("scan.pdf", buffer.getvalue())
    broken = extract_resume_document("broken.docx", b"not-a-docx")
    assert (blank.needs_manual_entry, blank.error_code) == (
        True,
        "resume_text_unavailable",
    )
    assert (broken.needs_manual_entry, broken.error_code) == (
        True,
        "resume_parse_unreadable",
    )
    broken_pdf = extract_resume_document("broken.pdf", b"not-a-pdf")
    assert (broken_pdf.needs_manual_entry, broken_pdf.error_code) == (
        True,
        "resume_parse_unreadable",
    )


def test_candidate_extraction_covers_standard_profile_sections() -> None:
    candidates = extract_evidence_candidates(
        "张三\nzhang@example.com\n教育经历\n某大学 软件工程\n"
        "项目经历\n职业助手 LangGraph\n技能\nPython、FastAPI\n"
        "获奖\n一等奖\n证书\n英语六级\n作品链接\nhttps://example.com/me"
    )
    by_path = {item.field_path: item for item in candidates}
    assert by_path["basics.name"].candidate_value == "张三"
    assert by_path["basics.email"].candidate_value == "zhang@example.com"
    assert by_path["education"].candidate_value == ["某大学 软件工程"]
    assert by_path["projects"].candidate_value == ["职业助手 LangGraph"]
    assert by_path["skills"].candidate_value == ["Python", "FastAPI"]
    assert by_path["portfolio_links"].candidate_value == ["https://example.com/me"]
    assert all(0 <= item.confidence <= 100 for item in candidates)


def test_candidate_extraction_skips_blank_lines_and_captures_phone() -> None:
    """Blank lines are ignored and a mobile number is captured exactly once."""
    candidates = extract_evidence_candidates(
        "李四\n\n联系方式\n13800138000\n技能\nPython\n"
    )
    by_path = {item.field_path: item for item in candidates}
    assert by_path["basics.name"].candidate_value == "李四"
    assert by_path["basics.phone"].candidate_value == "13800138000"


def test_candidate_extraction_deduplicates_repeated_urls_on_one_line() -> None:
    """A URL repeated on a single line is captured once, not twice."""
    candidates = extract_evidence_candidates(
        "张三\n参考 https://example.com 与 https://example.com 同一链接\n"
    )
    by_path = {item.field_path: item for item in candidates}
    assert by_path["portfolio_links"].candidate_value == ["https://example.com"]


def test_candidate_extraction_ignores_an_unstructured_first_line_longer_than_a_name() -> None:
    """A first line too long to be a name and without a heading yields no candidate."""
    candidates = extract_evidence_candidates("x" * 130)
    assert candidates == []
