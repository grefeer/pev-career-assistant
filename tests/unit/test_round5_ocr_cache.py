"""Round 5 regression tests for content-addressed OCR cache entries."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _ocr_module():
    path = Path(__file__).parents[2] / "skill" / "job-discovery" / "scripts" / "ocr_image.py"
    spec = importlib.util.spec_from_file_location("round5_ocr_image", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ocr_cache_key_uses_full_content_hash_and_engine() -> None:
    module = _ocr_module()
    digest = "a" * 64

    assert module._ocr_cache_key(digest, "paddleocr") == f"paddleocr:{digest}"
    assert module._ocr_cache_key(digest, "tesseract") != module._ocr_cache_key(
        digest, "paddleocr"
    )


def test_ocr_cache_write_is_atomic(tmp_path) -> None:
    module = _ocr_module()
    cache = tmp_path / "ocr_cache.json"
    module._write_ocr_cache(cache, {"paddleocr:abc": {"status": "ok"}})

    assert cache.exists()
    assert not list(tmp_path.glob("*.tmp"))
