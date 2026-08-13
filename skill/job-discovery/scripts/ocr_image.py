#!/usr/bin/env python3
"""ocr_image.py — Extract text from images using available OCR backends.

Usage:
  ocr_image.py <image_path> [--engine auto|paddleocr|tesseract|vision]
                             [--out <dir>] [--no-cache]

OCR backends (tried in order unless --engine specified):
  vision  — pi-agent's built-in vision capability (always available)
  paddleocr — PaddleOCR with Chinese model (requires: pip install paddleocr)
  tesseract — Tesseract with chi_sim+eng (requires: pip install pytesseract + tesseract)
  auto    — try vision → paddleocr → tesseract

Output (stdout): JSON object with status, full_text, confidence, warnings.
Exit code 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

# PaddleX defaults CPU inference to oneDNN run_mode; the PIR -> oneDNN
# attribute converter is broken in paddlepaddle >= 3.3.0 on Windows
# (Paddle#77340: ConvertPirAttribute2RuntimeAttribute not support
# ArrayAttribute<DoubleAttribute>), so every CPU OCR call crashes. The
# env var is read at paddlex import time, hence it MUST be set here at
# module scope — before any code path (including _check_paddleocr)
# can import paddleocr.
if "PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT" not in os.environ:
    os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "0"
if "FLAGS_use_onednn" not in os.environ:
    os.environ["FLAGS_use_onednn"] = "0"


# ---------------------------------------------------------------------------
# Image inspection helpers (zero dependencies, pure stdlib)
# ---------------------------------------------------------------------------

def _parse_png_dimensions(image_bytes: bytes) -> tuple[int, int] | None:
    """Parse PNG width/height from raw bytes (no PIL needed)."""
    if len(image_bytes) < 33:
        return None
    if image_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        width = struct.unpack(">I", image_bytes[16:20])[0]
        height = struct.unpack(">I", image_bytes[20:24])[0]
    except (struct.error, IndexError):
        return None
    return (width, height)


def _parse_jpeg_dimensions(image_bytes: bytes) -> tuple[int, int] | None:
    """Parse JPEG width/height from raw bytes (no PIL needed)."""
    if len(image_bytes) < 4:
        return None
    if image_bytes[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(image_bytes) - 1:
        if image_bytes[i] != 0xFF:
            i += 1
            continue
        marker = image_bytes[i + 1]
        if marker == 0x00:
            i += 2
            continue
        if marker in (0xC0, 0xC1, 0xC2):
            if i + 11 > len(image_bytes):
                return None
            try:
                height = struct.unpack(">H", image_bytes[i + 5 : i + 7])[0]
                width = struct.unpack(">H", image_bytes[i + 7 : i + 9])[0]
            except (struct.error, IndexError):
                return None
            return (width, height)
        if i + 3 > len(image_bytes):
            break
        seg_len = struct.unpack(">H", image_bytes[i + 2 : i + 4])[0]
        i += 2 + seg_len
        if seg_len == 0:
            break
    return None


def _detect_suffix(image_bytes: bytes) -> str:
    """Detect image format from magic bytes."""
    if len(image_bytes) >= 8 and image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if len(image_bytes) >= 2 and image_bytes[:2] == b"\xff\xd8":
        return ".jpg"
    if len(image_bytes) >= 4 and image_bytes[:4] == b"RIFF":
        return ".webp"
    if len(image_bytes) >= 4 and image_bytes[:4] == b"GIF8":
        return ".gif"
    return ".png"


def _get_dimensions(image_bytes: bytes) -> tuple[int, int] | None:
    """Get image (width, height) for supported formats."""
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return _parse_png_dimensions(image_bytes)
    if image_bytes[:2] == b"\xff\xd8":
        return _parse_jpeg_dimensions(image_bytes)
    return None


# ---------------------------------------------------------------------------
# OCR backends
# ---------------------------------------------------------------------------

_SLICE_HEIGHT_THRESHOLD = 2000
_SLICE_OVERLAP = 100
_MIN_TEXT_CONFIDENCE = 0.5


def _ocr_cache_key(content_hash: str, engine: str) -> str:
    """Use the full image digest and backend in the cache identity."""
    return f"{engine}:{content_hash}"


def _read_ocr_cache(cache_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_ocr_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    """Write the cache atomically so concurrent workers never see half JSON."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_path.parent,
            prefix=f"{cache_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(cache, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, cache_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _check_paddleocr() -> bool:
    try:
        import paddleocr  # noqa: F401
        return True
    except ImportError:
        return False


