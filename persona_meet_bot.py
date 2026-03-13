"""
PersonaMeet Bot — Joins a Google Meet, records audio, and plays an audio file through a virtual mic.
"""

import asyncio
import sys
import os
import shutil
import base64
import signal
import argparse
from datetime import datetime
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, Browser, BrowserContext

from inject_scripts import (
    STEALTH_SCRIPT,
    INIT_SCRIPT,
    JS_FIND_TOGGLE,
    JS_DISMISS_POPUPS,
    JS_FIND_JOIN,
    JS_PREJOIN_DETECTED,
    JS_IS_MEETING_OVER,
)

LOG = "[PersonaMeet Bot]"


def log(*args):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{ts} {LOG}", *args, flush=True)


def log_error(*args):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"{ts} {LOG} ERROR:", *args, file=sys.stderr, flush=True)


class PersonaMeetBot:

    def __init__(self, meet_url: str, audio_file: str = "sample.mp3",
                 user_data_dir: str = None, bot_name: str = "Meeting Agent"):
        self.meet_url = self._normalize_url(meet_url)
        self.audio_file = os.path.abspath(audio_file) if audio_file else None
        self.user_data_dir = os.path.abspath(user_data_dir) if user_data_dir else None
        self.bot_name = bot_name
        self.playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None
        self.bot_active: bool = False
        self.recording_active: bool = False
        self._audio_data: bytes = None

    # ─── Profile management ───────────────────────────────────

    def _get_profile_dir(self) -> str:
        if self.user_data_dir:
            return self.user_data_dir
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "_persona_bot_profile")

    def _nuke_profile_dir(self):
        # Only nuke the auto-created bot profile, never a user-supplied --profile
        if self.user_data_dir:
            return

        profile_dir = self._get_profile_dir()
        if not os.path.exists(profile_dir):
            return

        try:
            shutil.rmtree(profile_dir)
        except Exception:
            # Remove lock files and retry
            try:
                import glob
                for pattern in ('**/LOCK', '**/SingletonLock'):
                    for lock in glob.glob(os.path.join(profile_dir, pattern), recursive=True):
                        try:
                            os.remove(lock)
                        except Exception:
                            pass
                shutil.rmtree(profile_dir)
            except Exception as e:
                log(f"Warning: could not delete profile dir: {e}")

    # ─── URL helpers ──────────────────────────────────────────

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.strip()
        if url.startswith("meet.google.com/"):
            url = "https://" + url
        return url

    @staticmethod
    def _is_valid_meet_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            return parsed.hostname == "meet.google.com" and len(parsed.path) > 1
        except Exception:
            return url.startswith("meet.google.com/") and len(url) > len("meet.google.com/")

    # ─── Main entry point ────────────────────────────────────

    async def start(self):
        if not self._is_valid_meet_url(self.meet_url):
            log_error(f"Invalid Meet URL: {self.meet_url}")
            return

        has_audio = self.audio_file and os.path.exists(self.audio_file)
        if not has_audio:
            log("Warning: audio file not found — will join and record but won't play audio")

        log("=" * 60)
        log("PERSONAMEET BOT STARTING")
        log(f"  URL     : {self.meet_url}")
        log(f"  Name    : {self.bot_name}")
        log(f"  Audio   : {self.audio_file if has_audio else 'N/A'}")
        log(f"  Profile : {self.user_data_dir or 'ephemeral'}")
        log("=" * 60)

        try:
            self._nuke_profile_dir()
            await self._launch_browser()
            await self._setup_page()
            await self._navigate_to_meet()

            # Pre-join flow
            await self._wait_for_prejoin_ui()
            await self._fill_name_if_needed()

            # Disable mic & camera before joining
            await self._disable_with_retry("microphone", 8)
            await asyncio.sleep(1)
            await self._disable_with_retry("camera", 8)
            await asyncio.sleep(2)

            # Join the meeting
            await self._click_join()

            self.bot_active = True
            log("Bot is now active — recording meeting audio")

            # Start monitoring immediately so instant meeting end/rejection is caught
            monitor_task = asyncio.create_task(self._monitor_meeting_end())

            # Start recording immediately after clicking join
            log("Starting audio recording...")
            await asyncio.sleep(3)
            await self._start_recording()

            # Run post-join setup while monitor continues in parallel
            await self._post_join_flow()
            await monitor_task

            await self._stop_and_save_recording()
            log("Session complete")

        except Exception as e:
            log_error(f"Bot error: {e}")
            import traceback
            traceback.print_exc()
            try:
                await self._stop_and_save_recording()
            except Exception:
                pass
        finally:
            await self._cleanup()

    # ─── Browser launch ──────────────────────────────────────

    async def _launch_browser(self):
        log("Launching browser...")
        self.playwright = await async_playwright().start()

        launch_args = [
            "--use-fake-ui-for-media-stream",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--start-maximized",
        ]

        profile_dir = self._get_profile_dir()

        # Block Playwright defaults that expose automation
        suppress = ["--enable-automation", "--no-sandbox"]

        # Launch with real Chrome — no custom UA so Chrome uses its own real one
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                profile_dir,
                channel="chrome",
                headless=False,
                args=launch_args,
                ignore_default_args=suppress,
                permissions=["microphone", "camera", "notifications"],
                ignore_https_errors=True,
                no_viewport=True,
            )
        except Exception as e:
            if self.user_data_dir:
                log_error(f"Failed to launch Chrome with --profile: {e}")
                raise
            log(f"Chrome not available, falling back to Chromium")
            self.context = await self.playwright.chromium.launch_persistent_context(
                profile_dir,
                headless=False,
                args=launch_args,
                ignore_default_args=suppress,
                permissions=["microphone", "camera", "notifications"],
                ignore_https_errors=True,
                no_viewport=True,
            )

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        log("Browser launched")

    # ─── Page setup ──────────────────────────────────────────

    async def _setup_page(self):
        # Forward browser console to Python stdout
        self.page.on("console", lambda msg: print(
            f"        [BROWSER {msg.type.upper()}] {msg.text}", flush=True
        ))

        # Load audio file as base64 data URL (avoids Meet's service worker blocking routed URLs)
        self._audio_data_url = None
        if self.audio_file and os.path.exists(self.audio_file):
            self._audio_data = open(self.audio_file, "rb").read()
            b64 = base64.b64encode(self._audio_data).decode("ascii")
            self._audio_data_url = f"data:audio/mpeg;base64,{b64}"
            log(f"Audio loaded ({len(self._audio_data) / 1024:.1f} KB)")

        # Inject stealth patches (must be first)
        await self.page.add_init_script(STEALTH_SCRIPT)
        # Inject virtual audio + recording system
        await self.page.add_init_script(INIT_SCRIPT)

    # ─── Navigation ──────────────────────────────────────────

    async def _navigate_to_meet(self):
        log(f"Navigating to {self.meet_url}")
        await self.page.goto(self.meet_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await self.page.wait_for_load_state("load", timeout=30_000)
        except Exception:
            pass
        # Wait for UI framework to initialize
        await asyncio.sleep(4)

    # ─── Pre-join UI detection ───────────────────────────────

    async def _wait_for_prejoin_ui(self):
        # Wait up to 40s for mic/camera toggles or join button
        for i in range(80):
            try:
                if await self.page.evaluate(JS_PREJOIN_DETECTED):
                    log("Pre-join UI detected")
                    await asyncio.sleep(5)
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        raise TimeoutError("Pre-join UI not found within 40 seconds")

    # ─── Name field (when not signed in) ─────────────────────

    async def _fill_name_if_needed(self):
        try:
            name_input = None

            # Try placeholder match
            try:
                name_input = self.page.locator('input[placeholder="Your name"]')
                if await name_input.count() == 0:
                    name_input = None
            except Exception:
                name_input = None

            # Try text input near name prompt
            if not name_input:
                try:
                    name_input = self.page.locator('input[type="text"]').first
                    if await name_input.count() > 0:
                        page_text = await self.page.evaluate("() => document.body.innerText || ''")
                        if "your name" not in page_text.lower():
                            name_input = None
                    else:
                        name_input = None
                except Exception:
                    name_input = None

            # Fallback: any visible text input
            if not name_input:
                try:
                    name_input = self.page.locator('input:visible').first
                    if await name_input.count() > 0:
                        input_type = await name_input.get_attribute("type") or "text"
                        if input_type not in ("text", "", None):
                            name_input = None
                    else:
                        name_input = None
                except Exception:
                    name_input = None

            if name_input:
                await name_input.click()
                await name_input.fill(self.bot_name)
                await asyncio.sleep(0.5)
                value = await name_input.input_value()
                if value:
                    log(f"Name entered: \"{value}\"")
                else:
                    # Type character by character as fallback
                    await name_input.click(triple=True)
                    await self.page.keyboard.type(self.bot_name, delay=50)
                    await asyncio.sleep(0.3)
            else:
                log("No name field found (user may be signed in)")

        except Exception as e:
            log(f"Name field handling: {e}")

    # ─── Toggle buttons (mic / camera) ───────────────────────

    async def _disable_with_retry(self, button_type: str, max_attempts: int = 8):
        for attempt in range(1, max_attempts + 1):
            info = await self.page.evaluate(JS_FIND_TOGGLE, button_type)
            if info is None:
                if attempt < max_attempts:
                    await asyncio.sleep(1.5)
                continue
            if info["state"] == "off":
                return True
            if info["state"] == "on":
                await self.page.mouse.click(info["x"], info["y"])
                await asyncio.sleep(0.4)
                return True
            if attempt < max_attempts:
                await asyncio.sleep(1.5)
        return False

    async def _enable_toggle(self, button_type: str) -> bool:
        for attempt in range(1, 6):
            info = await self.page.evaluate(JS_FIND_TOGGLE, button_type)
            if info is None:
                await asyncio.sleep(1)
                continue
            if info["state"] == "on":
                return True
            if info["state"] == "off":
                await self.page.mouse.click(info["x"], info["y"])
                await asyncio.sleep(1.5)
                new_info = await self.page.evaluate(JS_FIND_TOGGLE, button_type)
                if new_info and new_info["state"] == "on":
                    return True
                continue
            await asyncio.sleep(1)
        return False

    # ─── Join button ─────────────────────────────────────────

    async def _click_join(self):
        for attempt in range(40):
            try:
                await self.page.evaluate(JS_DISMISS_POPUPS)
            except Exception:
                pass

            info = await self.page.evaluate(JS_FIND_JOIN)
            if info:
                log(f"Clicking join: \"{info['text']}\"")
                await self.page.mouse.click(info["x"], info["y"])
                return

            await asyncio.sleep(1)

        raise TimeoutError("Join button not found within 40 seconds")

    # ─── Recording ───────────────────────────────────────────

    async def _start_recording(self):
        if not self.bot_active:
            return

        for attempt in range(30):
            if not self.bot_active:
                return
            try:
                started = await self.page.evaluate("() => window.__personaMeetBot.startRecording()")
                if started:
                    self.recording_active = True
                    log("Recording started")
                    return
            except Exception:
                pass
            await asyncio.sleep(1)

        # Force-start even without confirmed remote tracks
        try:
            started = await self.page.evaluate("() => window.__personaMeetBot.startRecording()")
            self.recording_active = started
            if started:
                log("Recording started (awaiting participants)")
        except Exception as e:
            log_error(f"Recording start failed: {e}")

    # ─── Post-join flow (runs concurrently with monitor) ────

    async def _post_join_flow(self):
        try:
            # Wait for in-meeting UI to stabilize
            await asyncio.sleep(7)
            if not self.bot_active:
                return

            # Re-verify mic & camera OFF inside meeting
            await self._disable_with_retry("microphone", 5)
            await asyncio.sleep(0.5)
            await self._disable_with_retry("camera", 5)

            if not self.bot_active:
                return

            # Schedule bot speech
            # await self._schedule_bot_speech()
        except Exception:
            pass

    # ─── Bot speech ──────────────────────────────────────────

    async def _schedule_bot_speech(self):
        if not self._audio_data_url:
            return

        await asyncio.sleep(10)
        if not self.bot_active:
            return

        log("Playing audio through virtual mic...")

        try:
            # Resume AudioContext
            await self.page.evaluate("""
                async () => {
                    if (window.__personaMeetBot && window.__personaMeetBot.getVirtualAudioStream)
                        await window.__personaMeetBot.getVirtualAudioStream();
                }
            """)

            # Enable microphone
            mic_enabled = await self._enable_toggle("microphone")
            if not mic_enabled:
                raise Exception("Failed to enable microphone")
            await asyncio.sleep(2)

            # Inject and play audio data
            await self.page.evaluate(
                "(dataUrl) => { window.__personaMeetAudioDataUrl = dataUrl; }",
                self._audio_data_url,
            )
            result = await self.page.evaluate("""
                async () => {
                    try {
                        const url = window.__personaMeetAudioDataUrl;
                        if (!url) throw new Error('Audio data URL not injected');
                        return await window.__personaMeetBot.playSong(url);
                    } catch (err) {
                        console.error('[PersonaMeet Bot] Song error:', err);
                        return false;
                    }
                }
            """)

            if result:
                log("Audio playback finished")

            # Disable mic after playback
            await asyncio.sleep(1)
            await self._disable_with_retry("microphone", 5)

        except Exception as e:
            log_error(f"Error during bot speech: {e}")

    # ─── Meeting end detection ───────────────────────────────

    async def _monitor_meeting_end(self):
        last_url = self.page.url

        while self.bot_active:
            try:
                # Check for end-of-meeting or rejection UI
                if await self.page.evaluate(JS_IS_MEETING_OVER):
                    log("Meeting ended — saving recording before page navigates away")
                    await self._stop_and_save_recording()
                    self.bot_active = False
                    return

                # Check if page navigated away from Meet
                current_url = self.page.url
                if last_url != current_url:
                    if not current_url.startswith("https://meet.google.com/") or "/landing" in current_url:
                        log("Meeting ended (navigated away)")
                        await self._stop_and_save_recording()
                        self.bot_active = False
                        return
                    last_url = current_url

            except Exception:
                # Page closed or context destroyed
                log("Meeting ended (browser closed)")
                try:
                    await self._stop_and_save_recording()
                except Exception:
                    pass
                self.bot_active = False
                return

            await asyncio.sleep(2)

    # ─── Save recording ─────────────────────────────────────

    async def _stop_and_save_recording(self):
        if not self.recording_active:
            return

        log("Saving recording...")

        try:
            data_url = await self.page.evaluate(
                "async () => await window.__personaMeetBot.stopRecording()"
            )

            if data_url:
                _, b64data = data_url.split(",", 1)
                audio_bytes = base64.b64decode(b64data)

                ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
                filename = f"meeting-recording-{ts}.webm"
                filepath = os.path.join(os.getcwd(), filename)

                with open(filepath, "wb") as f:
                    f.write(audio_bytes)

                log(f"Recording saved: {filename} ({len(audio_bytes) / 1024:.1f} KB)")
            else:
                log("No audio data captured")

        except Exception as e:
            log_error(f"Error saving recording: {e}")

        self.recording_active = False

    # ─── Cleanup ─────────────────────────────────────────────

    async def _cleanup(self):
        # Close the browser
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

        # Nuke bot profile for next run
        self._nuke_profile_dir()


# ─── CLI ─────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="PersonaMeet Bot — Joins a Google Meet, records audio, and plays an audio file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python persona_meet_bot.py "https://meet.google.com/abc-defg-hij" --name "Meeting Agent"\n'
            '  python persona_meet_bot.py "https://meet.google.com/abc-defg-hij" --profile user_login\n'
            "\n"
            "Modes:\n"
            "  Anonymous (default): fresh profile each run, enters --name on pre-join screen.\n"
            "  Logged-in (--profile): uses a saved Chrome profile from login_profile.py.\n"
        ),
    )
    parser.add_argument("meet_url", help="Google Meet URL to join")
    parser.add_argument("--audio", default="sample.mp3", help="Audio file to play (default: sample.mp3)")
    parser.add_argument("--name", default="Meeting Agent", help="Bot display name (default: Meeting Agent)")
    parser.add_argument("--profile", default=None, help="Chrome profile directory for logged-in session")

    args = parser.parse_args()

    bot = PersonaMeetBot(
        meet_url=args.meet_url,
        audio_file=args.audio,
        user_data_dir=args.profile,
        bot_name=args.name,
    )
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
