# Round 5 OCR probe report

## Conclusion

Keep the live WeChat OCR feature flag off in this round. Implement and retain
the content-addressed cache, but do not turn a borderline five-image result
into a production-wide OCR dependency: the measured 112.7 seconds leaves only
7.3 seconds under the 120-second article ceiling, and larger articles or a
second cold model initialization will exceed it.

## Environment

- Python: `D:\Program Files\JetBrains\PyCharm Community Edition 2024.2.2\proj\langgraph-multi-agent-career-assistant-main\.venv\Scripts\python.exe`
- PaddleOCR: installed
- PaddlePaddle: installed
- PIL: installed
- OCR engine: PaddleOCR PP-OCRv5, CPU, oneDNN disabled by the script's existing Windows workaround

## Measurements

Representative asset: `temp/wechat_repro_out/ocr/wechat_img_00.png` (1080x2169,
two slices).

| Probe | Wall time | Result |
|---|---:|---|
| Single-image cold/no-cache | 56.7 s | 36 chars, confidence 0.9163 |
| Same image cache hit in a new CLI process | 106 ms | exact result returned |
| Five-image article, serial, no-cache | 112.7 s | all 5 returned `status=ok` |

Five-image per-image timings were 42.5 s, 41.1 s, 10.2 s, 10.5 s, and 8.1 s.
The first two images are long enough to trigger the expensive path. The probe
cache contained five distinct image entries; text lengths were 36, 243, 10,
50, and 19 characters, with confidence 0.9163–0.9995.

## Decision and safeguards

1. `job_discovery_ocr_enabled` remains false; no production feature toggle was changed.
2. OCR cache entries are keyed by the full image SHA-256 plus backend, so a
   result from one engine cannot masquerade as another engine's result.
3. Cache writes are atomic; a concurrent reader cannot observe partial JSON.
4. Empty/failed OCR remains a manual-review/blocked result and is not converted
   to an empty successful evidence artifact.
5. A future enablement probe must measure a representative 8–10 image article,
   include process startup and model warm-up, and pass with margin rather than
   merely touching the 120-second ceiling.

## Output paths

- Cold probe cache/timing artifacts: `temp/round5_ocr_probe_cold/`
- Five-image probe artifacts: `temp/round5_ocr_probe_article/`
- This durable decision: `plan/optim-2026-08-09/ocr_probe_report.md`
