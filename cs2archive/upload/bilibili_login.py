"""Launch headed Chrome, let user login to bilibili, save storage state for MCP reuse."""
import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright

STORAGE_FILE = Path(__file__).resolve().parents[2] / ".bilibili_storage.json"
TARGET_URL = "https://www.bilibili.tv/en"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            storage_state=str(STORAGE_FILE) if STORAGE_FILE.exists() else None,
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await page.goto("https://www.bilibili.tv/en/login")
        print("=== Bilibili login page opened in headed browser ===")
        print("Log in manually (username/password or QR scan).")
        print("Waiting for login to complete...")

        # Wait until we leave the login page
        while True:
            try:
                await page.wait_for_url("**/login*", timeout=3000)
                await asyncio.sleep(2)
            except:
                break

        current_url = page.url
        print(f"Login detected! Current URL: {current_url}")

        # Navigate to main page to ensure full auth state
        await page.goto(TARGET_URL)
        await page.wait_for_timeout(3000)

        # Save storage state
        state = await context.storage_state()
        STORAGE_FILE.write_text(json.dumps(state, indent=2))
        print(f"Storage state saved to {STORAGE_FILE}")

        # Print cookies summary
        cookies = state.get("cookies", [])
        print(f"Saved {len(cookies)} cookies")
        for c in cookies:
            print(f"  {c['name']}: {c['value'][:30]}... (domain={c['domain']})")

        input("\nPress Enter to close browser...")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
