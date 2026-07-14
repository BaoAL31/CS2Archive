"""Open visible CloakBrowser, let user login to bilibili.tv, save cookies."""
import json
import time
from pathlib import Path
from cloakbrowser import launch_persistent_context

PROFILE = Path(__file__).parent.parent / ".bili-cloak-profile"
STORAGE = Path(__file__).parent.parent / ".bilibili_storage.json"

PROFILE.mkdir(parents=True, exist_ok=True)

print("=== Opening bilibili.tv in visible CloakBrowser ===")

ctx = launch_persistent_context(
    str(PROFILE),
    headless=False,
    viewport={"width": 1280, "height": 800},
    humanize=True,
    channel="chrome",
)

page = ctx.new_page()
page.goto("https://www.bilibili.tv/en/login")
print("Browser open. Login manually, then close the browser window.")
print("Cookies auto-saved on close.\n")

# Wait until page closes (user closes browser window)
page.wait_for_event("close", timeout=0)
print("\nBrowser closed.")

# Save storage state
time.sleep(0.5)
try:
    state = ctx.storage_state()
    with open(STORAGE, "w") as f:
        json.dump(state, f, indent=2)
    cookies = state.get("cookies", [])
    tv = [c for c in cookies if "bilibili.tv" in c.get("domain", "")]
    print(f"Saved {len(tv)} bilibili.tv cookies to {STORAGE}")
except Exception as e:
    print(f"Save error: {e}")

print("Done.")
