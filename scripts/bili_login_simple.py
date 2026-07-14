"""Open headed browser to login to bilibili.tv, save cookies on close."""
import json
from playwright.sync_api import sync_playwright

STORAGE_FILE = r"D:\Projects\CS2Archive\.bilibili_storage.json"

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.goto("https://www.bilibili.tv/en/login")
    print("=== Browser opened. Login to bilibili.tv, then close the browser window. ===")
    
    # Keep alive until user presses Enter in console
    input('Press Enter in this terminal after login and closing the browser...')
    
    # Save storage state
    state = context.storage_state()
    with open(STORAGE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
    
    cookies = state.get('cookies', [])
    print(f'\nSaved {len(cookies)} cookies to {STORAGE_FILE}')
    for c in cookies:
        print(f'  {c["name"]}: {c["value"][:40]}... (domain={c["domain"]})')
    
    context.close()
    browser.close()
