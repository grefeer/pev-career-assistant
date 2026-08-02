from __future__ import annotations

import logging
import os
import struct
import tempfile
from pathlib import Path

from backend.app.services.job_discovery.schemas import OcrResult

logger = logging.getLogger(__name__)

# Height threshold for slicing tall images (pixels).
_SLICE_HEIGHT_THRESHOLD = 2000
# Overlap between consecutive slices (pixels).
_SLICE_OVERLAP = 100

# Minimum confidence threshold for considering OCR text as valid.
_MIN_TEXT_CONFIDENCE = 0.5

# Default OCR version for PaddleOCR (v5 avoids PIR/oneDNN compatibility issues on Windows).
_DEFAULT_OCR_VERSION = "PP-OCRv5"


def _parse_png_dimensions(image_bytes: bytes) -> tuple[int, int] | None:
    """Parse PNG dimensions from raw bytes without PIL dependency.

    PNG format: signature (8 bytes) then IHDR chunk:
      - 4 bytes: length (always 13 for IHDR)
      - 4 bytes: 'IHDR'
      - 4 bytes: width (big-endian)
      - 4 bytes: height (big-endian)

    Returns (width, height) or None if parsing fails.
    """
    if len(image_bytes) < 33:
        return None
    if image_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    # Skip signature (8) + chunk length (4) + chunk type 'IHDR' (4) = 16
    try:
        width = struct.unpack(">I", image_bytes[16:20])[0]
        height = struct.unpack(">I", image_bytes[20:24])[0]
    except (struct.error, IndexError):
        return None
    return (width, height)


def _parse_jpeg_dimensions(image_bytes: bytes) -> tuple[int, int] | None:
    """Parse JPEG dimensions from raw bytes without PIL dependency.

    JPEG starts with marker FF D8. Dimensions are in SOF0 (Start Of Frame)
    markers: FF C0 (or C1/C2) followed by length (2), precision (1),
    height (2), width (2).

    Returns (width, height) or None if parsing fails.
    """
    if len(image_bytes) < 4:
        return None
    if image_bytes[:2] != b"\xff\xd8":
        return None

    i = 2
    while i < len(image_bytes) - 1:
        # Find next marker (FF xx)
        if image_bytes[i] != 0xFF:
            i += 1
            continue
        marker = image_bytes[i + 1]
        if marker == 0x00:  # escaped FF
            i += 2
            continue
        if marker in (0xC0, 0xC1, 0xC2):  # SOF0, SOF1, SOF2
            if i + 11 > len(image_bytes):
                return None
            try:
                height = struct.unpack(">H", image_bytes[i + 5 : i + 7])[0]
                width = struct.unpack(">H", image_bytes[i + 7 : i + 9])[0]
            except (struct.error, IndexError):
                return None
            return (width, height)
        # Skip to next marker: segment length is at offset +2
        if i + 3 > len(image_bytes):
            break
        seg_len = struct.unpack(">H", image_bytes[i + 2 : i + 4])[0]
        i += 2 + seg_len
        if seg_len == 0:
            break

    return None


def _get_image_dimensions(image_bytes: bytes) -> tuple[int, int] | None:
    """Get image dimensions from raw bytes.

    Supports PNG and JPEG. Falls back to returning None (will trigger
    manual review). Returns (width, height) or None.
    """
    if len(image_bytes) < 12:
        return None

    # Try PNG
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return _parse_png_dimensions(image_bytes)

    # Try JPEG
    if image_bytes[:2] == b"\xff\xd8":
        return _parse_jpeg_dimensions(image_bytes)

    return None


def _count_slices(height: int, threshold: int, overlap: int) -> int:
    """Calculate how many slices a tall image needs."""
    if height <= threshold:
        return 1
    effective = height - overlap
    step = threshold - overlap
    if step <= 0:
        return 1
    slices = 1
    remaining = effective
    while remaining > threshold:
        slices += 1
        remaining -= step
    return slices


def _check_paddleocr_available() -> bool:
    """Check if PaddleOCR is importable."""
    try:
        import paddleocr  # noqa: F401
        return True
    except ImportError:
        return False


def _check_tesseract_available() -> bool:
    """Check if pytesseract is importable."""
    try:
        import pytesseract  # noqa: F401
        return True
    except ImportError:
        return False


def _check_ocr_available() -> bool:
    """Check if any OCR engine is available."""
    return _check_paddleocr_available() or _check_tesseract_available()


