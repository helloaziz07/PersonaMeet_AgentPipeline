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
import time
from datetime import datetime
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, Browser, BrowserContext

from meeting_pipeline import MeetingProcessingPipeline, PipelineConfig
from inject_scripts import (
    STEALTH_SCRIPT,
    INIT_SCRIPT,
    JS_FIND_TOGGLE,
    JS_DISMISS_POPUPS,
    JS_FIND_JOIN,
    JS_OPEN_CHAT_PANEL,
    JS_GET_CHAT_MESSAGES,
    JS_GET_PARTICIPANTS,
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
        self._audio_data_url: str | None = None
        self.chat_messages: list[dict] = []
        self.participant_names: list[str] = []
        self._chat_seen_keys: set[tuple[str | None, str, str | None]] = set()
        self._chat_capture_task = None
        self.session_started_monotonic: float | None = None
        self.session_dir = os.path.join(
            os.getcwd(),
            f"meeting-session-{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}"
        )
        self.recording_path: str | None = None
        self.pipeline_outputs: dict | None = None
        self._post_process_started: bool = False

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

        self.session_started_monotonic = time.monotonic()
        os.makedirs(self.session_dir, exist_ok=True)

        has_audio = self.audio_file and os.path.exists(self.audio_file)
        if not has_audio:
            log("Warning: audio file not found — will join and record but won't play audio")

        log("=" * 60)
        log("PERSONAMEET BOT STARTING")
        log(f"  URL     : {self.meet_url}")
        log(f"  Name    : {self.bot_name}")
        log(f"  Audio   : {self.audio_file if has_audio else 'N/A'}")
        log(f"  Output  : {self.session_dir}")
        log(f"  Profile : {self.user_data_dir or 'ephemeral'}")
        log("=" * 60)

        try:
            self._nuke_profile_dir()
            await self._launch_browser()
            await self._setup_page()
            await self._navigate_to_meet()

            # Pre-load Whisper model in background so it's cached before the meeting ends.
            # This way post-meeting transcription starts instantly instead of downloading then.
            whisper_preload_task = asyncio.create_task(self._preload_whisper_model())

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
            await self._run_post_meeting_pipeline()
            log("Session complete")

        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                log("Interrupted by user — attempting graceful shutdown and post-processing...")
            else:
                log_error(f"Bot error: {e}")
            import traceback
            traceback.print_exc()
            try:
                await self._stop_and_save_recording()
            except Exception:
                pass
            try:
                await self._run_post_meeting_pipeline()
            except Exception:
                pass
        finally:
            # Final safety net: if recording exists, still try pipeline before cleanup.
            try:
                await self._stop_and_save_recording()
            except Exception:
                pass
            try:
                await self._run_post_meeting_pipeline()
            except Exception:
                pass
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

            await self._open_chat_panel()
            await self._capture_participant_names()
            self._chat_capture_task = asyncio.create_task(self._capture_chat_messages())

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
        end_signal_hits = 0

        while self.bot_active:
            try:
                # Check for end-of-meeting or rejection UI
                if await self.page.evaluate(JS_IS_MEETING_OVER):
                    end_signal_hits += 1
                else:
                    end_signal_hits = 0

                # Require repeated confirmation to avoid false positives from transient page text.
                if end_signal_hits >= 2:
                    log("Meeting ended — saving recording before page navigates away")
                    await self._stop_and_save_recording()
                    await self._close_browser_runtime()
                    await self._run_post_meeting_pipeline()
                    self.bot_active = False
                    return

                # Check if page navigated away from Meet
                current_url = self.page.url
                if last_url != current_url:
                    if not current_url.startswith("https://meet.google.com/") or "/landing" in current_url:
                        log("Meeting ended (navigated away)")
                        await self._stop_and_save_recording()
                        await self._close_browser_runtime()
                        await self._run_post_meeting_pipeline()
                        self.bot_active = False
                        return
                    last_url = current_url

            except Exception:
                # Page closed or context destroyed
                log("Meeting ended (browser closed)")
                try:
                    await self._stop_and_save_recording()
                    await self._run_post_meeting_pipeline()
                except Exception:
                    pass
                self.bot_active = False
                return

            await asyncio.sleep(2)

    async def _close_browser_runtime(self):
        """Close browser early when meeting ends so user does not have to close it manually."""
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
                filepath = os.path.join(self.session_dir, filename)

                with open(filepath, "wb") as f:
                    f.write(audio_bytes)

                self.recording_path = filepath
                log(f"Recording saved: {filename} ({len(audio_bytes) / 1024:.1f} KB)")
            else:
                log("No audio data captured")

        except Exception as e:
            log_error(f"Error saving recording: {e}")

        self.recording_active = False

    async def _open_chat_panel(self):
        try:
            await asyncio.sleep(2)
            opened = await self.page.evaluate(JS_OPEN_CHAT_PANEL)
            if opened:
                log("Chat panel opened")
        except Exception:
            pass

    async def _capture_participant_names(self):
        try:
            await asyncio.sleep(2)
            names = await self.page.evaluate(JS_GET_PARTICIPANTS)
            if not isinstance(names, list):
                return

            bot_name = (self.bot_name or "").strip().lower()
            cleaned: list[str] = []
            seen: set[str] = set()

            for item in names:
                name = str(item or "").strip()
                if not name:
                    continue
                lower = name.lower()
                if lower == bot_name:
                    continue
                if lower in seen:
                    continue
                seen.add(lower)
                cleaned.append(name)

            self.participant_names = cleaned
            if cleaned:
                log(f"Captured participants: {', '.join(cleaned)}")
            else:
                log("Participant names not found in UI yet.")
        except Exception:
            pass

    async def _capture_chat_messages(self):
        while self.bot_active:
            try:
                messages = await self.page.evaluate(JS_GET_CHAT_MESSAGES)
                if isinstance(messages, list):
                    self._merge_chat_messages(messages)
            except Exception:
                pass
            await asyncio.sleep(4)

    def _merge_chat_messages(self, messages: list[dict]):
        now_relative = None
        if self.session_started_monotonic is not None:
            now_relative = max(0.0, time.monotonic() - self.session_started_monotonic)

        for item in messages:
            text = (item.get("text") or "").strip()
            author = (item.get("author") or "").strip() or None
            captured_at = item.get("captured_at")
            if not text:
                continue

            key = (author, text, captured_at)
            if key in self._chat_seen_keys:
                continue

            self._chat_seen_keys.add(key)
            self.chat_messages.append(
                {
                    "author": author,
                    "text": text,
                    "captured_at": captured_at,
                    "relative_seconds": now_relative,
                }
            )

    async def _preload_whisper_model(self):
        """Download / warm-up the local Whisper model in the background while the
        meeting is in progress so that post-meeting transcription starts instantly."""
        config = PipelineConfig(base_dir=self.session_dir)
        if config.sarvam_api_key or config.openai_api_key or config.gemini_api_key:
            return  # API backend — nothing to pre-load locally

        model_size = config.local_whisper_model
        log(f"Pre-loading Whisper model '{model_size}' in background (downloads once if not cached)...")
        try:
            await asyncio.to_thread(self._load_whisper_model, model_size)
            log(f"Whisper model '{model_size}' ready.")
        except Exception as exc:
            log(f"Warning: Whisper pre-load failed ({exc}). Will retry during post-processing.")

    @staticmethod
    def _load_whisper_model(model_size: str):
        """Blocking call — runs in a thread. Loads (and caches) the faster-whisper model."""
        from faster_whisper import WhisperModel
        WhisperModel(model_size, device="cpu", compute_type="int8")

    async def _run_post_meeting_pipeline(self):
        if self._post_process_started or not self.recording_path:
            return

        self._post_process_started = True
        log("Starting post-meeting processing pipeline (transcription + report). This can take 1-3 minutes.")

        config = PipelineConfig(base_dir=self.session_dir)
        pipeline = MeetingProcessingPipeline(config)
        metadata = {
            "meet_url": self.meet_url,
            "bot_name": self.bot_name,
            "participant_names": self.participant_names,
        }

        try:
            # Shield so Ctrl+C cancellation does not abort processing midway.
            self.pipeline_outputs = await asyncio.shield(
                asyncio.to_thread(
                    pipeline.process,
                    self.recording_path,
                    self.chat_messages,
                    metadata,
                )
            )
            log(f"Transcript saved: {self.pipeline_outputs['transcript_path']}")
            log(f"Report saved: {self.pipeline_outputs['report_path']}")
        except Exception as e:
            log_error(f"Post-meeting processing failed: {e}")
            log_error("If running without OPENAI_API_KEY, ensure faster-whisper is installed and wait for model download on first run.")

    # ─── Cleanup ─────────────────────────────────────────────

    async def _cleanup(self):
        if self._chat_capture_task:
            self._chat_capture_task.cancel()
            try:
                await self._chat_capture_task
            except BaseException:
                pass

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