def _check_tesseract() -> bool:
    try:
        import pytesseract  # noqa: F401
        return True
    except ImportError:
        return False


def _check_pil() -> bool:
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


def _run_paddleocr_on_bytes(image_bytes: bytes, suffix: str = ".png") -> tuple[str, float, list[str]]:
    """Run PaddleOCR on a single image (in-memory bytes).

    This is the low-level worker — it writes bytes to a temp file, runs
    PaddleOCR, and returns (full_text, avg_confidence, warnings).

    NOTE: the oneDNN-disabling env vars (Paddle#77340 workaround) are set at
    module scope above — they must be in place before this module's first
    paddleocr import, which can happen earlier via _check_paddleocr().
    """
    import paddleocr

    tmp_path = None
    warnings: list[str] = []
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(image_bytes)
            tmp_path = f.name

        ocr = paddleocr.PaddleOCR(
            use_textline_orientation=True,
            lang="ch",
            ocr_version="PP-OCRv5",
        )
        result = ocr.predict(tmp_path)

        all_texts: list[str] = []
        all_scores: list[float] = []
        for page in result:
            for text, score in zip(page["rec_texts"], page["rec_scores"]):
                if score >= _MIN_TEXT_CONFIDENCE and text.strip():
                    all_texts.append(text.strip())
                    all_scores.append(score)

        if not all_texts:
            warnings.append("PaddleOCR found no text above confidence threshold")
            return "", 0.0, warnings

        full_text = "\n".join(all_texts)
        avg_conf = sum(all_scores) / len(all_scores)
        return full_text, round(avg_conf, 4), warnings

    except Exception as exc:
        warnings.append(f"PaddleOCR error: {exc}")
        return "", 0.0, warnings
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


def _run_paddleocr(image_bytes: bytes, suffix: str) -> tuple[str, float, list[str]]:
    """Run PaddleOCR with auto-slicing for tall images.

    When the image is taller than _SLICE_HEIGHT_THRESHOLD (2000px) and PIL
    is available, the image is split into horizontal slices with _SLICE_OVERLAP
    (100px) overlap. Each slice is OCR'd independently, then results are
    concatenated. This avoids PaddleOCR timeouts on ultra-tall images (e.g.
    8000px+ WeChat article screenshots).

    When PIL is not available or the image fits within the limit, falls back
    to single-pass OCR.
    """
    dims = _get_dimensions(image_bytes)

    # Fast path: image fits in one pass, or PIL unavailable
    if dims is None or dims[1] <= _SLICE_HEIGHT_THRESHOLD or not _check_pil():
        if dims and dims[1] <= _SLICE_HEIGHT_THRESHOLD:
            pass  # fits — single pass
        elif not _check_pil():
            pass  # can't slice without PIL
        return _run_paddleocr_on_bytes(image_bytes, suffix)

    # Tall image with PIL available — slice and OCR
    from PIL import Image
    from io import BytesIO

    img = Image.open(BytesIO(image_bytes))
    w, h = img.size
    slice_count = (h + _SLICE_HEIGHT_THRESHOLD - 1) // _SLICE_HEIGHT_THRESHOLD

    all_texts: list[str] = []
    all_scores: list[float] = []
    all_warnings: list[str] = [
        f"Tall image ({w}x{h}) auto-sliced into {slice_count} chunks "
        f"(slice_height={_SLICE_HEIGHT_THRESHOLD}, overlap={_SLICE_OVERLAP})"
    ]

    for i in range(slice_count):
        y_start = max(0, i * _SLICE_HEIGHT_THRESHOLD - (i > 0) * _SLICE_OVERLAP)
        y_end = min(h, (i + 1) * _SLICE_HEIGHT_THRESHOLD + (i < slice_count - 1) * _SLICE_OVERLAP)
        chunk = img.crop((0, y_start, w, y_end))

        buf = BytesIO()
        chunk.save(buf, format="PNG")
        chunk_bytes = buf.getvalue()

        text, conf, warns = _run_paddleocr_on_bytes(chunk_bytes, ".png")
        if text:
            all_texts.append(text)
            all_scores.append(conf)
        all_warnings.extend(warns)

    if not all_texts:
        all_warnings.append("All slices produced no text")
        return "", 0.0, all_warnings

    full_text = "\n".join(all_texts)
    avg_conf = round(sum(all_scores) / len(all_scores), 4)
    return full_text, avg_conf, all_warnings


