from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_JOB_URL_LENGTH = 4096
MAX_JD_TEXT_LENGTH = 100_000
DUPLICATE_ALGORITHM_VERSION = "manual-job-dedup-v1"
MIN_TEXT_OVERLAP_BPS = 7200


class SubmissionInputType(StrEnum):
    URL = "url"
    JD_TEXT = "jd_text"


class SubmissionStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class DeduplicationStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobSourceLinkType(StrEnum):
    TENCENT_SMARTSHEET = "tencent_smartsheet"
    USER_SUBMISSION = "user_submission"


class InvalidSubmissionInput(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class NormalizedSubmission:
    input_type: SubmissionInputType
    original_url: str | None
    original_jd: str | None
    normalized_url: str | None
    normalized_text: str | None
    content_sha256: str
    fingerprint: str
    preview: str


@dataclass(frozen=True)
class JobFingerprint:
    job_id: str
    apply_url: str | None
    description_text: str | None


@dataclass(frozen=True)
class DuplicateMatch:
    job_id: str
    score_basis_points: int
    reasons: tuple[str, ...]
    score_components: dict[str, int]
    algorithm_version: str = DUPLICATE_ALGORITHM_VERSION


def _canonicalize_url(value: str) -> str:
    if len(value) > MAX_JOB_URL_LENGTH:
        raise InvalidSubmissionInput("job_url_too_large")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise InvalidSubmissionInput("invalid_job_url") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise InvalidSubmissionInput("invalid_job_url")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidSubmissionInput("unsafe_job_url")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise InvalidSubmissionInput("unsafe_job_url")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise InvalidSubmissionInput("unsafe_job_url")
    scheme = parsed.scheme.lower()
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = host if port is None or default_port else f"{host}:{port}"
    safe_query = sorted(
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    )
    return urlunsplit((scheme, netloc, parsed.path or "/", urlencode(safe_query), ""))


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _preview(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())[:240]


def normalize_submission_input(
    input_type: SubmissionInputType, raw_value: str
) -> NormalizedSubmission:
    if input_type is SubmissionInputType.URL:
        canonical = _canonicalize_url(raw_value)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return NormalizedSubmission(
            input_type=input_type,
            original_url=raw_value.strip(),
            original_jd=None,
            normalized_url=canonical,
            normalized_text=None,
            content_sha256=digest,
            fingerprint=digest,
            preview=canonical[:240],
        )
    if len(raw_value) > MAX_JD_TEXT_LENGTH:
        raise InvalidSubmissionInput("job_description_too_large")
    normalized = _normalize_text(raw_value)
    if not normalized:
        raise InvalidSubmissionInput("empty_job_description")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return NormalizedSubmission(
        input_type=input_type,
        original_url=None,
        original_jd=raw_value,
        normalized_url=None,
        normalized_text=normalized,
        content_sha256=digest,
        fingerprint=digest,
        preview=_preview(raw_value),
    )


def _tokens(value: str) -> set[str]:
    ascii_tokens = set(re.findall(r"[a-z0-9+#.]{2,}", value.lower()))
    han_runs = re.findall(r"[\u4e00-\u9fff]+", value)
    han_bigrams = {
        run[index : index + 2]
        for run in han_runs
        for index in range(max(0, len(run) - 1))
    }
    return ascii_tokens | han_bigrams


class DuplicateDetector:
    def find_candidates(
        self,
        submission: NormalizedSubmission,
        jobs: list[JobFingerprint],
    ) -> list[DuplicateMatch]:
        matches: list[DuplicateMatch] = []
        for job in jobs:
            if submission.normalized_url and job.apply_url:
                try:
                    canonical_job_url = _canonicalize_url(job.apply_url)
                except InvalidSubmissionInput:
                    canonical_job_url = None
                if canonical_job_url == submission.normalized_url:
                    matches.append(
                        DuplicateMatch(
                            job_id=job.job_id,
                            score_basis_points=10_000,
                            reasons=("canonical_apply_url_exact",),
                            score_components={"canonical_url": 10_000},
                        )
                    )
                    continue
            if submission.normalized_text and job.description_text:
                left = _tokens(submission.normalized_text)
                right = _tokens(_normalize_text(job.description_text))
                union = left | right
                score = round(10_000 * len(left & right) / len(union)) if union else 0
                if score >= MIN_TEXT_OVERLAP_BPS:
                    matches.append(
                        DuplicateMatch(
                            job_id=job.job_id,
                            score_basis_points=score,
                            reasons=("jd_token_overlap",),
                            score_components={"jd_token_jaccard": score},
                        )
                    )
        return sorted(matches, key=lambda item: (-item.score_basis_points, item.job_id))
