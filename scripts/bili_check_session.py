"""Check if bilibili.tv session is active in CloakBrowser profile, save cookies."""
import json
from pathlib import Path
from cloakbrowser import launch_persistent_context

PROFILE = Path(__file__).parent.parent / ".bili-cloak-profile"
STORAGE = Path(__file__).parent.parent / ".bilibili_storage.json"
TARGET = "https://www.bilibili.tv/en/space/1604674785"

ctx = launch_persistent_context(
    str(PROFILE),
    headless=True,
    humanize=True,
    channel="chrome",
)

page = ctx.new_page()
page.goto("https://www.bilibili.tv/en")

# Check cookies
cookies = page.context.cookies()
tv_cookies = [c for c in cookies if "bilibili.tv" in c.get("domain", "")]
print(f"bilibili.tv cookies: {len(tv_cookies)}")
for c in tv_cookies:
    print(f"  {c['name']}: {c['value'][:50]}...")

# Check if logged in
if any(c["name"] in ("sid", "sessionid", "SESSDATA") for c in tv_cookies):
    print("\nSESSION ACTIVE! Navigating to space page...")
    page.goto(TARGET)
    page.wait_for_timeout(3000)
    print(f"Final URL: {page.url}")
    print(f"Title: {page.title()}")

    # Save storage state
    state = ctx.storage_state()
    with open(STORAGE, 'w') as f:
        json.dump(state, f, indent=2)
    print(f'\nSaved to {STORAGE}')
else:
    print("\nNo session. Navigate to login page...")
    page.goto("https://www.bilibili.tv/en/login")
    print(f"Login page title: {page.title()}")

ctx.close()
