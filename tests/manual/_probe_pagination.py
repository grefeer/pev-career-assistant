"""Probe pagination mechanism of the 4 career sites. No LLM, no extraction.

For each URL determines: URL-based pagination (?page=N) vs "load more" button
click vs infinite scroll vs single page. Records job-link count before/after
scroll and after clicking a pagination element, plus any URL change.

Security: only dismisses privacy/consent popups (reading JD context). Never
bypasses login/captcha (just notes their presence). Never submits anything.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright.sync_api import sync_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125 Safari/537.36"
)

URLS = [
    ("Moka/元戎", "https://app.mokahr.com/recommendation-apply/deeproute/6488?sharePageId=4200484&recommendCode=NTAIUtn&codeType=1&code=061yTN0w36fOd53Sqd0w3ct2UU0yTN0e&state=3#/jobs/?isCampusJob=1"),
    ("禾赛", "https://kwh0jtf778.jobs.feishu.cn/229043/m/?external_referral_code=GA2DJVE"),
    ("小米", "https://xiaomi.jobs.f.mioffice.cn/s/m5DjWDrhl_g"),
    ("拼多多", "https://careers.pddglobalhr.com/campus/grad?t=N5ch0DXEtA"),
]

CONSENT_TEXTS = ["同意", "接受", "我知道了", "accept", "agree", "确定", "继续访问", "同意并继续", "知道了"]
LOGIN_WALL = ["请先登录", "扫码登录", "sign in", "log in", "请登录", "登录后"]
VERIFY_WALL = ["环境异常", "完成验证后即可继续访问", "验证码", "captcha"]


def dismiss_consent(page) -> None:
    for txt in CONSENT_TEXTS:
        try:
            btn = page.get_by_role("button", name=txt).first
            if btn.is_visible(timeout=400):
                btn.click(timeout=1000)
                page.wait_for_timeout(400)
                return
        except Exception:
            pass
    # fallback: any clickable element with consent text
    try:
        for txt in CONSENT_TEXTS:
            el = page.locator(f"text={txt}").first
            if el.is_visible(timeout=400):
                el.click(timeout=1000)
                page.wait_for_timeout(400)
                return
    except Exception:
        pass


def count_job_links(page) -> int:
    return page.evaluate(
        """() => {
        const links = Array.from(document.querySelectorAll('a, [role=link], [role=button]'));
        let n = 0;
        for (const a of links) {
            const href = (a.getAttribute('href') || '').toLowerCase();
            const txt = (a.innerText || '').trim();
            if (/job|position|recruit|detail/.test(href)) { n++; continue; }
            if (txt.length > 2 && txt.length < 60 && /工程师|专员|实习|经理|开发|算法|设计|分析师|架构师|产品|运营|测试/.test(txt)) { n++; }
        }
        return n;
    }"""
    )


def find_pagination(page):
    return page.evaluate(
        """() => {
        const out = [];
        const push = (el, why) => {
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return;
            out.push({why, tag: el.tagName, txt: (el.innerText||'').trim().slice(0,30),
                      cls: (el.className||'').toString().slice(0,100),
                      href: (el.getAttribute('href')||'').slice(0,80)});
        };
        const nextTexts = ['下一页','下页','加载更多','更多','next','load more','more','»','›','>'];
        for (const t of nextTexts) {
            const cand = Array.from(document.querySelectorAll('button, a, [role=button], li, span, div'));
            for (const el of cand) {
                const inner = (el.innerText||'').trim();
                if (inner.length < 12 && (inner === t || inner.includes(t))) { push(el, 'text:'+t); break; }
            }
        }
        const sels = ['.next','a[rel=next]','[class*=pagination]','[class*=pager]','[class*=page-next]',
                     '[class*=next-btn]','[class*=load-more]','[class*=loadmore]','[class*=more-btn]',
                     '[class*=ant-pagination]','[class*=el-pagination]','li[class*=next]','button[class*=next]'];
        for (const s of sels) {
            document.querySelectorAll(s).forEach(el => push(el, 'sel:'+s));
        }
        // page-number style links (1 2 3 ...)
        const numLinks = Array.from(document.querySelectorAll('a, li, button'))
            .filter(el => /^\d{1,3}$/.test((el.innerText||'').trim()) && el.getBoundingClientRect().width>0);
        numLinks.slice(0,6).forEach(el => push(el, 'pagenum'));
        // dedupe by cls+txt
        const seen = new Set(); const res = [];
        for (const r of out) { const k = r.cls+'|'+r.txt+'|'+r.why; if(!seen.has(k)){seen.add(k); res.push(r);} }
        return res.slice(0, 12);
    }"""
    )


def wall_check(page) -> list[str]:
    body = page.evaluate("() => (document.body.innerText||'').slice(0,400)")
    hits = []
    for w in LOGIN_WALL:
        if w in body.lower():
            hits.append("login:" + w)
    for w in VERIFY_WALL:
        if w in body:
            hits.append("verify:" + w)
    return hits


def probe(name: str, url: str) -> None:
    print(f"\n{'='*72}\n[{name}] {url}\n{'='*72}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print("  goto error:", type(e).__name__, str(e)[:120]); browser.close(); return
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        dismiss_consent(page)
        page.wait_for_timeout(1500)
        dismiss_consent(page)
        page.wait_for_timeout(800)

        cur = page.url
        print(f"  final URL : {cur}")
        walls = wall_check(page)
        if walls:
            print(f"  ⚠ walls   : {walls}")
        n0 = count_job_links(page)
        print(f"  job-links initial      : {n0}")

        # infinite scroll test
        for _ in range(4):
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
        ns = count_job_links(page)
        url_s = page.url
        print(f"  job-links after 4x scroll: {ns}  (Δ={ns - n0})")
        print(f"  URL after scroll       : {'CHANGED' if url_s != cur else 'same'}")

        pg = find_pagination(page)
        print(f"  pagination elems found : {len(pg)}")
        for r in pg[:8]:
            print(f"    - {r['why']:<14} tag={r['tag']:<6} txt={r['txt']!r:<14} cls={r['cls']!r}")

        # try clicking the first "next"-like element
        clicked = None
        for r in pg:
            why = r["why"]
            if why.startswith("text:") and any(
                k in r["txt"] for k in ["下一页", "下页", "加载更多", "next", "load more", "»", "›", "更多"]
            ) or why.startswith("sel:") and any(
                k in why for k in ["next", "load-more", "loadmore", "more-btn", "page-next"]
            ):
                clicked = r
                break
        if clicked:
            print(f"  → trying click: {clicked['why']} txt={clicked['txt']!r}")
            try:
                loc = page.locator(
                    f"button:has-text('{clicked['txt']}'), a:has-text('{clicked['txt']}'), "
                    f"[role=button]:has-text('{clicked['txt']}')"
                ).first
                if not loc.is_visible(timeout=1000):
                    raise RuntimeError("not visible")
                loc.click(timeout=3000)
                page.wait_for_timeout(2500)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                nc = count_job_links(page)
                url_c = page.url
                print(f"    after click: job-links={nc} (Δ={nc - n0})  URL={'CHANGED' if url_c != cur else 'same'}")
                if url_c != cur:
                    print(f"    new URL: {url_c}")
            except Exception as e:
                print(f"    click failed: {type(e).__name__}: {str(e)[:100]}")
        else:
            print("  → no next-like element to click-test")

        browser.close()


if __name__ == "__main__":
    for name, url in URLS:
        try:
            probe(name, url)
        except Exception as e:
            print(f"  PROBE ERROR [{name}]: {type(e).__name__}: {e}")
