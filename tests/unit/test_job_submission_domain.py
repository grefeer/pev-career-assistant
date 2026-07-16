import pytest

from backend.app.domain.job_submissions import (
    DuplicateDetector,
    InvalidSubmissionInput,
    JobFingerprint,
    SubmissionInputType,
    normalize_submission_input,
)


@pytest.mark.parametrize(
    "value,expected_code",
    [
        ("ftp://jobs.example.com/1", "invalid_job_url"),
        ("https://user:secret@jobs.example.com/1", "unsafe_job_url"),
        ("http://localhost/jobs/1", "unsafe_job_url"),
        ("http://127.0.0.1/jobs/1", "unsafe_job_url"),
        ("http://169.254.169.254/latest/meta-data", "unsafe_job_url"),
        ("http://10.0.0.8/jobs/1", "unsafe_job_url"),
        ("http://192.168.1.8/jobs/1", "unsafe_job_url"),
        ("http://[::1]/jobs/1", "unsafe_job_url"),
        ("https://careers.internal/jobs/1", "unsafe_job_url"),
    ],
)
def test_url_input_rejects_unsafe_targets(value: str, expected_code: str) -> None:
    with pytest.raises(InvalidSubmissionInput) as exc_info:
        normalize_submission_input(SubmissionInputType.URL, value)
    assert exc_info.value.error_code == expected_code


def test_url_input_canonicalizes_without_fetching() -> None:
    result = normalize_submission_input(
        SubmissionInputType.URL,
        "HTTPS://Jobs.Example.COM:443/opening/1?utm_source=feed&lang=zh#apply",
    )
    assert result.normalized_url == "https://jobs.example.com/opening/1?lang=zh"
    assert result.content_sha256 == result.fingerprint
    assert result.preview == "https://jobs.example.com/opening/1?lang=zh"


def test_jd_text_has_explicit_size_boundary_and_redacted_preview() -> None:
    result = normalize_submission_input(
        SubmissionInputType.JD_TEXT,
        "  后端实习生\r\n负责 FastAPI   与 MySQL 开发。  ",
    )
    assert result.normalized_text == "后端实习生 负责 fastapi 与 mysql 开发。"
    assert result.preview == "后端实习生 负责 FastAPI 与 MySQL 开发。"
    assert len(result.preview) <= 240
    with pytest.raises(InvalidSubmissionInput) as exc_info:
        normalize_submission_input(SubmissionInputType.JD_TEXT, "x" * 100_001)
    assert exc_info.value.error_code == "job_description_too_large"


def test_duplicate_detector_returns_stable_explanations_without_merging() -> None:
    submission = normalize_submission_input(
        SubmissionInputType.URL,
        "https://jobs.example.com/opening/1?utm_campaign=summer",
    )
    matches = DuplicateDetector().find_candidates(
        submission,
        [
            JobFingerprint(
                job_id="job-1",
                apply_url="https://jobs.example.com/opening/1",
                description_text="不同文本",
            ),
            JobFingerprint(
                job_id="job-2",
                apply_url="https://jobs.example.com/opening/2",
                description_text="不同文本",
            ),
        ],
    )
    assert [(item.job_id, item.score_basis_points) for item in matches] == [
        ("job-1", 10_000)
    ]
    assert matches[0].reasons == ("canonical_apply_url_exact",)
    assert matches[0].score_components == {"canonical_url": 10_000}
    assert matches[0].algorithm_version == "manual-job-dedup-v1"


def test_jd_overlap_below_threshold_is_not_a_candidate() -> None:
    submission = normalize_submission_input(
        SubmissionInputType.JD_TEXT,
        "负责 python fastapi mysql redis 后端服务开发和测试",
    )
    matches = DuplicateDetector().find_candidates(
        submission,
        [
            JobFingerprint("job-match", None, "负责 Python FastAPI MySQL Redis 后端服务开发和测试"),
            JobFingerprint("job-noise", None, "市场运营 内容编辑 品牌活动"),
        ],
    )
    assert [item.job_id for item in matches] == ["job-match"]
    assert matches[0].reasons == ("jd_token_overlap",)
    assert matches[0].score_basis_points >= 7200


from pydantic import ValidationError

from backend.app.api.job_submission_schemas import (
    AdminJobSubmissionDecisionRequest,
    JobSubmissionCreateRequest,
)


def test_create_request_requires_exactly_one_matching_input() -> None:
    request = JobSubmissionCreateRequest(input_type="url", url="https://jobs.example.com/1")
    assert request.jd_text is None
    with pytest.raises(ValidationError):
        JobSubmissionCreateRequest(input_type="url", url=None, jd_text="JD")


def test_admin_decision_is_discriminated_and_complete() -> None:
    request = AdminJobSubmissionDecisionRequest(
        expected_version=2,
        action="create_pending",
        company_name="示例科技",
        title="后端实习生",
        apply_url="https://jobs.example.com/1",
    )
    assert request.job_id is None
    with pytest.raises(ValidationError):
        AdminJobSubmissionDecisionRequest(expected_version=2, action="link_existing")
