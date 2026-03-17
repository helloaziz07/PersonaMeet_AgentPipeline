from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    speaker_source: str | None = None
    speaker_confidence: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class TranscriptData:
    text: str
    language: str | None = None
    duration_seconds: float | None = None
    segments: list[TranscriptSegment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "segments": [segment.to_dict() for segment in self.segments],
        }


@dataclass(slots=True)
class ChatMessage:
    text: str
    author: str | None = None
    relative_seconds: float | None = None
    captured_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class DecisionItem:
    decision: str
    timestamp: str | None = None
    evidence: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ActionItem:
    task: str
    owner: str | None = None
    deadline: str | None = None
    timestamp: str | None = None
    evidence: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class SpeakerHighlight:
    speaker: str
    highlights: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class MeetingReport:
    important_highlights: list[str] = field(default_factory=list)
    chronological_summary: list[str] = field(default_factory=list)
    speaker_highlights: list[SpeakerHighlight] = field(default_factory=list)
    decisions: list[DecisionItem] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    key_timestamps: list[str] = field(default_factory=list)
    transcript_language: str | None = None
    summary_note: str | None = None

    def to_dict(self) -> dict:
        return {
            "important_highlights": self.important_highlights,
            "chronological_summary": self.chronological_summary,
            "speaker_highlights": [item.to_dict() for item in self.speaker_highlights],
            "decisions": [item.to_dict() for item in self.decisions],
            "action_items": [item.to_dict() for item in self.action_items],
            "key_timestamps": self.key_timestamps,
            "transcript_language": self.transcript_language,
            "summary_note": self.summary_note,
        }


@dataclass(slots=True)
class SpeakerEventInterval:
    """A time interval during which a specific participant was the active speaker."""

    speaker: str
    start_ms: float
    end_ms: float
    source: str = "unknown"
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return asdict(self)