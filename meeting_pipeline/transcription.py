from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .config import PipelineConfig
from .models import TranscriptData, TranscriptSegment


class TranscriptionError(RuntimeError):
    pass


def _response_to_dict(response) -> dict:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if isinstance(response, dict):
        return response
    raise TranscriptionError("Unexpected transcription response type")


class AudioTranscriber:
    def __init__(self, config: PipelineConfig):
        self.config = config

    def _ensure_speaker_labels(self, segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
        """Fill missing speaker labels with synthetic Speaker 1/2/... turn labels."""
        if not segments:
            return segments

        # If model already produced labels for all segments, keep them.
        if all((segment.speaker or "").strip() for segment in segments):
            return segments

        total_speakers = max(1, int(self.config.synthetic_speaker_count))
        turn_gap = max(0.5, float(self.config.speaker_turn_gap_seconds))

        current_speaker_index = 1
        previous_end: float | None = None

        for segment in segments:
            if (segment.speaker or "").strip():
                previous_end = segment.end
                continue

            if previous_end is not None and (segment.start - previous_end) >= turn_gap:
                # Rotate to the next synthetic speaker on larger pauses.
                current_speaker_index = (current_speaker_index % total_speakers) + 1

            segment.speaker = f"Speaker {current_speaker_index}"
            previous_end = segment.end

        return segments

    def transcribe(self, audio_path: str | Path) -> TranscriptData:
        """Transcription priority: OpenAI → Gemini → local faster-whisper."""
        if self.config.openai_api_key:
            try:
                return self._transcribe_openai(audio_path)
            except Exception as exc:
                print(f"[Pipeline] OpenAI transcription failed ({exc}). Trying Gemini...")
        if self.config.gemini_api_key:
            try:
                return self._transcribe_gemini(audio_path)
            except Exception as exc:
                print(f"[Pipeline] Gemini transcription failed ({exc}). Falling back to local Whisper...")
        return self._transcribe_local(audio_path)

    # ── OpenAI backend ─────────────────────────────────────────────────

    def _transcribe_openai(self, audio_path: str | Path) -> TranscriptData:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise TranscriptionError(
                "The openai package is not installed. Install it with: pip install openai"
            ) from exc

        client = OpenAI(api_key=self.config.openai_api_key)

        with Path(audio_path).open("rb") as audio_file:
            response = client.audio.transcriptions.create(
                model=self.config.transcription_model,
                file=audio_file,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )

        data = _response_to_dict(response)
        segments = [
            TranscriptSegment(
                start=float(seg.get("start", 0.0)),
                end=float(seg.get("end", 0.0)),
                text=(seg.get("text") or "").strip(),
                speaker=None,
            )
            for seg in data.get("segments", [])
        ]
        segments = self._ensure_speaker_labels(segments)

        transcript_text = (data.get("text") or "").strip()
        if not transcript_text:
            raise TranscriptionError("OpenAI transcription completed but returned no text.")

        return TranscriptData(
            text=transcript_text,
            language=data.get("language"),
            duration_seconds=segments[-1].end if segments else None,
            segments=segments,
        )

    # ── Gemini backend ──────────────────────────────────────────────────

    def _transcribe_gemini(self, audio_path: str | Path) -> TranscriptData:
        """Transcribe audio using Gemini 1.5 Flash — excellent for Hindi/Marathi/English."""
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise TranscriptionError(
                "google-generativeai package not installed. Run: pip install google-generativeai"
            ) from exc

        genai.configure(api_key=self.config.gemini_api_key)
        audio_path = Path(audio_path)
        mime_map = {
            ".webm": "audio/webm",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".mp4": "audio/mp4",
            ".m4a": "audio/mp4",
            ".ogg": "audio/ogg",
        }
        mime_type = mime_map.get(audio_path.suffix.lower(), "audio/webm")

        print("[Pipeline] Uploading audio to Gemini for transcription...")
        audio_file = genai.upload_file(path=str(audio_path), mime_type=mime_type)

        # Poll until Gemini finishes processing the file
        while audio_file.state.name == "PROCESSING":
            time.sleep(2)
            audio_file = genai.get_file(audio_file.name)
        if audio_file.state.name != "ACTIVE":
            raise TranscriptionError(
                f"Gemini file processing failed (state: {audio_file.state.name})."
            )

        print("[Pipeline] Transcribing with Gemini...")
        model = genai.GenerativeModel(self.config.gemini_model)
        prompt = (
            "Transcribe this audio completely and accurately. "
            "Keep the original language — do NOT translate. Include every word spoken. "
            "Return ONLY a valid JSON object (no markdown, no extra text) with this structure:\n"
            '{"language": "ISO_639-1_code", "duration_seconds": 0.0, '
            '"segments": [{"start": 0.0, "end": 0.0, "speaker": "Speaker 1", "text": "spoken text"}]}\n'
            "Use approximate timestamps in seconds. If multiple voices are present, keep speaker labels "
            "consistent as Speaker 1, Speaker 2, etc."
        )
        response = model.generate_content(
            [prompt, audio_file],
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )

        # Clean up uploaded file from Gemini servers
        try:
            genai.delete_file(audio_file.name)
        except Exception:
            pass

        raw = (response.text or "").strip()
        if not raw:
            raise TranscriptionError("Gemini returned empty transcription response.")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                raise TranscriptionError(
                    f"Gemini transcription response was not valid JSON: {raw[:300]}"
                )

        segments = [
            TranscriptSegment(
                start=float(seg.get("start", 0.0)),
                end=float(seg.get("end", 0.0)),
                text=(seg.get("text") or "").strip(),
                speaker=(seg.get("speaker") or seg.get("speaker_label") or "").strip() or None,
            )
            for seg in data.get("segments", [])
            if (seg.get("text") or "").strip()
        ]
        segments = self._ensure_speaker_labels(segments)

        transcript_text = " ".join(seg.text for seg in segments).strip()
        if not transcript_text:
            raise TranscriptionError(
                "Gemini transcription completed but returned no text. "
                "The audio may be too short or silent."
            )

        duration = float(data.get("duration_seconds") or 0) or (
            segments[-1].end if segments else None
        )
        return TranscriptData(
            text=transcript_text,
            language=data.get("language"),
            duration_seconds=duration,
            segments=segments,
        )

    # ── Local faster-whisper backend ───────────────────────────────────

    def _transcribe_local(self, audio_path: str | Path) -> TranscriptData:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError(
                "faster-whisper is not installed and OPENAI_API_KEY is not set.\n"
                "Install faster-whisper with: pip install faster-whisper\n"
                "Or set OPENAI_API_KEY to use the OpenAI backend."
            ) from exc

        model_size = self.config.local_whisper_model
        print(f"[Pipeline] Loading local Whisper model '{model_size}' (first run downloads it)...")

        cpu_threads = max(1, int(self.config.local_whisper_cpu_threads))
        beam_size = max(1, int(self.config.local_whisper_beam_size))
        print(
            f"[Pipeline] Local Whisper settings: beam_size={beam_size}, cpu_threads={cpu_threads}"
        )

        model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            cpu_threads=cpu_threads,
        )

        def _run_transcription(vad: bool) -> tuple[list[TranscriptSegment], list[str], object]:
            segs_iter, det_info = model.transcribe(
                str(audio_path),
                beam_size=beam_size,
                vad_filter=vad,
                task="transcribe",
            )
            seg_list: list[TranscriptSegment] = []
            text_parts: list[str] = []
            for seg in segs_iter:
                text = (seg.text or "").strip()
                if not text:
                    continue
                seg_list.append(
                    TranscriptSegment(
                        start=seg.start,
                        end=seg.end,
                        text=text,
                        speaker=None,
                    )
                )
                text_parts.append(text)
            return seg_list, text_parts, det_info

        # First attempt: with VAD filter (faster, skips silence)
        segments, full_text_parts, info = _run_transcription(vad=True)

        # Retry without VAD filter — helps with non-English / multilingual audio
        # where VAD may incorrectly silence valid speech segments
        if not full_text_parts:
            print(
                f"[Pipeline] VAD-filtered pass produced no text "
                f"(audio may be in Hindi/Marathi or a non-English language). "
                f"Retrying without VAD filter..."
            )
            segments, full_text_parts, info = _run_transcription(vad=False)

        transcript_text = " ".join(full_text_parts).strip()
        if not transcript_text:
            raise TranscriptionError(
                "Local transcription produced no text after both VAD and non-VAD passes.\n"
                "Possible causes:\n"
                "  1. The recording is silent or very short.\n"
                "  2. The audio is in Hindi, Marathi, or another non-English language and the\n"
                f"     '{model_size}' Whisper model is struggling. Try a larger model:\n"
                "       export PERSONA_LOCAL_WHISPER_MODEL=small   (recommended for Hindi/Marathi)\n"
                "       export PERSONA_LOCAL_WHISPER_MODEL=medium  (best multilingual quality)\n"
                "  3. The in-browser recording captured no remote audio (WebRTC mix issue)."
            )

        return TranscriptData(
            text=transcript_text,
            language=getattr(info, "language", None),
            duration_seconds=segments[-1].end if segments else None,
            segments=self._ensure_speaker_labels(segments),
        )