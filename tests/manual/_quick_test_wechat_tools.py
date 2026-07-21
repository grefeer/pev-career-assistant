"""Quick test: Verify WeChat tool chain works before agent test."""
import json
import os
import sys

os.environ["FLAGS_use_onednn"] = "0"
os.environ["PYTHONIOENCODING"] = "utf-8"

# Force UTF-8 encoding for stdout/stderr
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import dotenv_values
vals = dotenv_values("D:/Python/langgraph-multi-agent-career-assistant-main/.env", interpolate=False)
if "READGZH_API_KEY" in vals:
    os.environ["READGZH_API_KEY"] = vals["READGZH_API_KEY"]

from backend.app.services.job_discovery.deepagents_runner import (
    _fetch_wechat_via_readgzh, parse_wechat_article, ocr_images_from_urls,
    extract_jd_candidates
)

# Test 1: Fetch WeChat article via ReadGZH
url = "https://mp.weixin.qq.com/s/6tGCObeYzmSwYDn3d3L3Hg"
print("Test 1: ReadGZH fetch...", flush=True)
text, title, error = _fetch_wechat_via_readgzh(url)
if error:
    print(f"  FAIL: {error[:200]}", flush=True)
else:
    print(f"  OK: title={title[:100] if title else 'None'}", flush=True)
    print(f"  text_len={len(text) if text else 0}", flush=True)

# Test 2: Parse WeChat article
if text:
    print("\nTest 2: Parse article...", flush=True)
    result = parse_wechat_article(text, url)
    print(f"  title: {str(result.get('title'))[:100]}", flush=True)
    article_text = result.get("text_content", "") or ""
    print(f"  text_len: {len(article_text)}", flush=True)
    images = result.get("image_urls", []) or []
    print(f"  image_count: {len(images)}", flush=True)
    print(f"  email_delivery: {str(result.get('email_delivery_instructions'))[:200]}", flush=True)
    if article_text:
        print(f"  text_preview: {article_text[:200]}...", flush=True)

    # Test 3: OCR images if any
    if images:
        print(f"\nTest 3: OCR {len(images)} images...", flush=True)
        ocr_json = ocr_images_from_urls(json.dumps(images[:5]))
        ocr_data = json.loads(ocr_json)
        combined_ocr = ""
        for i, r in enumerate(ocr_data):
            ocr_text = r.get("ocr_text", "") or ""
            print(f"  [{i}] ocr_len={len(ocr_text)} conf={r.get('confidence')} err={str(r.get('error'))[:100]}", flush=True)
            if ocr_text:
                combined_ocr += ocr_text + "\n"
    else:
        combined_ocr = ""

    # Test 4: Extract JD candidates from combined text
    combined = (article_text or "") + "\n" + combined_ocr
    print(f"\nTest 4: Extract JD from combined text ({len(combined)} chars)...", flush=True)
    candidates_json = extract_jd_candidates(combined, url)
    candidates = json.loads(candidates_json)
    print(f"  Candidates found: {len(candidates)}", flush=True)
    for i, c in enumerate(candidates):
        print(f"  [{i}] title={c.get('title')}", flush=True)
        print(f"       company={c.get('company_name')}", flush=True)
        print(f"       locations={c.get('locations')}", flush=True)
        print(f"       recruitment_types={c.get('recruitment_types')}", flush=True)
        print(f"       confidence={c.get('confidence')}", flush=True)
        print(f"       warnings={c.get('normalization_warnings')}", flush=True)
        desc = c.get("description_text", "") or ""
        print(f"       desc_len={len(desc)}", flush=True)
        if desc:
            print(f"       desc_preview: {desc[:200]}...", flush=True)
else:
    print("Skipping further tests - no text from ReadGZH", flush=True)

print("\nDone.", flush=True)