def _run_tesseract(image_bytes: bytes) -> tuple[str, float, list[str]]:
    """Run Tesseract OCR with Chinese + English."""
    warnings: list[str] = []
    try:
        import pytesseract
        from PIL import Image
        from io import BytesIO

        img = Image.open(BytesIO(image_bytes))
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, lang="chi_sim+eng")

        texts: list[str] = []
        confs: list[float] = []
        for text, conf in zip(data["text"], data["conf"]):
            conf_f = float(conf) / 100.0
            if text.strip() and conf_f >= _MIN_TEXT_CONFIDENCE:
                texts.append(text.strip())
                confs.append(conf_f)

        if not texts:
            warnings.append("Tesseract found no text")
            return "", 0.0, warnings

        full_text = "\n".join(texts)
        avg_conf = sum(confs) / len(confs)
        return full_text, round(avg_conf, 4), warnings

    except Exception as exc:
        warnings.append(f"Tesseract error: {exc}")
        return "", 0.0, warnings


# ---------------------------------------------------------------------------
# Vision-based OCR (pi-agent built-in)
# ---------------------------------------------------------------------------
# This is a placeholder that instructs the caller to use pi-agent's vision.
# When called from within pi-agent, the LLM should read the image file and
# extract text directly. When called standalone, this returns a prompt for
# manual processing.

def _run_vision_placeholder(image_path: str) -> tuple[str, float, list[str]]:
    """Return a structured message indicating vision-based OCR is needed.

    When integrated with pi-agent, the agent's LLM reads the image and extracts
    text. This function exists so that the CLI always succeeds — the real vision
    extraction happens when pi-agent reads the output JSON and sees the
    `requires_vision` flag.
    """
    return (
        f"[VISION_REQUIRED] Image at {image_path} — use vision capability to read text from this image. "
        f"Read the image with the read tool and extract all visible job descriptions, "
        f"company names, requirements, and application channels.",
        0.0,
        ["Vision-based OCR — invoke read tool on image, then extract text"],
    )


# ---------------------------------------------------------------------------
# Main OCR orchestration
# ---------------------------------------------------------------------------

