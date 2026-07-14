"""Upload Mirage bili copy — cover modal needs Crop before Confirm."""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

ROOT = Path(r"D:\Projects\CS2Archive")
STORAGE = ROOT / ".bilibili_storage.json"
YT = ROOT / "youtube" / "2395002_furia-vs-falcons-m1-mirage_karrigan_Mirage_overlay"
VIDEO = YT / "video_bili.mp4"
THUMB = YT / "thumbnail.jpg"
META = json.loads((YT / "upload_meta.json").read_text(encoding="utf-8"))
SHOTS = ROOT / "tmp" / "bili_shots"
LOG = ROOT / "tmp" / "bili_upload_mirage.log"
STATE = ROOT / "tmp" / "bili_upload_mirage_state.json"
PUBLISH = datetime(2026, 7, 12, 16, 30, tzinfo=ZoneInfo("Australia/Sydney"))

SHOTS.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def shot(page, tag: str) -> Path:
    path = SHOTS / f"{int(time.time())}_{tag}.png"
    page.screenshot(path=str(path))
    log(f"shot {path.name}")
    return path


def js_click_text(page, pattern: str) -> dict:
    return page.evaluate(
        """(pat) => {
      const re = new RegExp(pat, 'i');
      const buttons = [...document.querySelectorAll('button, [role=button], a')];
      const btn = buttons.find(b => re.test((b.innerText || '').trim()));
      if (!btn) return {ok:false, found: buttons.slice(0,25).map(b => (b.innerText||'').trim()).filter(Boolean)};
      btn.disabled = false;
      btn.removeAttribute('disabled');
      btn.click();
      return {ok:true, text:(btn.innerText||'').trim(), cls:btn.className, disabled: !!btn.disabled};
    }""",
        pattern,
    )


def dismiss_cover_editor(page) -> None:
    """Cover editor: click Crop (div), wait until Confirm enables, then Confirm. Never Cancel (resets upload)."""
    shot(page, "cover_before")
    if not page.get_by_text("Cover editor", exact=False).count():
        log("no cover editor open")
        return

    crop_clicked = page.evaluate(
        """() => {
      const nodes = [...document.querySelectorAll('div,span,button,a,p')];
      const el = nodes.find(n => {
        const t = (n.innerText || '').replace(/\\s+/g,' ').trim();
        return t === 'Crop' || t === '✓ Crop' || /^✓\\s*Crop$/.test(t);
      });
      if (!el) return {ok:false};
      el.click();
      return {ok:true, text:(el.innerText||'').trim(), cls: el.className};
    }"""
    )
    log(f"crop click: {crop_clicked}")
    page.wait_for_timeout(2500)
    shot(page, "after_crop")

    # Wait until Confirm is enabled (up to ~10s)
    enabled = False
    for _ in range(20):
        st = page.evaluate(
            """() => {
          const btn = [...document.querySelectorAll('button')].find(b => /^\\s*Confirm\\s*$/i.test((b.innerText||'').trim()));
          if (!btn) return {found:false};
          return {found:true, disabled: !!(btn.disabled || btn.getAttribute('disabled') !== null || btn.classList.contains('is-disabled') || getComputedStyle(btn).opacity < 0.9)};
        }"""
        )
        log(f"confirm state: {st}")
        if st.get("found") and not st.get("disabled"):
            enabled = True
            break
        page.wait_for_timeout(500)

    conf = page.evaluate(
        """() => {
      const btn = [...document.querySelectorAll('button')].find(b => /^\\s*Confirm\\s*$/i.test((b.innerText||'').trim()))
        || document.querySelector('.cover-local__btn.primary-btn');
      if (!btn) return {ok:false};
      btn.click();
      return {ok:true, disabled: !!btn.disabled};
    }"""
    )
    log(f"confirm click: {conf} enabled_wait={enabled}")
    page.wait_for_timeout(2000)
    shot(page, "after_confirm")

    # Retry Confirm once more if still open
    if page.get_by_text("Cover editor", exact=False).count():
        log("retry Confirm")
        page.evaluate(
            """() => {
              const btn = [...document.querySelectorAll('button')].find(b => /^\\s*Confirm\\s*$/i.test((b.innerText||'').trim()));
              if (btn) btn.click();
            }"""
        )
        page.wait_for_timeout(2000)

    open_still = bool(page.get_by_text("Cover editor", exact=False).count())
    shot(page, "cover_done")
    log(f"cover editor open={open_still}")
    if open_still:
        # Close ONLY the dialog X inside cover editor — not Cancel (that resets the whole upload)
        closed = page.evaluate(
            """() => {
              const dlg = [...document.querySelectorAll('.el-dialog, [class*=cover]')].find(d => /Cover editor/i.test(d.innerText||''));
              if (!dlg) return 'no-dlg';
              const x = dlg.querySelector('.el-dialog__headerbtn, [aria-label=Close], .el-dialog__close');
              if (x) { x.click(); return 'x'; }
              return 'no-x';
            }"""
        )
        log(f"close cover via X: {closed}")
        page.wait_for_timeout(1000)
        shot(page, "cover_closed")
        if page.get_by_text("Cover editor", exact=False).count():
            log("WARN: cover still open — leaving it; do not Cancel")
        else:
            log("cover closed via X (cover skipped)")


