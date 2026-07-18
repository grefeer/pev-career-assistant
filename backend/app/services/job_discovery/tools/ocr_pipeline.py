from __future__ import annotations

import struct

from backend.app.services.job_discovery.schemas import OcrResult

# Height threshold for slicing tall images (pixels).
_SLICE_HEIGHT_THRESHOLD = 2000
# Overlap between consecutive slices (pixels).
_SLICE_OVERLAP = 100


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


def _check_ocr_available() -> bool:
    """Check if an OCR engine is available (paddleocr or tesseract)."""
    try:
        import paddleocr  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import pytesseract  # noqa: F401
        return True
    except ImportError:
        pass
    return False


def ocr_image(image_bytes: bytes, ocr_enabled: bool = True) -> OcrResult:
    """Process an image through the OCR pipeline.

    This is a deterministic inspection tool:
    - Checks if OCR is enabled (config flag)
    - Inspects image dimensions
    - Slices tall images for segmented processing
    - Checks OCR engine availability
    - Returns structured results (placeholder — actual OCR not performed)

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
    ocr_available = _check_ocr_available()
    if not ocr_available:
        warnings.append(
            "No OCR engine available (paddleocr / pytesseract not installed) — "
            "returning placeholder result"
        )
        return OcrResult(
            full_text="",
            confidence=0.0,
            slice_count=slice_count,
            warnings=warnings,
            needs_manual_review=True,
        )

    # Placeholder: actual OCR would run here
    # When OCR is available but we haven't integrated it yet, indicate manual review
    warnings.append("OCR engine detected but text extraction is not yet integrated")
    return OcrResult(
        full_text="",
        confidence=0.0,
        slice_count=slice_count,
        warnings=warnings,
        needs_manual_review=True,
    )
