"""Unit tests for error_classifier."""
from __future__ import annotations

import pytest
from backend.app.services.job_discovery.strategy.error_classifier import classify_error


@pytest.mark.parametrize("message,expected", [
    ("Connection timed out after 30 seconds", "network_timeout"),
    ("ReadTimeout: HTTPSConnectionPool", "network_timeout"),
    ("timed out waiting for response", "network_timeout"),
    ("HTTP 403 Forbidden", "http_blocked"),
    ("401 Unauthorized access", "http_blocked"),
    ("captcha required to proceed", "captcha"),
    ("请完成验证后继续访问", "captcha"),
    ("滑块验证码", "captcha"),
    ("环境异常，完成验证后即可继续访问", "wechat_blocked"),
    ("wechat verification wall detected", "wechat_blocked"),
    ("404 Not Found", "site_changed"),
    ("页面不存在", "site_changed"),
    ("no text content found on page", "empty_text"),
    ("empty body returned", "empty_text"),
    ("JSONDecodeError: Expecting value", "parse_error"),
    ("unexpected format in response", "parse_error"),
    ("OCR engine returned no text", "ocr_failed"),
    ("tesseract failed to initialize", "ocr_failed"),
    ("paddle could not process image", "ocr_failed"),
    ("some random other error", "unknown"),
    ("", "unknown"),
])
def test_classify_error(message, expected):
    assert classify_error(message) == expected


def test_classify_error_case_insensitive():
    assert classify_error("TIMEOUT ERROR") == "network_timeout"
    assert classify_error("Captcha Required") == "captcha"
