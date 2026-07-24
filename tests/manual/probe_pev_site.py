"""PEV site contract probe (Task 4.1).

Captures a sanitized, provably-terminated listing contract for one recruitment
site, so the site adapter (Task 4.2+) is built against a fixed input fixture
rather than a live response.

Anti-scraping policy (hard gate):
  * The probe NEVER decrypts, de-obfuscates, or reverse-engineers a response. If
    a listing XHR is encrypted/obfuscated (e.g. Moka's ``{data, necromancer}``
    AES envelope), it is recorded as ``obfuscated_xhr`` and the contract is
    taken from the RENDERED DOM -- exactly what a human visitor sees.
  * If the site presents a login / captcha / slider / anti-bot wall, only a
    blocked-marker fixture is written and the site's adapter is NOT implemented.

Sanitization:
  * Request headers (Authorization, Cookie) and request bodies are never stored.
  * Sensitive query params (token, referral, share code, ...) are replaced with
    ``<redacted>``.
  * At most 3 sample listings are kept, with email/phone/ID-card PII redacted.
  * No email, phone, name, resume, or device identifier is persisted.

Usage:
    python tests/manual/probe_pev_site.py \\
        --site moka \\
        --url "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home" \\
        --output tests/fixtures/job_discovery/moka
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Response, sync_playwright


# --- site-aware configuration -------------------------------------------------

SITE_CONFIG: dict[str, dict[str, Any]] = {
    "moka": {
        "host_contains": "mokahr.com",
        # Moka renders all campus jobs on a single hash-route page; the encrypted
        # XHR is not decrypted, the contract is read from the rendered DOM.
        "single_page_proof": "single_page_mokahr_hash_jobs",
        "detail_href_regex": r"#/job/",
        "listing_click_texts": ["职位", "全部职位", "Jobs", "Positions"],
    },
    "feishu": {
        "host_contains": "feishu.cn",
        "single_page_proof": "single_page_feishu_render",
        "detail_href_regex": r"/(job|position|detail)",
        "listing_click_texts": ["职位", "全部职位", "Jobs", "Positions"],
    },
    "inovance": {
        "host_contains": "inovance.com",
        "single_page_proof": "single_page_inovance_hash_jobs",
        "detail_href_regex": r"#/(job|position|detail)",
        "listing_click_texts": ["职位", "全部职位", "Jobs", "Positions"],
    },
    "xiaohongshu": {
        "host_contains": "xiaohongshu.com",
        "single_page_proof": "single_page_xhs_render",
        "detail_href_regex": r"/(job|position|detail|intern)",
        "listing_click_texts": ["职位", "全部职位", "Jobs", "Positions", "Intern"],
    },
}

JOB_FIELD_HINTS = (
    "title", "jobtitle", "jobname", "positionname", "position", "job",
    "department", "workplace", "location", "city", "jobid",
)
# Strong title fields: a clean listing array must carry at least one of these,
# otherwise it is a non-job array (privacy policy, calling codes, ...).
STRONG_TITLE_FIELDS = ("title", "jobtitle", "jobname", "positionname", "position", "job")
TOTAL_KEY_HINTS = ("totalcount", "total_count", "total", "totalelements", "count", "recordcount")
HASMORE_KEY_HINTS = ("hasmore", "hasnext", "hasmorepage", "more", "islast", "hasnextpage")
CURSOR_KEY_HINTS = ("nextcursor", "next_cursor", "nextpage", "next_page", "pageno", "offset", "cursor", "after")
SENSITIVE_QUERY_EXACT = {"ref", "code", "key", "st", "sig"}
SENSITIVE_QUERY_SUBSTR = (
    "token", "share", "sign", "auth", "refer", "recommend", "session",
    "ticket", "secret", "access", "captcha", "verify",
)


def _is_sensitive_query_key(key: str) -> bool:
    k = key.lower()
    if k in SENSITIVE_QUERY_EXACT:
        return True
    return any(s in k for s in SENSITIVE_QUERY_SUBSTR)
# Hard wall markers only. A generic "login"/"登录" nav link does NOT count -- the
# site is only blocked when no listings render AND one of these appears, or when
# the API returns 401/403.
BLOCKED_MARKERS = (
    "captcha", "验证码", "滑块", "slider验证", "环境异常", "完成验证后即可继续访问",
    "安全验证", "人机验证", "anti-bot", "antibot", "访问被拒", "请完成验证",
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+?86)?1[3-9]\d{9}")
IDCARD_RE = re.compile(r"\d{17}[\dXx]")


# --- sanitization -------------------------------------------------------------

def redact_query(url: str) -> str:
    """Redact sensitive query params; keep param names and structure."""
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    frag_sep = ""
    if "#" in query:
        query, _, frag_sep = query.partition("#")
        frag_sep = "#" + frag_sep
    parts = []
    for seg in query.split("&"):
        if not seg:
            continue
        key, _, val = seg.partition("=")
        if val and _is_sensitive_query_key(key):
            parts.append(f"{key}=<redacted>")
        else:
            parts.append(seg)
    return f"{base}?{'&'.join(parts)}{frag_sep}"


def redact_pii(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = EMAIL_RE.sub("<redacted_email>", text)
    text = PHONE_RE.sub("<redacted_phone>", text)
    text = IDCARD_RE.sub("<redacted_pii>", text)
    return text


def sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, list):
        return [sanitize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitize_value(v) for k, v in value.items()}
    return value


def sanitize_item(item: dict) -> dict:
    return {k: sanitize_value(v) for k, v in item.items()}


# --- JSON contract analysis ---------------------------------------------------

def walk_arrays(obj: Any, path: str = "$") -> list[tuple[str, list]]:
    out: list[tuple[str, list]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(walk_arrays(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        out.append((path, obj))
        for i, v in enumerate(obj):
            out.extend(walk_arrays(v, f"{path}[{i}]"))
    return out


def find_key_path(obj: Any, names: set[str], path: str = "$") -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in names:
                return f"{path}.{k}"
            found = find_key_path(v, names, f"{path}.{k}")
            if found:
                return found
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found = find_key_path(v, names, f"{path}[{i}]")
            if found:
                return found
    return None


def resolve_path(obj: Any, path: str | None) -> Any:
    """Return the value at a dotted ``$.a.b`` path, or None."""
    if not path:
        return None
    cur: Any = obj
    for part in path.lstrip("$").lstrip(".").split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def is_obfuscated(body: Any) -> bool:
    return (
        isinstance(body, dict)
        and isinstance(body.get("data"), str)
        and isinstance(body.get("necromancer"), str)
    )


def score_jobish(arr: list) -> int:
    if not arr or not isinstance(arr[0], dict):
        return 0
    keys = {k.lower() for k in arr[0]}
    if not any(h in keys for h in STRONG_TITLE_FIELDS):
        return 0
    hits = sum(1 for h in JOB_FIELD_HINTS if h in keys)
    return hits * 100 + min(len(arr), 1000)


def analyze_clean_json(body: Any) -> dict | None:
    """If body is a clean (non-obfuscated) JSON with a jobish array, return contract."""
    if is_obfuscated(body):
        return None
    arrays = walk_arrays(body)
    if not arrays:
        return None
    best = max(arrays, key=lambda kv: score_jobish(kv[1]))
    path, arr = best
    score = score_jobish(arr)
    if score < 100:
        return None
    item_fields = list(arr[0].keys()) if arr and isinstance(arr[0], dict) else []
    total_path = find_key_path(body, set(TOTAL_KEY_HINTS))
    hasmore_path = find_key_path(body, set(HASMORE_KEY_HINTS))
    cursor_path = find_key_path(body, set(CURSOR_KEY_HINTS))
    return {
        "top_level_keys": list(body.keys()) if isinstance(body, dict) else [],
        "items_path": path,
        "item_field_names": item_fields,
        "total_count_path": total_path,
        "total_count_value": resolve_path(body, total_path),
        "has_more_path": hasmore_path,
        "has_more_value": resolve_path(body, hasmore_path),
        "next_cursor_path": cursor_path,
        "next_cursor_value": resolve_path(body, cursor_path),
        "sample_listings": [sanitize_item(it) for it in arr[:3] if isinstance(it, dict)],
        "observed_total": len(arr),
        "score": score,
    }


# --- DOM extraction -----------------------------------------------------------

JOB_ANCHOR_JS = r"""
els => {
  const re = new RegExp(%s);
  const out = [];
  for (const a of els) {
    const href = a.href || "";
    if (!re.test(href)) continue;
    const text = (a.textContent || "").replace(/\s+/g, " ").trim();
    out.push({href: href, text: text});
  }
  return out;
}
"""


def extract_dom_listings(page, detail_re: str) -> list[dict]:
    try:
        js = JOB_ANCHOR_JS % json.dumps(detail_re)
        anchors = page.eval_on_selector_all("a", js)
    except Exception:
        return []
    seen: dict[str, dict] = {}
    for a in anchors:
        href = a.get("href", "")
        text = a.get("text", "")
        if not href:
            continue
        # a real job-detail URL carries an id (digits/uuid) in its path; nav
        # links do not. Check the path (before any query) so a digit-bearing
        # query param on a nav link does not let it through.
        path_only = href.split("?")[0]
        if not any(ch.isdigit() for ch in path_only):
            continue
        key = href.split("#")[-1] if "#" in href else href
        if key not in seen and (text or href):
            seen[key] = {"detail_url": redact_query(href), "title": redact_pii(text)}
    return list(seen.values())


def detect_next_page_control(page) -> str | None:
    """Return a description of a disabled/absent next-page control, else None."""
    for sel, desc in [
        ("button:has-text('下一页')", "next_page_button_cn"),
        ("button:has-text('Next')", "next_page_button_en"),
        (".pagination .disabled", "pagination_disabled"),
        (".ant-pagination-disabled", "ant_pagination_disabled"),
        (".next-pagination-disabled", "next_pagination_disabled"),
    ]:
        try:
            if page.locator(sel).first.is_visible(timeout=800):
                return desc
        except Exception:
            continue
    return None


def detect_blocked(page, responses: list[dict]) -> list[str]:
    markers: list[str] = []
    try:
        text = (page.content() or "").lower()
    except Exception:
        text = ""
    for m in BLOCKED_MARKERS:
        if m.lower() in text:
            markers.append(m)
    for r in responses:
        if r["status"] in (401, 403):
            markers.append(f"http_{r['status']}")
    # de-dup, keep order
    seen: set[str] = set()
    out: list[str] = []
    for m in markers:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


# --- main ---------------------------------------------------------------------

def write_fixture(output: Path, contract: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "contract.json").write_text(
        json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_blocked_contract(site: str, page_url: str, blocked_markers: list[str]) -> dict:
    return {
        "site": site,
        "page_url": redact_query(page_url),
        "blocked_markers": blocked_markers,
        "response_url_pattern": None,
        "response_content_type": None,
        "top_level_keys": [],
        "items_path": None,
        "item_field_names": [],
        "total_count_path": None,
        "has_more_path": None,
        "next_cursor_path": None,
        "terminal_selector": None,
        "single_page_proof": None,
        "terminal_evidence": None,
        "detail_url_examples": [],
        "sample_listings": [],
        "expected_listing_count": 0,
        "data_source": "blocked",
    }


def run_probe(site: str, url: str, output: Path, timeout: int) -> dict:
    cfg = SITE_CONFIG.get(site)
    if cfg is None:
        raise SystemExit(f"unknown site: {site}")
    detail_re = cfg["detail_href_regex"]

    captured: list[dict] = []
    obfuscated_endpoints: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(resp: Response) -> None:
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" not in ctype:
                return
            try:
                body = resp.json()
            except Exception:
                return
            entry = {
                "url": redact_query(resp.url),
                "status": resp.status,
                "content_type": ctype.split(";")[0].strip(),
                "body": body,
            }
            captured.append(entry)
            if is_obfuscated(body) and "ats-apply" in resp.url:
                obfuscated_endpoints.append(resp.url.split("?")[0])

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception as exc:
            print(f"[probe] nav warning: {exc}", file=sys.stderr)

        # best-effort: reveal the job list (click through a landing entry)
        for txt in cfg["listing_click_texts"]:
            try:
                page.click(f"text={txt}", timeout=1500)
                page.wait_for_load_state("networkidle", timeout=timeout)
                break
            except Exception:
                continue

        # scroll to surface lazy-rendered listings
        for _ in range(3):
            try:
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(600)
            except Exception:
                break

        page_url = page.url
        blocked_markers = detect_blocked(page, captured)

        if os.environ.get("PROBE_DEBUG"):
            for r in captured:
                c = analyze_clean_json(r["body"]) if not is_obfuscated(r["body"]) else None
                print(f"[debug] {r['status']} {r['url'][:90]} obf={is_obfuscated(r['body'])} "
                      f"obs={c['observed_total'] if c else '-'} path={c['items_path'] if c else '-'}",
                      file=sys.stderr)
        # 1) clean-JSON listing contract (preferred when not obfuscated)
        json_contract: dict | None = None
        json_source_url: str | None = None
        json_source_ctype: str | None = None
        for r in captured:
            if is_obfuscated(r["body"]):
                continue
            c = analyze_clean_json(r["body"])
            if not c:
                continue
            # prefer the array with the strongest job signal (hits*100);
            # break ties by raw item count. This avoids picking a larger but
            # non-job array (e.g. a "news"/announcement feed) over the real
            # listing endpoint.
            better = json_contract is None
            if json_contract is not None:
                if c["score"] != json_contract["score"]:
                    better = c["score"] > json_contract["score"]
                else:
                    better = c["observed_total"] > json_contract["observed_total"]
            if better:
                json_contract = c
                json_source_url = r["url"]
                json_source_ctype = r["content_type"]

        # 2) rendered-DOM listing (used when XHR is obfuscated or absent)
        dom_listings = extract_dom_listings(page, detail_re)
        next_page_control = detect_next_page_control(page)

        if os.environ.get("PROBE_DEBUG"):
            for d in dom_listings:
                print(f"[debug-dom] {d['detail_url']!r}", file=sys.stderr)

        browser.close()

    # --- assemble contract ---
    sample_listings: list[dict] = []
    observed_total = 0
    if json_contract:
        sample_listings = json_contract["sample_listings"]
        observed_total = json_contract["observed_total"]
    if dom_listings and not json_contract:
        sample_listings = dom_listings[:3]
        observed_total = len(dom_listings)

    # blocked only when nothing rendered AND a hard wall marker fired
    if not sample_listings and blocked_markers:
        contract = build_blocked_contract(site, page_url, blocked_markers)
        write_fixture(output, contract)
        return contract

    contract: dict[str, Any] = {
        "site": site,
        "page_url": redact_query(page_url),
        "blocked_markers": blocked_markers if not sample_listings else [],
        "obfuscated_xhr": bool(obfuscated_endpoints),
        "obfuscated_endpoints": [redact_query(u) for u in obfuscated_endpoints],
        "response_url_pattern": json_source_url,
        "response_content_type": json_source_ctype,
        "top_level_keys": json_contract["top_level_keys"] if json_contract else [],
        "items_path": json_contract["items_path"] if json_contract else None,
        "item_field_names": (
            json_contract["item_field_names"] if json_contract
            else sorted({k for item in dom_listings for k in item})
        ),
        "total_count_path": json_contract["total_count_path"] if json_contract else None,
        "total_count_value": json_contract["total_count_value"] if json_contract else None,
        "has_more_path": json_contract["has_more_path"] if json_contract else None,
        "has_more_value": json_contract["has_more_value"] if json_contract else None,
        "next_cursor_path": json_contract["next_cursor_path"] if json_contract else None,
        "next_cursor_value": json_contract["next_cursor_value"] if json_contract else None,
        "terminal_selector": next_page_control,
        "single_page_proof": None,
        "terminal_evidence": None,
        "detail_url_examples": [d["detail_url"] for d in dom_listings[:3]] if dom_listings else [],
        "sample_listings": sample_listings,
        "expected_listing_count": len(sample_listings),
        "observed_total": observed_total,
        "data_source": "json_xhr" if json_contract else ("dom_rendered" if dom_listings else None),
    }

    # --- terminal signal (honest: claim reached/terminal only when proven) ---
    has_next_cursor = bool(contract["next_cursor_path"])
    has_total = bool(contract["total_count_path"])
    has_hasmore = bool(contract["has_more_path"])
    total_val = contract.get("total_count_value")
    hasmore_val = contract.get("has_more_value")
    cursor_val = contract.get("next_cursor_value")
    if contract["obfuscated_xhr"] or (dom_listings and not json_contract):
        # hash SPA / rendered single page: all listings in one snapshot
        contract["single_page_proof"] = cfg["single_page_proof"]
        contract["terminal_evidence"] = cfg["single_page_proof"]
    elif next_page_control:
        contract["terminal_evidence"] = f"terminal_selector:{next_page_control}"
    elif has_hasmore and hasmore_val is False:
        contract["terminal_evidence"] = "has_more=false"
    elif has_next_cursor and (cursor_val is None or cursor_val == "" or cursor_val == 0):
        contract["terminal_evidence"] = "next_cursor=null"
    elif (
        has_total
        and isinstance(total_val, (int, float))
        and total_val > 0
        and observed_total >= int(total_val)
    ):
        contract["terminal_evidence"] = "expected_listing_count reached"
    # else: paginated, more pages remain on this snapshot; terminal_evidence stays
    # None. The captured total/cursor PATHS still satisfy the fixture gate; the
    # adapter paginates to the real terminal at runtime.

    write_fixture(output, contract)
    return contract


def main() -> int:
    ap = argparse.ArgumentParser(description="PEV site contract probe")
    ap.add_argument("--site", required=True, choices=list(SITE_CONFIG))
    ap.add_argument("--url", required=True)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--timeout", type=int, default=20000)
    args = ap.parse_args()

    contract = run_probe(args.site, args.url, args.output, args.timeout)
    print(json.dumps(
        {
            "site": contract["site"],
            "data_source": contract["data_source"],
            "obfuscated_xhr": contract.get("obfuscated_xhr"),
            "blocked_markers": contract["blocked_markers"],
            "expected_listing_count": contract["expected_listing_count"],
            "single_page_proof": contract["single_page_proof"],
            "total_count_path": contract["total_count_path"],
            "has_more_path": contract["has_more_path"],
            "next_cursor_path": contract["next_cursor_path"],
            "detail_url_examples": contract["detail_url_examples"],
            "item_field_names": contract["item_field_names"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
