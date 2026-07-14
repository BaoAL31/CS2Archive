"""Fix mangled smokes title; put all upload_meta tags on Mirage (10 chips + rest in description)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(r"D:\Projects\CS2Archive")
STORAGE = ROOT / ".bilibili_storage.json"
META = json.loads(
    (ROOT / "youtube" / "2395002_furia-vs-falcons-m1-mirage_karrigan_Mirage_overlay" / "upload_meta.json").read_text(
        encoding="utf-8"
    )
)
ALL_TAGS = META["tags"]
TOP10 = [
    "karrigan",
    "Mirage",
    "FURIA",
    "Falcons",
    "IEM Cologne Major 2026",
    "Grand Final",
    "CS2",
    "CS2 POV",
    "input overlay",
    "utility cam",
]
SHOTS = ROOT / "tmp" / "bili_shots"
SHOTS.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def shot(page, tag: str) -> None:
    path = SHOTS / f"{int(time.time())}_{tag}.png"
    page.screenshot(path=str(path))
    log(f"shot {path.name}")


def open_edit_for(page, title_substr: str) -> bool:
    """Click the Edit control in the card/row containing title_substr."""
    ok = page.evaluate(
        """(s) => {
      const lower = s.toLowerCase();
      // Prefer leaf-ish title nodes
      const candidates = [...document.querySelectorAll('div,span,p,a,h1,h2,h3')]
        .filter(el => {
          const t = (el.innerText || '').trim();
          return t && t.length < 180 && t.toLowerCase().includes(lower);
        })
        .sort((a,b) => a.innerText.length - b.innerText.length);
      for (const hit of candidates.slice(0, 12)) {
        let root = hit;
        for (let i = 0; i < 12 && root; i++) {
          const edit = [...root.querySelectorAll('button,a,span,div')]
            .find(el => /^\\s*Edit\\s*$/i.test((el.innerText || '').trim()));
          if (edit) {
            edit.click();
            return {ok:true, via: (hit.innerText||'').slice(0,80)};
          }
          root = root.parentElement;
        }
      }
      // fallback: nth Edit — 0 = first card
      const edits = [...document.querySelectorAll('button,a,span,div')]
        .filter(el => /^\\s*Edit\\s*$/i.test((el.innerText || '').trim()));
      return {ok:false, editCount: edits.length};
    }""",
        title_substr,
    )
    log(f"open_edit_for({title_substr!r}) -> {ok}")
    page.wait_for_timeout(4500)
    return bool(ok.get("ok")) if isinstance(ok, dict) else bool(ok)


def fill_tags(page, tags: list[str]) -> None:
    n = page.evaluate(
        """() => {
      let n = 0;
      for (let i = 0; i < 30; i++) {
        const closes = [...document.querySelectorAll('.el-tag__close')];
        if (!closes.length) break;
        closes[0].click();
        n++;
      }
      return n;
    }"""
    )
    log(f"cleared {n} tags")
    page.wait_for_timeout(400)
    inp = page.locator(
        'input[placeholder*="Press Enter"], input[placeholder*="tag" i], input[placeholder*="Tag"]'
    )
    for tag in tags:
        inp.first.click()
        inp.first.fill(tag)
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
    counter = page.evaluate(
        """() => {
      const t = document.body.innerText;
      const m = t.match(/Tags[\\s\\S]{0,500}?(\\d{1,2}\\/10)/);
      return m ? m[1] : null;
    }"""
    )
    log(f"tag counter={counter} want={len(tags)}")


def set_schedule_1630(page) -> None:
    if page.get_by_text("Scheduled Release", exact=True).count():
        page.get_by_text("Scheduled Release", exact=True).first.click()
        page.wait_for_timeout(400)
    page.evaluate(
        """() => {
      const setNative = (el, val) => {
        const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
        if (desc && desc.set) desc.set.call(el, val); else el.value = val;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
      };
      for (const el of document.querySelectorAll('input')) {
        const v = el.value || '';
        const t = el.type || '';
        if (t === 'date' || /^\\d{4}-\\d{2}-\\d{2}$/.test(v)) setNative(el, '2026-07-12');
        if (t === 'time' || /^\\d{1,2}:\\d{2}$/.test(v)) setNative(el, '16:30');
      }
    }"""
    )
    log("schedule forced 2026-07-12 16:30")


def save_upload_now(page) -> None:
    btn = page.get_by_role("button", name="Upload Now")
    if not btn.count():
        btn = page.locator("button:has-text('Upload Now')")
    btn.first.click()
    log("clicked Upload Now")
    page.wait_for_timeout(5000)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        ctx = browser.new_context(storage_state=str(STORAGE), viewport={"width": 1440, "height": 960})
        page = ctx.new_page()
        page.set_default_timeout(60000)

        page.goto("https://studio.bilibili.tv/archive-list", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        shot(page, "tags3_list")
        log(page.inner_text("body")[:500].replace("\n", " | "))

        # 1) Restore Ancient Smokes title
        if "smoke lineup" in page.inner_text("body").lower():
            if open_edit_for(page, "smoke lineup"):
                shot(page, "tags3_smokes_edit")
                title = page.locator('input[maxlength="100"]').first.input_value()
                log(f"smokes title={title!r} url={page.url}")
                page.locator('input[maxlength="100"]').first.fill(
                    "Top Ancient Smokes Used by Pros During CAC 2026"
                )
                page.locator("textarea").first.fill(
                    "META Smoke lineups from the CS Asia Championships 2026."
                )
                shot(page, "tags3_smokes_restored")
                save_upload_now(page)
                shot(page, "tags3_smokes_saved")
                page.goto("https://studio.bilibili.tv/archive-list", wait_until="domcontentloaded")
                page.wait_for_timeout(4000)

        # 2) Edit karrigan Mirage — all tags in description, top10 as chips
        if not open_edit_for(page, "karrigan"):
            raise RuntimeError("could not open karrigan edit")
        shot(page, "tags3_mirage_edit")
        title = page.locator('input[maxlength="100"]').first.input_value()
        log(f"mirage title={title!r} url={page.url}")
        if "karrigan" not in title.lower():
            raise RuntimeError(f"wrong video: {title!r}")

        desc = META["description"].rstrip() + "\n\nTags: " + ", ".join(ALL_TAGS)
        page.locator("textarea").first.fill(desc[:2000])
        fill_tags(page, TOP10)
        set_schedule_1630(page)
        page.evaluate(
            """() => {
          const el = [...document.querySelectorAll('*')].find(e => (e.innerText||'').trim() === 'Tags');
          if (el) el.scrollIntoView({block:'center'});
        }"""
        )
        shot(page, "tags3_mirage_ready")
        save_upload_now(page)
        shot(page, "tags3_mirage_saved")
        log(f"final url={page.url}")
        log(page.inner_text("body")[:900].replace("\n", " | "))
        ctx.storage_state(path=str(STORAGE))
        (ROOT / "tmp" / "bili_tags_state.json").write_text(
            json.dumps(
                {
                    "note": "bilibili.tv hard-caps at 10 tag chips; remaining tags appended to description",
                    "top10_chips": TOP10,
                    "all_tags_in_description": ALL_TAGS,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        page.wait_for_timeout(4000)
        browser.close()
    log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