def _run_paddleocr(image_bytes: bytes) -> tuple[str, float, list[str]]:
    """Run PaddleOCR on image bytes.

    Writes bytes to a temp file (PaddleOCR requires a file path), then runs
    PP-OCRv5 with Chinese language model and textline orientation detection.

    If the image is in WebP format, converts to PNG first since PaddleOCR
    may not support WebP natively.

    Returns:
        (full_text, confidence, warnings)
    """
    import paddleocr

    # Workaround for oneDNN PIR attribute conversion bug on Windows.
    # Must be set before any PaddleOCR model is created.
    if "FLAGS_use_onednn" not in os.environ:
        os.environ["FLAGS_use_onednn"] = "0"

    suffix = _detect_image_suffix(image_bytes)
    warnings: list[str] = []

    # ── Convert WebP to PNG ──
    if suffix == ".webp":
        try:
            from PIL import Image
            from io import BytesIO
            img = Image.open(BytesIO(image_bytes))
            buf = BytesIO()
            img.save(buf, format="PNG")
            image_bytes = buf.getvalue()
            suffix = ".png"
        except Exception as exc:
            warnings.append(f"WebP→PNG conversion failed: {exc}")
            return "", 0.0, warnings

    tmp_path = None

    try:
        # Write to temp file — PaddleOCR needs a path, not bytes
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(image_bytes)
            tmp_path = f.name

        ocr = paddleocr.PaddleOCR(
            use_textline_orientation=True,
            lang="ch",
            ocr_version=_DEFAULT_OCR_VERSION,
        )
        result = ocr.predict(tmp_path)

        all_texts: list[str] = []
        all_scores: list[float] = []

        for page in result:
            texts = page["rec_texts"]
            scores = page["rec_scores"]
            for text, score in zip(texts, scores):
                if score >= _MIN_TEXT_CONFIDENCE and text.strip():
                    all_texts.append(text.strip())
                    all_scores.append(score)

        if not all_texts:
            warnings.append("PaddleOCR found no text above confidence threshold")
            return "", 0.0, warnings

        full_text = "\n".join(all_texts)
        avg_confidence = sum(all_scores) / len(all_scores)

        return full_text, avg_confidence, warnings

    except Exception as exc:
        warnings.append(f"PaddleOCR error: {exc}")
        logger.warning("PaddleOCR failed: %s", exc)
        return "", 0.0, warnings

    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


def _run_tesseract(image_bytes: bytes) -> tuple[str, float, list[str]]:
    """Run Tesseract OCR (pytesseract) on image bytes.

    Requires Tesseract engine installed separately on the system.

    Returns:
        (full_text, confidence, warnings)
    """
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
            warnings.append("Tesseract found no text above confidence threshold")
            return "", 0.0, warnings

        full_text = "\n".join(texts)
        avg_confidence = sum(confs) / len(confs)

        return full_text, avg_confidence, warnings

    except Exception as exc:
        warnings.append(f"Tesseract error: {exc}")
        logger.warning("Tesseract failed: %s", exc)
        return "", 0.0, warnings


def _detect_image_suffix(image_bytes: bytes) -> str:
    """Detect image file suffix from magic bytes."""
    if len(image_bytes) >= 8 and image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if len(image_bytes) >= 2 and image_bytes[:2] == b"\xff\xd8":
        return ".jpg"
    if len(image_bytes) >= 4 and image_bytes[:4] == b"RIFF":
        return ".webp"
    return ".png"  # fallback


def ocr_image(image_bytes: bytes, ocr_enabled: bool = True) -> OcrResult:
    """Process an image through the OCR pipeline.

    This is a deterministic inspection tool:
    - Checks if OCR is enabled (config flag)
    - Inspects image dimensions
    - Slices tall images for segmented processing
    - Runs PaddleOCR (primary) or Tesseract (fallback) for text extraction
    - Returns structured results with extracted text and confidence

    No network I/O, no database access, no LLM calls.
    """
    # --- OCR disabled ---
    if not ocr_enabled:
        return OcrResult(
            full_text="",
            confidence=0.0,
            slice_count=0,
            warnings=["OCR disabled by configuration"],
            needs_manual_review=True,
        )

    # --- Empty input ---
    if not image_bytes:
        return OcrResult(
            full_text="",
            confidence=0.0,
            slice_count=0,
            warnings=["Empty image bytes"],
            needs_manual_review=True,
        )

    warnings: list[str] = []

    # --- Inspect image dimensions ---
    dims = _get_image_dimensions(image_bytes)
    if dims is None:
        warnings.append("Could not parse image dimensions (unsupported format)")
        slice_count = 0
    else:
        width, height = dims
        if width == 0 or height == 0:
            warnings.append("Image has zero dimensions")
            return OcrResult(
                full_text="",
                confidence=0.0,
                slice_count=0,
                warnings=warnings,
                needs_manual_review=True,
            )

        # Slice tall images
        slice_count = _count_slices(height, _SLICE_HEIGHT_THRESHOLD, _SLICE_OVERLAP)
        if slice_count > 1:
            warnings.append(
                f"Image is {height}px tall — would be split into "
                f"{slice_count} overlapping slices for OCR"
            )

    # --- Check OCR engine availability ---
    has_paddleocr = _check_paddleocr_available()
    has_tesseract = _check_tesseract_available()

    if not has_paddleocr and not has_tesseract:
        warnings.append(
            "No OCR engine available (paddleocr / pytesseract not installed)"
        )
        return OcrResult(
            full_text="",
            confidence=0.0,
            slice_count=slice_count,
            warnings=warnings,
            needs_manual_review=True,
        )

    # --- Run OCR ---
    # Prefer PaddleOCR (better Chinese text support), fall back to Tesseract
    if has_paddleocr:
        full_text, confidence, engine_warnings = _run_paddleocr(image_bytes)
        warnings.extend(engine_warnings)
    else:
        full_text, confidence, engine_warnings = _run_tesseract(image_bytes)
        warnings.extend(engine_warnings)

    # --- Determine if manual review is needed ---
    needs_manual_review = False
    if not full_text:
        needs_manual_review = True
        warnings.append("OCR produced no usable text — manual review required")
    elif confidence < 0.6:
        warnings.append(f"Low OCR confidence ({confidence:.2f}) — manual review recommended")

    return OcrResult(
        full_text=full_text,
        confidence=round(confidence, 4),
        slice_count=slice_count,
        warnings=warnings,
        needs_manual_review=needs_manual_review,
    )
