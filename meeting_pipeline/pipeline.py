from __future__ import annotations

import traceback

from .analyzer import MeetingAnalyzer
from .config import PipelineConfig
from .models import ChatMessage, TranscriptData
from .reporting import render_report_markdown, render_transcript_markdown, write_json
from .transcription import AudioTranscriber


class MeetingProcessingPipeline:
    def __init__(self, config: PipelineConfig):
        self.config = config
        self.config.ensure_dirs()
        self.transcriber = AudioTranscriber(config)
        self.analyzer = MeetingAnalyzer(config)

    @staticmethod
    def _apply_speaker_names(transcript: TranscriptData, participant_names: list[str]) -> TranscriptData:
        if not participant_names or not transcript.segments:
            return transcript

        normalized_names = [str(name).strip() for name in participant_names if str(name).strip()]
        if not normalized_names:
            return transcript

        # Preserve first-seen order of model speaker ids and map to participant names.
        speaker_order: list[str] = []
        for segment in transcript.segments:
            label = (segment.speaker or "").strip()
            if not label:
                continue
            if label not in speaker_order:
                speaker_order.append(label)

        # Avoid incorrect hard attribution: if we only have one detected speaker label
        # but multiple participants were present, keep generic labels instead of forcing
        # one participant name across all lines.
        if len(speaker_order) <= 1 and len(normalized_names) > 1:
            for segment in transcript.segments:
                if (segment.speaker or "").strip():
                    segment.speaker = "Unattributed Speaker"
            return transcript

        mapping: dict[str, str] = {}
        for idx, label in enumerate(speaker_order):
            if idx < len(normalized_names):
                mapping[label] = normalized_names[idx]

        if not mapping:
            return transcript

        for segment in transcript.segments:
            label = (segment.speaker or "").strip()
            if label in mapping:
                segment.speaker = mapping[label]

        return transcript

    def process(self, recording_path: str, chat_messages: list[dict] | None = None, metadata: dict | None = None) -> dict:
        metadata = metadata or {}
        metadata = dict(metadata)
        metadata["recording_path"] = str(recording_path)

        normalized_chat = [
            ChatMessage(
                text=(item.get("text") or "").strip(),
                author=(item.get("author") or "").strip() or None,
                relative_seconds=item.get("relative_seconds"),
                captured_at=item.get("captured_at"),
            )
            for item in (chat_messages or [])
            if (item.get("text") or "").strip()
        ]
        metadata["chat_count"] = len(normalized_chat)

        # Always persist chat capture first, even if transcription fails later.
        write_json(self.config.raw_chat_path, [message.to_dict() for message in normalized_chat])

        try:
            transcript = self.transcriber.transcribe(recording_path)
            metadata["transcription_backend"] = self.transcriber.last_backend or "unknown"
            participant_names = metadata.get("participant_names") or []
            if isinstance(participant_names, list):
                transcript = self._apply_speaker_names(transcript, participant_names)
            metadata["analysis_backend"] = (
                "openai" if self.config.openai_api_key else "gemini" if self.config.gemini_api_key else "rule-based"
            )
            report = self.analyzer.analyze(transcript, normalized_chat, metadata)
            metadata["analysis_backend"] = self.analyzer.last_backend or metadata["analysis_backend"]

            write_json(self.config.transcript_path, transcript.to_dict())
            write_json(self.config.analysis_path, report.to_dict())
            self.config.transcript_markdown_path.write_text(
                render_transcript_markdown(transcript, normalized_chat, metadata),
                encoding="utf-8",
            )
            self.config.report_path.write_text(
                render_report_markdown(report, metadata),
                encoding="utf-8",
            )

            return {
                "transcript_path": str(self.config.transcript_path),
                "transcript_markdown_path": str(self.config.transcript_markdown_path),
                "chat_path": str(self.config.raw_chat_path),
                "analysis_path": str(self.config.analysis_path),
                "report_path": str(self.config.report_path),
                "language": transcript.language,
                "duration_seconds": transcript.duration_seconds,
                "chat_count": len(normalized_chat),
            }
        except Exception as exc:
            error_path = self.config.base_dir / "pipeline_error.txt"
            error_report_path = self.config.report_path

            error_path.write_text(
                "Post-meeting pipeline failed.\n\n"
                f"Error: {exc}\n\n"
                "Traceback:\n"
                f"{traceback.format_exc()}\n",
                encoding="utf-8",
            )

            error_report_path.write_text(
                "# Meeting Report\n\n"
                "## Status\n\n"
                "- Report generation failed due to a post-processing error.\n"
                f"- Error: {exc}\n"
                f"- Check details in: {error_path.name}\n\n"
                "## Captured Chat Messages\n\n"
                + ("\n".join([f"- {(m.author or 'Unknown')}: {m.text}" for m in normalized_chat]) or "- No chat messages captured.")
                + "\n",
                encoding="utf-8",
            )

            return {
                "transcript_path": str(self.config.transcript_path),
                "transcript_markdown_path": str(self.config.transcript_markdown_path),
                "chat_path": str(self.config.raw_chat_path),
                "analysis_path": str(self.config.analysis_path),
                "report_path": str(self.config.report_path),
                "error_path": str(error_path),
                "language": None,
                "duration_seconds": None,
                "chat_count": len(normalized_chat),
                "status": "failed",
            }