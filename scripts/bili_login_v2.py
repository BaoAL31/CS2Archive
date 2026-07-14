"""
Launch headed Chrome via CDP on a fixed port.
User logs in to bilibili.tv manually.
Script polls for auth cookies and saves them.
"""
import json
import os
import signal
import subprocess
import sys
import time
import atexit

STORAGE_FILE = r"D:\Projects\CS2Archive\.bilibili_storage.json"
CDP_PORT = 9229
USER_DATA_DIR = r"D:\Projects\CS2Archive\.bili-chrome-profile"

chrome_candidates = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
]

CHROME_PATH = None
for c in chrome_candidates:
    if os.path.exists(c):
        CHROME_PATH = c
        break

if not CHROME_PATH:
    print("ERROR: Chrome not found")
    sys.exit(1)

print(f"Using: {CHROME_PATH}")

# Kill any leftover chrome on our port
subprocess.run(
    f"wmic process where \"commandline like '%--remote-debugging-port={CDP_PORT}%'\" delete",
    shell=True, capture_output=True, timeout=5
)
time.sleep(1)

# Launch Chrome headed
proc = subprocess.Popen(
    [
        CHROME_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

def cleanup():
    if proc and proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=5)

atexit.register(cleanup)

print(f"\n=== Chrome opened on port {CDP_PORT} ===")
print("1. Navigate to https://www.bilibili.tv/en/login")
print("2. Login with your account")
print("3. After login, press Ctrl+C in this terminal to save cookies")
print()

# Poll for bilibili.tv cookies
try:
    from playwright.sync_api import sync_playwright

    while True:
        time.sleep(3)
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else None
                if page:
                    url = page.url
                    print(f"  Current page: {url[:80]}")

                # Check for bilibili.tv auth cookies
                cookies = context.cookies()
                tv_cookies = [c for c in cookies if "bilibili.tv" in c.get("domain", "")]
                auth_cookies = [c for c in tv_cookies if c.get("name") in ("sid", "DedeUserID", "SESSDATA", "bili_jct", "sessionid")]

                if auth_cookies:
                    print(f"\n  Found {len(auth_cookies)} auth cookies for bilibili.tv!")
                    state = context.storage_state()
                    with open(STORAGE_FILE, "w") as f:
                        json.dump(state, f, indent=2)
                    print(f"  Saved to {STORAGE_FILE}")
                    print("  You can close Chrome now. Cookies saved!")
                    break

                browser.close()
        except Exception as e:
            print(f"  Waiting for browser... ({e})")
            time.sleep(2)

except KeyboardInterrupt:
    # User pressed Ctrl+C — save cookies anyway
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
            context = browser.contexts[0]
            state = context.storage_state()
            with open(STORAGE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            cookies = state.get("cookies", [])
            tv_cookies = [c for c in cookies if "bilibili.tv" in c.get("domain", "")]
            print(f"\nSaved {len(tv_cookies)} bilibili.tv cookies to {STORAGE_FILE}")
            browser.close()
    except Exception as e:
        print(f"Could not save cookies: {e}")

print("Done.")