def ocr_image(
    image_path: str,
    engine: str = "auto",
    out_dir: Path | None = None,
    no_cache: bool = False,
) -> dict[str, Any]:
    """Run OCR on an image file and return structured result."""

    path = Path(image_path)
    if not path.exists():
        return {"status": "error", "error": f"File not found: {image_path}"}

    image_bytes = path.read_bytes()
    if not image_bytes:
        return {"status": "error", "error": "Empty image file"}

    # Cache key
    content_hash = hashlib.sha256(image_bytes).hexdigest()
    short_hash = f"sha256_{content_hash[:16]}"
    cache_key = _ocr_cache_key(content_hash, engine)

    # Check cache
    if out_dir and not no_cache:
        cache_path = out_dir / "ocr_cache.json"
        cache = _read_ocr_cache(cache_path)
        if cache_key in cache and isinstance(cache[cache_key], dict):
            cached = dict(cache[cache_key])
            cached["cached"] = True
            return cached

    suffix = _detect_suffix(image_bytes)
    dims = _get_dimensions(image_bytes)
    dimensions_info = {"width": dims[0], "height": dims[1]} if dims else None

    # Determine backends to try
    if engine == "vision":
        backends = ["vision"]
    elif engine == "paddleocr":
        backends = ["paddleocr"]
    elif engine == "tesseract":
        backends = ["tesseract"]
    else:  # auto
        backends = ["vision", "paddleocr", "tesseract"]

    all_warnings: list[str] = []
    full_text = ""
    confidence = 0.0
    used_backend = ""

    for backend in backends:
        if backend == "vision":
            text, conf, warns = _run_vision_placeholder(image_path)
            if text and not text.startswith("[VISION_REQUIRED]"):
                full_text = text
                confidence = conf
                used_backend = "vision"
                all_warnings = warns
                break
            elif "VISION_REQUIRED" in text:
                used_backend = "vision_pending"
                # Don't break — try other backends too
                continue

        elif backend == "paddleocr":
            if not _check_paddleocr():
                all_warnings.append("PaddleOCR not installed (pip install paddleocr)")
                continue
            text, conf, warns = _run_paddleocr(image_bytes, suffix)
            all_warnings.extend(warns)
            if text:
                full_text = text
                confidence = conf
                used_backend = "paddleocr"
                break

        elif backend == "tesseract":
            if not _check_tesseract():
                all_warnings.append("Tesseract not installed (pip install pytesseract)")
                continue
            text, conf, warns = _run_tesseract(image_bytes)
            all_warnings.extend(warns)
            if text:
                full_text = text
                confidence = conf
                used_backend = "tesseract"
                break

    # Determine review status
    needs_manual_review = False
    if used_backend == "vision_pending":
        needs_manual_review = False  # pi-agent will handle via vision
    elif not full_text:
        needs_manual_review = True
        all_warnings.append("All OCR backends failed — manual review required")
    elif confidence < 0.6:
        all_warnings.append(f"Low OCR confidence ({confidence})")

    # Check for tall images
    if dims and dims[1] > _SLICE_HEIGHT_THRESHOLD:
        all_warnings.append(
            f"Tall image ({dims[1]}px) — consider slicing for best results"
        )

    result: dict[str, Any] = {
        "status": "ok",
        "image_path": str(path),
        "content_hash": short_hash,
        "dimensions": dimensions_info,
        "format": suffix.lstrip("."),
        "engine": used_backend,
        "full_text": full_text,
        "confidence": confidence,
        "text_length": len(full_text),
        "warnings": all_warnings,
        "needs_manual_review": needs_manual_review,
    }

    # Save to cache
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        cache_path = out_dir / "ocr_cache.json"
        cache = _read_ocr_cache(cache_path)
        cache[cache_key] = result
        _write_ocr_cache(cache_path, cache)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="OCR text extraction from images")
    parser.add_argument("image", help="Path to image file")
    parser.add_argument("--engine", choices=["auto", "paddleocr", "tesseract", "vision"],
                        default="auto", help="OCR backend (default: auto)")
    parser.add_argument("--out", default=None, help="Output directory for cache")
    parser.add_argument("--no-cache", action="store_true", help="Skip OCR result cache")

    args = parser.parse_args()
    out_dir = Path(args.out) if args.out else None
    result = ocr_image(args.image, engine=args.engine, out_dir=out_dir, no_cache=args.no_cache)

    # Write JSON via stdout buffer to avoid Windows GBK/UTF-8 encoding
    # corruption. On Windows terminals, print() may encode through the
    # console code page (e.g. cp936), mangling Chinese characters.
    # Writing raw UTF-8 bytes to the buffer bypasses this.
    try:
        json_bytes = json.dumps(result, ensure_ascii=False).encode("utf-8")
        sys.stdout.buffer.write(json_bytes + b"\n")
        sys.stdout.buffer.flush()
    except (OSError, AttributeError):
        # Fallback for environments without a .buffer (e.g. some IDEs)
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))

    sys.exit(0 if result["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
