import os
from dataclasses import dataclass
from pathlib import Path


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