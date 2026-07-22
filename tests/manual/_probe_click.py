"""Focused click-verify: scroll pagination into view, click next, observe
job-link count delta + URL change. Answers URL-pagination (C feasible) vs
SPA-state pagination (B required). 拼多多 + 小米 only (the multi-page ones).
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125 Safari/537.36")

SITES = [
    ("拼多多", "https://careers.pddglobalhr.com/campus/grad?t=N5ch0DXEtA",
     "li.rocket-pagination-next", "下一页"),
    ("小米", "https://xiaomi.jobs.f.mioffice.cn/s/m5DjWDrhl_g",
     "li.atsx-pagination-next, .atsx-pagination-item-link", "next-arrow"),
]

COUNT_JS = r"""() => {
  const a = Array.from(document.querySelectorAll('a, [role=link]'));
  let n = 0;
  for (const x of a) {
    const h = (x.getAttribute('href')||'').toLowerCase();
    const t = (x.innerText||'').trim();
    if (/job|position|recruit|detail/.test(h)) { n++; continue; }
    if (t.length>2 && t.length<60 && /工程师|专员|实习|经理|开发|算法|设计|分析师|架构师|产品|运营|测试/.test(t)) n++;
  }
  return n;
}"""


def click_next(name, url, sel, label):
    print(f"\n{'='*60}\n[{name}] click-test ({label})\n{'='*60}")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(user_agent=UA, viewport={"width": 1366, "height": 900})
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        try: pg.wait_for_load_state("networkidle", timeout=20000)
        except Exception: pass
        pg.wait_for_timeout(1500)
        url0 = pg.url
        n0 = pg.evaluate(COUNT_JS)
        print(f"  page1: url={url0[:80]}")
        print(f"  page1 job-links={n0}")
        loc = pg.locator(sel).first
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
            pg.wait_for_timeout(300)
            vis = loc.is_visible(timeout=1000)
            print(f"  next elem visible after scroll: {vis}")
            loc.click(timeout=3000)
            pg.wait_for_timeout(2500)
            try: pg.wait_for_load_state("networkidle", timeout=8000)
            except Exception: pass
            n1 = pg.evaluate(COUNT_JS)
            url1 = pg.url
            print(f"  page2: job-links={n1} (Δ={n1-n0})  URL={'CHANGED' if url1!=url0 else 'same'}")
            if url1 != url0:
                print(f"  new url: {url1[:90]}")
            # try a 2nd click for page 3
            try:
                loc2 = pg.locator(sel).first
                loc2.scroll_into_view_if_needed(timeout=2000)
                loc2.click(timeout=3000)
                pg.wait_for_timeout(2500)
                n2 = pg.evaluate(COUNT_JS)
                url2 = pg.url
                print(f"  page3: job-links={n2} (Δ from p1={n2-n0})  URL={'CHANGED' if url2!=url0 else 'same'}")
            except Exception as e:
                print(f"  page3 click skipped: {type(e).__name__}")
        except Exception as e:
            print(f"  click failed: {type(e).__name__}: {str(e)[:120]}")
        b.close()


if __name__ == "__main__":
    for name, url, sel, label in SITES:
        try: click_next(name, url, sel, label)
        except Exception as e:
            print(f"  ERROR [{name}]: {type(e).__name__}: {e}")
