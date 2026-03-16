from __future__ import annotations

import json
from pathlib import Path

from .models import ChatMessage, MeetingReport, TranscriptData


def _format_seconds(total_seconds: float | None) -> str:
    if total_seconds is None:
        return "Unknown"
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def write_json(path: Path, payload: dict | list) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def render_transcript_markdown(transcript: TranscriptData, chat_messages: list[ChatMessage], metadata: dict) -> str:
    lines = [
        "# Meeting Transcript",
        "",
        f"- Meeting URL: {metadata.get('meet_url', 'Unknown')}",
        f"- Bot Name: {metadata.get('bot_name', 'Unknown')}",
        f"- Language: {transcript.language or 'Unknown'}",
        f"- Duration: {_format_seconds(transcript.duration_seconds)}",
        "",
        "## Audio Transcript",
        "",
    ]

    if transcript.segments:
        for segment in transcript.segments:
            speaker = segment.speaker or "Unknown Speaker"
            lines.append(
                f"- [{_format_seconds(segment.start)} - {_format_seconds(segment.end)}] {speaker}: {segment.text}"
            )
    else:
        lines.append(transcript.text or "No transcript text available.")

    lines.extend(["", "## Chat Messages", ""])
    if chat_messages:
        for message in chat_messages:
            author = message.author or "Unknown Author"
            lines.append(f"- [{_format_seconds(message.relative_seconds)}] {author}: {message.text}")
    else:
        lines.append("- No chat messages captured.")

    return "\n".join(lines) + "\n"


def render_report_markdown(report: MeetingReport, metadata: dict) -> str:
    lines = [
        "# Meeting Report",
        "",
        "## Metadata",
        "",
        f"- Meeting URL: {metadata.get('meet_url', 'Unknown')}",
        f"- Bot Name: {metadata.get('bot_name', 'Unknown')}",
        f"- Recording Path: {metadata.get('recording_path', 'Unknown')}",
        f"- Chat Messages Captured: {metadata.get('chat_count', 0)}",
        f"- Transcript Language: {report.transcript_language or 'Unknown'}",
        "",
        "## Important Highlights",
        "",
    ]

    if report.important_highlights:
        lines.extend(f"- {item}" for item in report.important_highlights)
    else:
        lines.append("- No highlights extracted.")

    lines.extend(["", "## Chronological Summary", ""])
    if report.chronological_summary:
        lines.extend(f"- {item}" for item in report.chronological_summary)
    else:
        lines.append("- No chronological summary available.")

    lines.extend(["", "## Speaker Highlights", ""])
    if report.speaker_highlights:
        for speaker in report.speaker_highlights:
            lines.append(f"### {speaker.speaker}")
            lines.append("")
            lines.extend(f"- {item}" for item in speaker.highlights)
            lines.append("")
    else:
        lines.append("- Speaker-specific highlights were not available.")

    lines.extend(["", "## Decisions", ""])
    if report.decisions:
        for decision in report.decisions:
            line = f"- {decision.decision}"
            if decision.timestamp:
                line += f" ({decision.timestamp})"
            lines.append(line)
            if decision.evidence:
                lines.append(f"  Evidence: {decision.evidence}")
    else:
        lines.append("- No decisions were extracted.")

    lines.extend(["", "## Action Items", ""])
    if report.action_items:
        for item in report.action_items:
            details = []
            if item.owner:
                details.append(f"Owner: {item.owner}")
            if item.deadline:
                details.append(f"Deadline: {item.deadline}")
            if item.timestamp:
                details.append(f"Time: {item.timestamp}")
            detail_text = f" ({'; '.join(details)})" if details else ""
            lines.append(f"- {item.task}{detail_text}")
            if item.evidence:
                lines.append(f"  Evidence: {item.evidence}")
    else:
        lines.append("- No action items were extracted.")

    lines.extend(["", "## Key Timestamps", ""])
    if report.key_timestamps:
        lines.extend(f"- {item}" for item in report.key_timestamps)
    else:
        lines.append("- No key timestamps extracted.")

    if report.summary_note:
        lines.extend(["", "## Notes", "", f"- {report.summary_note}"])

    return "\n".join(lines) + "\n"