def set_cover(page) -> None:
    if not THUMB.exists():
        return
    # Prefer clicking the cover dropzone
    loc = page.get_by_text("Upload a cover", exact=False)
    if loc.count():
        try:
            with page.expect_file_chooser(timeout=4000) as fc:
                loc.first.click()
            fc.value.set_files(str(THUMB))
            log("cover via Upload a cover chooser")
        except Exception as e:
            log(f"cover chooser miss: {e}")
            inputs = page.locator('input[type="file"]')
            for i in range(inputs.count()):
                acc = (inputs.nth(i).get_attribute("accept") or "").lower()
                if any(x in acc for x in ("jpg", "png", "jpeg", "image")):
                    inputs.nth(i).set_input_files(str(THUMB))
                    log(f"cover via file input #{i}")
                    break
    page.wait_for_timeout(2000)
    dismiss_cover_editor(page)


# bilibili.tv allows max 10 tags — pick the strongest set matching YT meta
BILI_TAGS = [
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


def fill_tags(page) -> None:
    """Clear existing chips if possible, then add exactly BILI_TAGS (max 10)."""
    # Remove existing tag chips (X buttons inside tag pills)
    removed = page.evaluate(
        """() => {
      let n = 0;
      for (let i = 0; i < 15; i++) {
        const closes = [...document.querySelectorAll('.el-tag__close, .tag-close, [class*=tag] .el-icon-close, i.el-tag__close')];
        if (!closes.length) break;
        closes[0].click();
        n++;
      }
      return n;
    }"""
    )
    log(f"cleared {removed} existing tags")
    page.wait_for_timeout(400)

    tag_in = page.locator('input[placeholder*="tag" i], input[placeholder*="Tag"], input[placeholder*="Press Enter"]')
    if not tag_in.count():
        # fallback: input near Tags label
        tag_in = page.locator("div").filter(has_text="Tags").locator("input").last
    for tag in BILI_TAGS:
        try:
            tag_in.first.click()
            tag_in.first.fill(tag)
            page.keyboard.press("Enter")
            page.wait_for_timeout(200)
        except Exception as e:
            log(f"tag fail {tag}: {e}")
    # verify count from body
    body = page.inner_text("body")
    m = re.search(r"(\d{1,2})/10", body)
    log(f"tags filled target=10 ui={m.group(0) if m else '?'}: {BILI_TAGS}")


def set_schedule(page) -> None:
    """Scheduled Release @ 2026-07-12 16:30 (match YouTube AU/Sydney slot)."""
    date_s = PUBLISH.strftime("%Y-%m-%d")
    time_s = PUBLISH.strftime("%H:%M")

    if page.get_by_text("Scheduled Release", exact=True).count():
        page.get_by_text("Scheduled Release", exact=True).first.click()
        log("Scheduled Release selected")
        page.wait_for_timeout(800)

    result = page.evaluate(
        """({date_s, time_s}) => {
      const setNative = (el, val) => {
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) desc.set.call(el, val); else el.value = val;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        el.dispatchEvent(new Event('blur', {bubbles:true}));
      };
      const inputs = [...document.querySelectorAll('input')];
      const out = {date:false, time:false, values:[]};
      for (const el of inputs) {
        const ph = (el.placeholder || '').toLowerCase();
        const t = (el.type || '').toLowerCase();
        const val = el.value || '';
        // date field: type=date or value looks like YYYY-MM-DD or placeholder date
        if (!out.date && (t === 'date' || /^\\d{4}-\\d{2}-\\d{2}$/.test(val) || ph.includes('date') || ph.includes('yyyy'))) {
          setNative(el, date_s);
          out.date = true;
          out.values.push(['date', el.value, t, ph]);
          continue;
        }
        // time field: type=time or HH:MM
        if (!out.time && (t === 'time' || /^\\d{1,2}:\\d{2}$/.test(val) || ph.includes('time') || ph.includes('hh'))) {
          setNative(el, time_s);
          out.time = true;
          out.values.push(['time', el.value, t, ph]);
        }
      }
      // fallback: any input currently showing a clock-like value near release section
      if (!out.time) {
        const timeEl = inputs.find(el => /^\\d{1,2}:\\d{2}$/.test(el.value || ''));
        if (timeEl) { setNative(timeEl, time_s); out.time = true; out.values.push(['time-fallback', timeEl.value]); }
      }
      if (!out.date) {
        const dateEl = inputs.find(el => /^\\d{4}-\\d{2}-\\d{2}$/.test(el.value || ''));
        if (dateEl) { setNative(dateEl, date_s); out.date = true; out.values.push(['date-fallback', dateEl.value]); }
      }
      return out;
    }""",
        {"date_s": date_s, "time_s": time_s},
    )
    log(f"schedule set date={date_s} time={time_s} result={result}")

    # Playwright fill as backup on visible date/time inputs
    for sel, val in (
        ('input[type="date"]', date_s),
        ('input[type="time"]', time_s),
    ):
        loc = page.locator(sel)
        if loc.count():
            try:
                loc.first.fill(val)
                log(f"filled {sel}={val}")
            except Exception as e:
                log(f"fill {sel} fail: {e}")

    page.wait_for_timeout(500)
    shot(page, "scheduled")
    # sanity from page text
    body = page.inner_text("body")
    if "16:30" in body or "4:30" in body:
        log("schedule time 16:30 visible on page")
    elif "14:47" in body:
        log("WARN: page still shows 14:47")
    else:
        log(f"schedule body check — looking for time fields…")


def main() -> int:
    if LOG.exists():
        LOG.write_text("", encoding="utf-8")
    if not VIDEO.exists():
        log(f"missing {VIDEO}")
        return 1
    log(f"video={VIDEO} ({VIDEO.stat().st_size/1e9:.2f} GB)")
    log(f"title={META['title']}")
    log(f"schedule={PUBLISH.isoformat()}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        ctx = browser.new_context(storage_state=str(STORAGE), viewport={"width": 1440, "height": 960})
        page = ctx.new_page()
        page.set_default_timeout(60000)

        page.goto("https://studio.bilibili.tv/archive/new", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        shot(page, "start")
        if "SESSDATA" not in {c["name"] for c in ctx.cookies()}:
            raise RuntimeError("not logged in")
        log("logged in")

        with page.expect_file_chooser(timeout=15000) as fc:
            page.locator("#step-one__upload-btn").click()
        fc.value.set_files(str(VIDEO))
        log("video attached")
        page.wait_for_timeout(3000)
        shot(page, "after_video")
        body = page.inner_text("body")
        if "exceeds the limit" in body.lower():
            raise RuntimeError("size exceeds limit")
        log("size accepted")

        page.locator('input[maxlength="100"]').first.wait_for(timeout=60000)
        page.locator('input[maxlength="100"]').first.fill(META["title"][:100])
        # Prefer Introduction textarea explicitly
        intro = page.locator('textarea[placeholder*="introduction" i], textarea[placeholder*="Introduction"], textarea').first
        intro.fill(META["description"][:2000])
        fill_tags(page)
        log("meta filled")
        shot(page, "after_meta")

        log("setting cover (required by studio)")
        set_cover(page)
        if page.get_by_text("Cover editor", exact=False).count():
            log("cover editor still open — close via X only")
            page.evaluate(
                """() => {
                  const dlg = [...document.querySelectorAll('.el-dialog')].find(d => /Cover editor/i.test(d.innerText||''));
                  const x = dlg && dlg.querySelector('.el-dialog__headerbtn');
                  if (x) x.click();
                }"""
            )
            page.wait_for_timeout(1000)
        set_schedule(page)

        for i in range(180):
            if page.is_closed():
                raise RuntimeError("page closed")
            try:
                status = page.evaluate(
                    """() => {
                  const t = (document.body && document.body.innerText || '').slice(0, 5000);
                  const m = t.match(/(\\d{1,3})\\s*%/);
                  const completed = /upload completed/i.test(t);
                  return {
                    pct: m ? parseInt(m[1], 10) : null,
                    completed,
                    hasUploadNow: /Upload Now/i.test(t),
                  };
                }"""
                )
            except Exception as e:
                log(f"pct poll error: {e}")
                status = {"pct": None, "completed": False}
            pct = status.get("pct")
            log(f"pct={pct} completed={status.get('completed')}")
            STATE.write_text(json.dumps({"phase": "uploading", **status}, indent=2), encoding="utf-8")
            if i % 6 == 0:
                try:
                    shot(page, f"prog_{pct}")
                except Exception as e:
                    log(f"shot warn: {e}")
            if status.get("completed") or (pct is not None and pct >= 100):
                log("upload finished (completed banner or 100%)")
                break
            page.wait_for_timeout(15000)
        else:
            log("WARN: never reached 100%")

        # Final pass before submit
        set_schedule(page)
        fill_tags(page)
        shot(page, "before_submit")
        if page.get_by_role("button", name="Upload Now").count():
            page.get_by_role("button", name="Upload Now").first.click()
        else:
            page.locator("button:has-text('Upload Now')").first.click()
        log("clicked Upload Now")
        page.wait_for_timeout(8000)
        shot(page, "after_submit")
        log(f"final url={page.url}")
        log("body=" + page.inner_text("body")[:1000].replace("\n", " | "))
        ctx.storage_state(path=str(STORAGE))
        STATE.write_text(
            json.dumps(
                {
                    "phase": "done",
                    "url": page.url,
                    "title": META["title"],
                    "schedule": PUBLISH.isoformat(),
                    "tags": BILI_TAGS,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        page.wait_for_timeout(10000)
        browser.close()
    log("DONE")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        log(f"FATAL: {e}")
        raise
