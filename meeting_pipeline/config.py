import os
from dataclasses import dataclass
from pathlib import Path


def _load_local_env_file() -> None:
    """Load key=value pairs from project .env into os.environ if not already set."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return

    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            # Remove optional wrapping quotes.
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]

            # Ignore inline comments for unquoted values.
            if "#" in value and not any(q in raw_line for q in ['"', "'"]):
                value = value.split("#", 1)[0].strip()

            os.environ.setdefault(key, value)
    except Exception:
        # Non-fatal: runtime can still rely on shell-exported environment variables.
        return


# Ensure .env is loaded before dataclass defaults read environment variables.
_load_local_env_file()


@dataclass(slots=True)
class PipelineConfig:
    base_dir: Path
    transcript_filename: str = "transcript.json"
    transcript_markdown_filename: str = "transcript.md"
    report_filename: str = "meeting_report.md"
    analysis_filename: str = "meeting_analysis.json"
    raw_chat_filename: str = "chat_messages.json"
    transcription_model: str = os.getenv("PERSONA_TRANSCRIPTION_MODEL", "whisper-1")
    summary_model: str = os.getenv("PERSONA_SUMMARY_MODEL", "gpt-4o-mini")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")
    gemini_model: str = os.getenv("PERSONA_GEMINI_MODEL", "gemini-1.5-flash")
    # Sarvam AI is recommended for Hindi/Marathi/English with diarization.
    sarvam_api_key: str | None = os.getenv("SARVAM_API_KEY")
    sarvam_model: str = os.getenv("PERSONA_SARVAM_MODEL", "saaras:v3")
    sarvam_mode: str = os.getenv("PERSONA_SARVAM_MODE", "transcribe")
    sarvam_enable_diarization: bool = (
        os.getenv("PERSONA_SARVAM_ENABLE_DIARIZATION", "false").strip().lower() in {"1", "true", "yes", "on"}
    )
    sarvam_chunk_seconds: float = float(os.getenv("PERSONA_SARVAM_CHUNK_SECONDS", "25"))
    # Batch diarization path (preferred for multi-speaker attribution when supported by account/API).
    sarvam_use_batch_diarization: bool = (
        os.getenv("PERSONA_SARVAM_USE_BATCH_DIARIZATION", "true").strip().lower() in {"1", "true", "yes", "on"}
    )
    sarvam_batch_submit_url: str = os.getenv(
        "PERSONA_SARVAM_BATCH_SUBMIT_URL", "https://api.sarvam.ai/speech-to-text/batch"
    )
    sarvam_batch_status_url_template: str = os.getenv(
        "PERSONA_SARVAM_BATCH_STATUS_URL_TEMPLATE", "https://api.sarvam.ai/speech-to-text/batch/{job_id}"
    )
    sarvam_batch_poll_seconds: float = float(os.getenv("PERSONA_SARVAM_BATCH_POLL_SECONDS", "2.0"))
    sarvam_batch_timeout_seconds: int = int(os.getenv("PERSONA_SARVAM_BATCH_TIMEOUT_SECONDS", "900"))
    # "small" is the minimum recommended model for Hindi/Marathi.
    # Override with: export PERSONA_LOCAL_WHISPER_MODEL=medium  (best quality, slower)
    local_whisper_model: str = os.getenv("PERSONA_LOCAL_WHISPER_MODEL", "small")
    # Beam size 2 is a practical quality/speed balance for local CPU inference.
    # Increase to 5 for best quality, reduce to 1 for faster transcripts.
    local_whisper_beam_size: int = int(os.getenv("PERSONA_LOCAL_WHISPER_BEAM_SIZE", "2"))
    # Use all available CPU cores by default for local transcription.
    local_whisper_cpu_threads: int = int(
        os.getenv("PERSONA_LOCAL_WHISPER_CPU_THREADS", str(os.cpu_count() or 4))
    )
    # Synthetic speaker labeling when model does not provide diarization.
    synthetic_speaker_count: int = int(os.getenv("PERSONA_SYNTHETIC_SPEAKER_COUNT", "2"))
    speaker_turn_gap_seconds: float = float(os.getenv("PERSONA_SPEAKER_TURN_GAP_SECONDS", "1.6"))
    max_chunk_chars: int = 12000
    # ── Active-speaker tracking + overlap attribution ─────────────────────
    # Poll the Meet UI every PERSONA_MEET_SPEAKER_POLL_MS ms in-browser to
    # capture who is speaking.  Intervals shorter than STABILITY_MS are
    # discarded (avoids noise from brief focus transitions).
    active_speaker_tracking: bool = (
        os.getenv("PERSONA_MEET_ACTIVE_SPEAKER_TRACKING", "true").strip().lower() in {"1", "true", "yes", "on"}
    )
    speaker_poll_ms: int = int(os.getenv("PERSONA_MEET_SPEAKER_POLL_MS", "200"))
    speaker_stability_ms: int = int(os.getenv("PERSONA_MEET_SPEAKER_STABILITY_MS", "500"))
    # When True, the overlap engine replaces the first-seen-order name mapping.
    attribution_enabled: bool = (
        os.getenv("PERSONA_MEET_NAME_ATTRIBUTION_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    )
    # Segments whose best speaker-overlap fraction is below this threshold are
    # kept without a real name (speaker_source = "diarization-only").
    attribution_min_confidence: float = float(os.getenv("PERSONA_MEET_ATTRIBUTION_MIN_CONFIDENCE", "0.45"))

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)

    @property
    def transcript_path(self) -> Path:
        return self.base_dir / self.transcript_filename

    @property
    def transcript_markdown_path(self) -> Path:
        return self.base_dir / self.transcript_markdown_filename

    @property
    def report_path(self) -> Path:
        return self.base_dir / self.report_filename

    @property
    def analysis_path(self) -> Path:
        return self.base_dir / self.analysis_filename

    @property
    def raw_chat_path(self) -> Path:
        return self.base_dir / self.raw_chat_filename

    def ensure_dirs(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)