"""
login_profile.py — Save a Google-authenticated Chrome profile for PersonaMeetBot.

Usage:
    python login_profile.py

A Chrome window will open to https://accounts.google.com.
Log into the Google account you want to use for meetings, then **close the
browser window**.  Your session is automatically saved in the ``user_login/``
directory next to this script.

After that, join a meeting as the logged-in account:

    python persona_meet_bot.py "https://meet.google.com/abc-defg-hij" --profile user_login
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright

# ── Profile directory ────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(SCRIPT_DIR, "user_login")

# ── Chrome flags (same stealth set used by PersonaMeetBot) ───────────────
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-background-networking",
    "--disable-sync",
    "--metrics-recording-only",
    "--disable-default-apps",
    "--mute-audio",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1280,900",
]


async def main() -> None:
    print("=" * 60)
    print("  Google Login — Profile Saver")
    print("=" * 60)
    print()
    print(f"  Profile directory: {PROFILE_DIR}")
    print()
    print("  1. A Chrome window will open to Google's sign-in page.")
    print("  2. Log into the Google account you want to use.")
    print("  3. When you're done, simply CLOSE the browser window.")
    print("     Your session will be saved automatically.")
    print()

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            channel="chrome",
            headless=False,
            args=CHROME_ARGS,
            ignore_default_args=["--enable-automation"],
            bypass_csp=True,
            viewport={"width": 1280, "height": 900},
        )

        page = context.pages[0] if context.pages else await context.new_page()

        # Remove the Playwright "navigator.webdriver" flag
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        await page.goto("https://accounts.google.com", wait_until="domcontentloaded")
        print("  Browser opened — log in and close the window when done.\n")

        # Wait until the user closes the browser
        try:
            await context.pages[0].wait_for_event("close", timeout=0)
            # Give Playwright a moment to flush profile data to disk
            await asyncio.sleep(1)
        except Exception:
            pass

        try:
            await context.close()
        except Exception:
            pass

    print()
    print("  Session saved to:", PROFILE_DIR)
    print()
    print("  To join a meeting as this account, run:")
    print()
    print('    python persona_meet_bot.py "<meet-url>" --profile user_login')
    print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Cancelled by user.")
        sys.exit(0)
