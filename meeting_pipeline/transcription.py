from __future__ import annotations

import json
import re
import tempfile
import time
import wave
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


def _extract_recommended_sarvam_model(error_text: str) -> str | None:
    """Extract recommended model from Sarvam deprecation messages."""
    if not error_text:
        return None

    lowered = error_text.lower()

    # Examples: "use 'saarika:v2.5'", "using Saaras v3"
    colon_match = re.search(r"(saarika\s*:\s*v[\d.]+|saaras\s*:\s*v[\d.]+)", lowered)
    if colon_match:
        return re.sub(r"\s+", "", colon_match.group(1))

    spaced_match = re.search(r"(saarika\s+v[\d.]+|saaras\s+v[\d.]+)", lowered)
    if spaced_match:
        return spaced_match.group(1).replace(" ", ":")

    return None


def _extract_sarvam_error_message(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    if isinstance(payload.get("detail"), str):
        return payload.get("detail") or ""
    err = payload.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str):
            return msg
    return ""


def _extract_sarvam_job_id(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None

    candidates = [
        payload.get("job_id"),
        payload.get("id"),
        payload.get("request_id"),
        payload.get("task_id"),
    ]
    nested = payload.get("data")
    if isinstance(nested, dict):
        candidates.extend([
            nested.get("job_id"),
            nested.get("id"),
            nested.get("request_id"),
            nested.get("task_id"),
        ])

    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _sarvam_status_value(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    status = payload.get("status")
    if isinstance(status, str):
        return status.strip().lower()
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("status"), str):
        return data.get("status", "").strip().lower()
    return ""


def _sarvam_extract_result_payload(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None

    for key in ("result", "output", "transcript", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value

    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("result", "output"):
            value = data.get(key)
            if isinstance(value, dict):
                return value

    # In some APIs, final payload already contains transcript fields at top level.
    if any(key in payload for key in ("text", "transcript", "segments", "diarized_transcript")):
        return payload
    return None


class AudioTranscriber:
    def __init__(self, config: PipelineConfig):
        self.config = config
        self.last_backend: str | None = None

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
        """Transcription priority: Sarvam → OpenAI → Gemini → local faster-whisper."""
        sarvam_attempted = False
        if self.config.sarvam_api_key:
            sarvam_attempted = True
            if self.config.sarvam_use_batch_diarization:
                try:
                    result = self._transcribe_sarvam_batch_diarized(audio_path)
                    self.last_backend = "sarvam-batch"
                    return result
                except Exception as exc:
                    print(f"[Pipeline] Sarvam batch diarization unavailable ({exc}). Falling back to standard Sarvam...")
            try:
                result = self._transcribe_sarvam(audio_path)
                self.last_backend = "sarvam"
                return result
            except Exception as exc:
                print(f"[Pipeline] Sarvam transcription failed ({exc}). Trying OpenAI...")
        if self.config.openai_api_key:
            try:
                result = self._transcribe_openai(audio_path)
                self.last_backend = "openai"
                return result
            except Exception as exc:
                print(f"[Pipeline] OpenAI transcription failed ({exc}). Trying Gemini...")
        if self.config.gemini_api_key:
            try:
                result = self._transcribe_gemini(audio_path)
                self.last_backend = "gemini"
                return result
            except Exception as exc:
                print(f"[Pipeline] Gemini transcription failed ({exc}). Falling back to local Whisper...")
        if sarvam_attempted:
            print("[Pipeline] API fallback exhausted (Sarvam failed and no OpenAI/Gemini key available). Using local Whisper.")
        else:
            print("[Pipeline] No SARVAM_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY detected. Using local Whisper.")
        result = self._transcribe_local(audio_path)
        self.last_backend = "local"
        return result

    # ── Sarvam backend ──────────────────────────────────────────────────

    def _sarvam_request(
        self,
        audio_path: Path,
        mime_type: str,
        model_name: str,
        with_diarization: bool,
    ):
        import requests

        payload = {
            "model": model_name,
            "with_timestamps": "true",
        }
        if with_diarization:
            payload["with_diarization"] = "true"
        # Saaras v3 docs recommend explicit output mode for transcription.
        if model_name.lower().startswith("saaras"):
            payload["mode"] = self.config.sarvam_mode

        with audio_path.open("rb") as audio_file:
            return requests.post(
                "https://api.sarvam.ai/speech-to-text",
                headers={"api-subscription-key": self.config.sarvam_api_key or ""},
                files={"file": (audio_path.name, audio_file, mime_type)},
                data=payload,
                timeout=300,
            )

    @staticmethod
    def _sarvam_normalize_data(data: dict) -> dict:
        if isinstance(data.get("output"), dict):
            return data.get("output") or data
        if isinstance(data.get("result"), dict):
            return data.get("result") or data
        return data

    def _parse_sarvam_payload(self, payload: dict) -> tuple[str, str | None, list[TranscriptSegment]]:
        data = self._sarvam_normalize_data(payload)

        transcript_text = (
            (data.get("transcript") or "").strip()
            or (data.get("text") or "").strip()
            or (data.get("full_text") or "").strip()
        )

        segments_payload = []
        if isinstance(data.get("diarized_transcript"), dict):
            diarized = data.get("diarized_transcript") or {}
            segments_payload = diarized.get("entries") or diarized.get("segments") or []
        elif isinstance(data.get("diarized_transcript"), list):
            segments_payload = data.get("diarized_transcript") or []
        elif isinstance(data.get("segments"), list):
            segments_payload = data.get("segments") or []

        segments: list[TranscriptSegment] = []
        for seg in segments_payload:
            text = (seg.get("transcript") or seg.get("text") or "").strip()
            if not text:
                continue
            speaker = (
                seg.get("speaker")
                or seg.get("speaker_label")
                or seg.get("speaker_id")
                or ""
            )
            segments.append(
                TranscriptSegment(
                    start=float(seg.get("start") or 0.0),
                    end=float(seg.get("end") or 0.0),
                    text=text,
                    speaker=str(speaker).strip() or None,
                )
            )

        language = data.get("language_code") or data.get("language")
        if isinstance(language, str) and "-" in language:
            language = language.split("-", 1)[0]

        return transcript_text, language, segments

    def _transcribe_sarvam_chunked(self, audio_path: Path, model_name: str) -> TranscriptData:
        """Chunk long recordings and merge Sarvam transcripts with timestamp offsets."""
        try:
            import av
            import numpy as np
        except ImportError as exc:
            raise TranscriptionError(
                "Chunked Sarvam mode requires av and numpy (already included with faster-whisper installs)."
            ) from exc

        sample_rate = 16000
        chunk_seconds = max(10.0, float(self.config.sarvam_chunk_seconds))
        chunk_samples = int(chunk_seconds * sample_rate)

        try:
            container = av.open(str(audio_path))
        except Exception as exc:
            raise TranscriptionError(f"Failed to decode audio for Sarvam chunking: {exc}") from exc

        audio_streams = [s for s in container.streams if s.type == "audio"]
        if not audio_streams:
            raise TranscriptionError("No audio stream found for Sarvam chunking.")

        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        pcm_parts = []
        for frame in container.decode(audio_streams[0]):
            resampled = resampler.resample(frame)
            frames = resampled if isinstance(resampled, list) else [resampled]
            for item in frames:
                arr = item.to_ndarray()
                pcm_parts.append(arr.reshape(-1))

        if not pcm_parts:
            raise TranscriptionError("Audio decode succeeded but produced no PCM data for Sarvam chunking.")

        pcm = np.concatenate(pcm_parts).astype(np.int16)
        total_duration = len(pcm) / sample_rate
        print(
            f"[Pipeline] Sarvam long-audio mode: splitting {total_duration:.1f}s into "
            f"{chunk_seconds:.1f}s chunks..."
        )

        merged_segments: list[TranscriptSegment] = []
        merged_text_parts: list[str] = []
        detected_language: str | None = None

        with tempfile.TemporaryDirectory(prefix="sarvam_chunks_") as temp_dir:
            temp_root = Path(temp_dir)
            chunk_index = 0
            for start_sample in range(0, len(pcm), chunk_samples):
                end_sample = min(start_sample + chunk_samples, len(pcm))
                chunk_pcm = pcm[start_sample:end_sample]
                if len(chunk_pcm) == 0:
                    continue

                chunk_path = temp_root / f"chunk_{chunk_index:03d}.wav"
                with wave.open(str(chunk_path), "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(sample_rate)
                    wav_file.writeframes(chunk_pcm.tobytes())

                chunk_response = self._sarvam_request(
                    audio_path=chunk_path,
                    mime_type="audio/wav",
                    model_name=model_name,
                    with_diarization=False,
                )
                if chunk_response.status_code != 200:
                    raise TranscriptionError(
                        "Sarvam chunked transcription failed with status "
                        f"{chunk_response.status_code}: {chunk_response.text[:300]}"
                    )

                payload = chunk_response.json()
                chunk_text, chunk_language, chunk_segments = self._parse_sarvam_payload(payload)

                if chunk_text:
                    merged_text_parts.append(chunk_text)
                if not detected_language and chunk_language:
                    detected_language = chunk_language

                offset = start_sample / sample_rate
                if chunk_segments:
                    for segment in chunk_segments:
                        segment.start += offset
                        segment.end += offset
                        merged_segments.append(segment)
                elif chunk_text:
                    merged_segments.append(
                        TranscriptSegment(
                            start=offset,
                            end=(end_sample / sample_rate),
                            text=chunk_text,
                            speaker=None,
                        )
                    )

                chunk_index += 1

        transcript_text = " ".join(merged_text_parts).strip()
        if not transcript_text and merged_segments:
            transcript_text = " ".join(segment.text for segment in merged_segments).strip()
        if not transcript_text:
            raise TranscriptionError("Sarvam chunked transcription produced no text.")

        merged_segments = self._ensure_speaker_labels(merged_segments)
        return TranscriptData(
            text=transcript_text,
            language=detected_language,
            duration_seconds=total_duration,
            segments=merged_segments,
        )

    def _transcribe_sarvam_batch_diarized(self, audio_path: str | Path) -> TranscriptData:
        """Run diarized batch transcription with Sarvam's official SDK.

        Uses documented job flow:
        create_job -> upload_files -> start -> wait_until_complete -> download_outputs
        """
        try:
            from sarvamai import SarvamAI
        except ImportError as exc:
            raise TranscriptionError(
                "sarvamai SDK is not installed. Install it with: pip install sarvamai"
            ) from exc

        audio_path = Path(audio_path)
        print("[Pipeline] Trying Sarvam batch diarization flow (SDK)...")

        client = SarvamAI(api_subscription_key=self.config.sarvam_api_key or "")

        requested_model = (self.config.sarvam_model or "").strip()
        if not requested_model:
            raise TranscriptionError("Sarvam batch diarization requires PERSONA_SARVAM_MODEL to be set.")
        create_job = client.speech_to_text_translate_job.create_job
        try:
            # Newer SDK variants may support `mode`; older versions reject it.
            job = create_job(
                model=requested_model,
                mode=self.config.sarvam_mode,
                with_diarization=True,
            )
        except TypeError as exc:
            if "unexpected keyword argument 'mode'" not in str(exc):
                raise TranscriptionError(f"Sarvam batch create_job failed: {exc}") from exc
            try:
                job = create_job(
                    model=requested_model,
                    with_diarization=True,
                )
            except Exception as inner_exc:
                raise TranscriptionError(f"Sarvam batch create_job failed: {inner_exc}") from inner_exc
        except Exception as exc:
            raise TranscriptionError(f"Sarvam batch create_job failed: {exc}") from exc

        timeout_seconds = max(60, int(self.config.sarvam_batch_timeout_seconds))

        with tempfile.TemporaryDirectory(prefix="sarvam_batch_job_") as temp_dir:
            output_dir = Path(temp_dir) / "outputs"
            output_dir.mkdir(parents=True, exist_ok=True)

            try:
                job.upload_files(file_paths=[str(audio_path)], timeout=timeout_seconds)
                job.start()
                job.wait_until_complete()
            except Exception as exc:
                raise TranscriptionError(f"Sarvam batch job execution failed: {exc}") from exc

            try:
                if hasattr(job, "is_failed") and job.is_failed():
                    raise TranscriptionError("Sarvam batch job reported failed status.")
            except Exception as exc:
                raise TranscriptionError(f"Sarvam batch failed status check: {exc}") from exc

            try:
                job.download_outputs(output_dir=str(output_dir))
            except Exception as exc:
                raise TranscriptionError(f"Sarvam batch output download failed: {exc}") from exc

            json_files = sorted(output_dir.glob("*.json"))
            if not json_files:
                raise TranscriptionError("Sarvam batch completed but returned no JSON output files.")

            # Merge all downloaded output JSONs in chronological order.
            merged_segments: list[TranscriptSegment] = []
            merged_text_parts: list[str] = []
            language: str | None = None
            max_end: float | None = None

            for json_file in json_files:
                try:
                    payload = json.loads(json_file.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise TranscriptionError(f"Failed to parse Sarvam batch output {json_file.name}: {exc}") from exc

                transcript_text, detected_language, segments = self._parse_sarvam_payload(payload)

                # Support cookbook field names under diarized_transcript.entries
                if not segments:
                    diarized = payload.get("diarized_transcript")
                    if isinstance(diarized, dict):
                        entries = diarized.get("entries") or []
                        for entry in entries:
                            text = (entry.get("transcript") or "").strip()
                            if not text:
                                continue
                            speaker = (
                                entry.get("speaker")
                                or entry.get("speaker_id")
                                or entry.get("speaker_label")
                                or ""
                            )
                            start = entry.get("start_time_seconds")
                            end = entry.get("end_time_seconds")
                            segments.append(
                                TranscriptSegment(
                                    start=float(start or 0.0),
                                    end=float(end or 0.0),
                                    text=text,
                                    speaker=str(speaker).strip() or None,
                                )
                            )

                if transcript_text:
                    merged_text_parts.append(transcript_text)
                if not language and detected_language:
                    language = detected_language

                for segment in segments:
                    merged_segments.append(segment)
                    if max_end is None or segment.end > max_end:
                        max_end = segment.end

            if not merged_text_parts and merged_segments:
                merged_text_parts = [segment.text for segment in merged_segments]

            transcript_text = " ".join(part for part in merged_text_parts if part).strip()
            if not transcript_text:
                raise TranscriptionError("Sarvam batch diarization completed but returned no text.")

            merged_segments = self._ensure_speaker_labels(merged_segments)
            return TranscriptData(
                text=transcript_text,
                language=language,
                duration_seconds=max_end,
                segments=merged_segments,
            )

    def _transcribe_sarvam(self, audio_path: str | Path) -> TranscriptData:
        """Transcribe audio with Sarvam using diarization-friendly output parsing."""
        try:
            import requests
        except ImportError as exc:
            raise TranscriptionError(
                "The requests package is not installed. Install it with: pip install requests"
            ) from exc

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

        model_name = self.config.sarvam_model
        diarization_enabled = bool(self.config.sarvam_enable_diarization)
        print(
            f"[Pipeline] Transcribing with Sarvam ({model_name}) "
            f"mode={self.config.sarvam_mode} diarization={diarization_enabled}..."
        )
        response = self._sarvam_request(audio_path, mime_type, model_name, diarization_enabled)

        # Auto-recover from deprecation by retrying with Sarvam's recommended model.
        if response.status_code == 400 and "deprecated" in response.text.lower():
            next_model = _extract_recommended_sarvam_model(response.text)
            if next_model and next_model != model_name:
                model_name = next_model
                print(f"[Pipeline] Sarvam model deprecated. Retrying with {model_name}...")
                response = self._sarvam_request(audio_path, mime_type, model_name, diarization_enabled)

        # Auto-recover when selected Sarvam mode/endpoint doesn't support diarization.
        if response.status_code == 400 and diarization_enabled:
            try:
                error_payload = response.json()
            except Exception:
                error_payload = {}
            message = _extract_sarvam_error_message(error_payload).lower()
            if "diarization" in message and "not supported" in message:
                diarization_enabled = False
                print("[Pipeline] Sarvam diarization is not supported for this mode/endpoint. Retrying without diarization...")
                response = self._sarvam_request(audio_path, mime_type, model_name, diarization_enabled)

        # Auto-recover when real-time endpoint enforces short audio limits.
        if response.status_code == 400 and "maximum limit of 30 seconds" in response.text.lower():
            return self._transcribe_sarvam_chunked(audio_path, model_name)

        if response.status_code != 200:
            raise TranscriptionError(
                f"Sarvam transcription failed with status {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        transcript_text, language, segments = self._parse_sarvam_payload(data)

        if not transcript_text and segments:
            transcript_text = " ".join(segment.text for segment in segments).strip()
        if not transcript_text:
            raise TranscriptionError("Sarvam transcription completed but returned no text.")

        # Preserve diarization labels when provided; synthesize only when missing.
        segments = self._ensure_speaker_labels(segments)

        normalized_data = self._sarvam_normalize_data(data)
        duration = float(normalized_data.get("duration_seconds") or 0) or (
            segments[-1].end if segments else None
        )

        return TranscriptData(
            text=transcript_text,
            language=language,
            duration_seconds=duration,
            segments=segments,
        )

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