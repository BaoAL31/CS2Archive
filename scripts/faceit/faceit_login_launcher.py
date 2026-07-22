"""Launch a headed, persistent-profile Chrome at a FACEIT room so the user can
log in manually. Session saved to .faceit_profile/ and reused by scrapers.
Keeps browser open until user closes it (or Ctrl-C).
"""
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

PROFILE = Path(__file__).resolve().parents[2] / ".faceit_profile"
URL = sys.argv[1] if len(sys.argv) > 1 else (
    "https://www.faceit.com/en/cs2/room/1-9ee7de08-444a-4617-99a7-3fd5974de4f1"
)


def main():
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1920, "height": 1080},
        )
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded")
        print(f"[faceit_login_launcher] Opened {URL}")
        print("[faceit_login_launcher] Log in manually. Close the browser window when done.")
        try:
            while True:
                time.sleep(1)
                if len(browser.pages) == 0:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            browser.close()
        print("[faceit_login_launcher] Closed. Session saved to .faceit_profile/")


if __name__ == "__main__":
    main()
