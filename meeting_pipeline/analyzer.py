from __future__ import annotations

import json
import re
from collections import defaultdict

from .config import PipelineConfig
from .models import ActionItem, ChatMessage, DecisionItem, MeetingReport, SpeakerHighlight, TranscriptData


# ── Rule-based pattern library ──────────────────────────────────────────────

# Phrases that strongly signal a decision was made
_DECISION_PATTERNS = re.compile(
    r"\b("
    r"we('re| are| will| have)? (decided|agreed|going|finalizing|finalised|finalize|approved|confirmed|chosen|picking|going with|sticking with)"
    r"|let'?s (go with|finalize|confirm|use|keep|stick with)"
    r"|decided to"
    r"|final decision"
    r"|we('l?l)? go with"
    r"|approved"
    r"|confirmed"
    r"|agreed (to|on|that)"
    r"|that'?s (decided|confirmed|final|settled|approved)"
    r"|we'?re going with"
    r"|the decision is"
    r"|we chose"
    r")",
    re.IGNORECASE,
)

# Phrases that signal an action item assignment
_ACTION_PATTERNS = re.compile(
    r"\b("
    r"will (take care of|handle|do|fix|send|write|review|update|check|create|build|test|deploy|push|merge|set up|set up|share|prepare|finish|complete|make sure|follow up|look into)"
    r"|needs? to"
    r"|should (send|write|review|update|check|create|build|test|deploy|push|fix|handle|prepare|finish|complete|follow up|look into)"
    r"|is responsible for"
    r"|action item"
    r"|assigned to"
    r"|take care of"
    r"|follow up on"
    r"|please (send|write|review|update|check|create|build|test|deploy|push|fix|handle|prepare|finish|complete)"
    r")",
    re.IGNORECASE,
)

# Deadline time words
_DEADLINE_PATTERN = re.compile(
    r"\b(by |before |until )(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|today|tomorrow|eod|eow|end of (day|week|month)"
    r"|next week|this week|[0-9]{1,2}[/-][0-9]{1,2}|[0-9]+th|[0-9]+st|[0-9]+rd|[0-9]+nd)",
    re.IGNORECASE,
)

# Common English first names + pronoun heuristic for owner extraction
_OWNER_PATTERN = re.compile(
    r"(?:^|\s)([A-Z][a-z]{2,15})(?:\s[A-Z][a-z]{2,15})?(?=\s+(will|should|needs? to|is responsible|please|can you|could you))"
)


class AnalysisError(RuntimeError):
    pass


def _format_seconds(total_seconds: float | None) -> str:
    if total_seconds is None:
        return "Unknown"
    total_seconds = max(0, int(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _safe_json_loads(payload: str) -> dict:
    text = payload.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.replace("json\n", "", 1)
    return json.loads(text)


class MeetingAnalyzer:
    def __init__(self, config: PipelineConfig):
        self.config = config
        self.last_backend: str | None = None

    def analyze(self, transcript: TranscriptData, chat_messages: list[ChatMessage], metadata: dict) -> MeetingReport:
        """Analyze meeting — tries OpenAI, then Gemini, then rule-based fallback."""
        if self.config.openai_api_key:
            try:
                report = self._analyze_openai(transcript, chat_messages, metadata)
                self.last_backend = "openai"
                return report
            except Exception as exc:
                print(f"[Pipeline] OpenAI analysis failed ({exc}). Trying Gemini...")
        if self.config.gemini_api_key:
            try:
                report = self._analyze_gemini(transcript, chat_messages, metadata)
                self.last_backend = "gemini"
                return report
            except Exception as exc:
                print(f"[Pipeline] Gemini analysis failed ({exc}). Using rule-based fallback...")
        self.last_backend = "rule-based"
        return self._fallback_report(transcript, chat_messages, metadata)

    @staticmethod
    def _normalize_text(text: str, max_len: int = 220) -> str:
        cleaned = re.sub(r"\s+", " ", (text or "").strip())
        if not cleaned:
            return ""

        # Collapse obvious repeated short phrase loops (e.g. 'haan ji' repeated many times).
        words = cleaned.split()
        if len(words) >= 12:
            for n in (1, 2, 3):
                if len(words) < n * 6:
                    continue
                unit = words[:n]
                repeats = 0
                idx = 0
                while idx + n <= len(words) and words[idx:idx + n] == unit:
                    repeats += 1
                    idx += n
                if repeats >= 6:
                    phrase = " ".join(unit)
                    tail = " ".join(words[idx:])
                    cleaned = f"{phrase} (repeated {repeats} times)"
                    if tail:
                        cleaned += f". {tail}"
                    break

        if len(cleaned) > max_len:
            cleaned = cleaned[: max_len - 3].rstrip() + "..."
        return cleaned

    # ── OpenAI analysis backend ─────────────────────────────────────────

    def _analyze_openai(self, transcript: TranscriptData, chat_messages: list[ChatMessage], metadata: dict) -> MeetingReport:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AnalysisError("openai package not installed") from exc

        client = OpenAI(api_key=self.config.openai_api_key)
        unified_lines = self._build_unified_lines(transcript, chat_messages)
        chunk_payloads = self._chunk_lines(unified_lines)

        chunk_analyses = []
        for index, chunk in enumerate(chunk_payloads, start=1):
            response = client.chat.completions.create(
                model=self.config.summary_model,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You analyze meetings and extract structured information. "
                            "Return strict JSON only. Focus on facts from the provided chunk."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Meeting metadata: {json.dumps(metadata, ensure_ascii=True)}\n"
                            f"Chunk {index} of {len(chunk_payloads)}\n"
                            "Analyze the following unified meeting events and return JSON with keys: "
                            "chunk_summary (string), important_highlights (array of strings), "
                            "decisions (array of objects with decision,timestamp,evidence), "
                            "action_items (array of objects with task,owner,deadline,timestamp,evidence), "
                            "speaker_highlights (array of objects with speaker,highlights), "
                            "key_timestamps (array of strings).\n\n"
                            + chunk
                        ),
                    },
                ],
            )
            payload = response.choices[0].message.content or "{}"
            chunk_analyses.append(_safe_json_loads(payload))

        response = client.chat.completions.create(
            model=self.config.summary_model,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You consolidate meeting analysis into one final meeting report. "
                        "Return strict JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Meeting metadata: {json.dumps(metadata, ensure_ascii=True)}\n"
                        "Combine these chunk analyses into one final JSON object with keys: "
                        "important_highlights (array of strings), chronological_summary (array of strings), "
                        "speaker_highlights (array of objects with speaker,highlights), "
                        "decisions (array of objects with decision,timestamp,evidence), "
                        "action_items (array of objects with task,owner,deadline,timestamp,evidence), "
                        "key_timestamps (array of strings), summary_note (string).\n\n"
                        + json.dumps(chunk_analyses, ensure_ascii=True)
                    ),
                },
            ],
        )
        final_payload = response.choices[0].message.content or "{}"
        return self._report_from_payload(_safe_json_loads(final_payload), transcript.language)

    # ── Gemini analysis backend ─────────────────────────────────────────

    def _analyze_gemini(self, transcript: TranscriptData, chat_messages: list[ChatMessage], metadata: dict) -> MeetingReport:
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise AnalysisError(
                "google-generativeai package not installed. Run: pip install google-generativeai"
            ) from exc

        genai.configure(api_key=self.config.gemini_api_key)
        model = genai.GenerativeModel(self.config.gemini_model)

        unified_lines = self._build_unified_lines(transcript, chat_messages)
        unified_text = "\n".join(unified_lines)

        prompt = (
            f"Meeting metadata: {json.dumps(metadata, ensure_ascii=False)}\n\n"
            "Analyze this meeting transcript. It may be in Hindi, Marathi, English, or a mix. "
            "Write ALL output (highlights, summaries, decisions, action items) in English. "
            "Return ONLY a valid JSON object with these exact keys:\n"
            "  important_highlights (array of strings),\n"
            "  chronological_summary (array of strings with [MM:SS] timestamps),\n"
            "  speaker_highlights (array of {speaker, highlights[]}),\n"
            "  decisions (array of {decision, timestamp, evidence}),\n"
            "  action_items (array of {task, owner, deadline, timestamp, evidence}),\n"
            "  key_timestamps (array of strings),\n"
            "  summary_note (string)\n\n"
            "Meeting transcript:\n"
            + unified_text
        )

        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )

        payload = _safe_json_loads(response.text or "{}")
        return self._report_from_payload(payload, transcript.language)

    def _build_unified_lines(self, transcript: TranscriptData, chat_messages: list[ChatMessage]) -> list[str]:
        lines = []
        for segment in transcript.segments:
            speaker = segment.speaker or "Unknown Speaker"
            lines.append(
                f"[{_format_seconds(segment.start)}][audio][{speaker}] {segment.text}"
            )

        for message in chat_messages:
            lines.append(
                f"[{_format_seconds(message.relative_seconds)}][chat][{message.author or 'Unknown Author'}] {message.text}"
            )

        if not lines and transcript.text:
            lines.append(f"[00:00][audio][Unknown Speaker] {transcript.text}")

        return sorted(lines)

    def _chunk_lines(self, lines: list[str]) -> list[str]:
        if not lines:
            return [""]

        chunks = []
        current_chunk = []
        current_length = 0

        for line in lines:
            if current_chunk and current_length + len(line) + 1 > self.config.max_chunk_chars:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_length = 0
            current_chunk.append(line)
            current_length += len(line) + 1

        if current_chunk:
            chunks.append("\n".join(current_chunk))

        return chunks

    def _report_from_payload(self, payload: dict, language: str | None) -> MeetingReport:
        speaker_highlights = []
        for entry in payload.get("speaker_highlights", []):
            speaker = (entry.get("speaker") or "Unknown Speaker").strip()
            highlights = [item.strip() for item in entry.get("highlights", []) if str(item).strip()]
            if highlights:
                speaker_highlights.append(SpeakerHighlight(speaker=speaker, highlights=highlights))

        decisions = []
        for entry in payload.get("decisions", []):
            decision_text = (entry.get("decision") or "").strip()
            if decision_text:
                decisions.append(
                    DecisionItem(
                        decision=decision_text,
                        timestamp=(entry.get("timestamp") or "").strip() or None,
                        evidence=(entry.get("evidence") or "").strip() or None,
                    )
                )

        action_items = []
        for entry in payload.get("action_items", []):
            task_text = (entry.get("task") or "").strip()
            if task_text:
                action_items.append(
                    ActionItem(
                        task=task_text,
                        owner=(entry.get("owner") or "").strip() or None,
                        deadline=(entry.get("deadline") or "").strip() or None,
                        timestamp=(entry.get("timestamp") or "").strip() or None,
                        evidence=(entry.get("evidence") or "").strip() or None,
                    )
                )

        return MeetingReport(
            important_highlights=[item.strip() for item in payload.get("important_highlights", []) if str(item).strip()],
            chronological_summary=[item.strip() for item in payload.get("chronological_summary", []) if str(item).strip()],
            speaker_highlights=speaker_highlights,
            decisions=decisions,
            action_items=action_items,
            key_timestamps=[item.strip() for item in payload.get("key_timestamps", []) if str(item).strip()],
            transcript_language=language,
            summary_note=(payload.get("summary_note") or "").strip() or None,
        )

    def _fallback_report(self, transcript: TranscriptData, chat_messages: list[ChatMessage], metadata: dict) -> MeetingReport:
        """Rule-based extraction — runs offline with no API key required."""

        # Combine all text lines with timestamps
        all_lines: list[tuple[float | None, str, str]] = []  # (start, source, text)
        for segment in transcript.segments:
            normalized = self._normalize_text(segment.text)
            if normalized:
                all_lines.append((segment.start, "audio", normalized))
        for message in chat_messages:
            normalized = self._normalize_text(message.text)
            if normalized:
                all_lines.append((message.relative_seconds, "chat", normalized))
        all_lines.sort(key=lambda item: item[0] if item[0] is not None else 0)

        if not all_lines and transcript.text:
            all_lines = [
                (None, "audio", self._normalize_text(part))
                for part in transcript.text.split(".")
                if self._normalize_text(part)
            ]

        # ── Chronological summary (first 20 meaningful lines) ──────────
        chronological_summary = [
            f"[{_format_seconds(ts)}] {text}" if ts is not None else text
            for ts, _source, text in all_lines[:12]
        ]

        # ── Speaker highlights ──────────────────────────────────────────
        speaker_map: dict[str, list[str]] = defaultdict(list)
        for segment in transcript.segments:
            normalized = self._normalize_text(segment.text)
            if normalized:
                speaker = segment.speaker or "Unknown Speaker"
                if len(speaker_map[speaker]) < 5:
                    if normalized not in speaker_map[speaker]:
                        speaker_map[speaker].append(normalized)
        speaker_highlights = [
            SpeakerHighlight(speaker=spk, highlights=items)
            for spk, items in speaker_map.items()
        ]

        # ── Decision detection ──────────────────────────────────────────
        decisions: list[DecisionItem] = []
        seen_decisions: set[str] = set()
        for ts, _source, text in all_lines:
            if _DECISION_PATTERNS.search(text):
                key = text[:80].lower()
                if key not in seen_decisions:
                    seen_decisions.add(key)
                    decisions.append(
                        DecisionItem(
                            decision=text,
                            timestamp=_format_seconds(ts) if ts is not None else None,
                            evidence=None,
                        )
                    )

        # ── Action item extraction ──────────────────────────────────────
        action_items: list[ActionItem] = []
        seen_tasks: set[str] = set()
        for ts, _source, text in all_lines:
            if _ACTION_PATTERNS.search(text):
                key = text[:80].lower()
                if key in seen_tasks:
                    continue
                seen_tasks.add(key)

                # Try to extract owner
                owner: str | None = None
                owner_match = _OWNER_PATTERN.search(text)
                if owner_match:
                    owner = owner_match.group(1)

                # Try to extract deadline
                deadline: str | None = None
                deadline_match = _DEADLINE_PATTERN.search(text)
                if deadline_match:
                    deadline = deadline_match.group(0).strip()

                action_items.append(
                    ActionItem(
                        task=text,
                        owner=owner,
                        deadline=deadline,
                        timestamp=_format_seconds(ts) if ts is not None else None,
                        evidence=None,
                    )
                )

        # ── Important highlights: decisions > action items > top lines ──
        important_highlights: list[str] = []
        for d in decisions[:3]:
            important_highlights.append(f"Decision: {d.decision}")
        for a in action_items[:3]:
            owner_part = f" (Owner: {a.owner})" if a.owner else ""
            deadline_part = f" (Deadline: {a.deadline})" if a.deadline else ""
            important_highlights.append(f"Action: {a.task}{owner_part}{deadline_part}")
        if not important_highlights:
            important_highlights = [text for _ts, _src, text in all_lines[:5]]
        if chat_messages:
            important_highlights.append(f"{len(chat_messages)} chat message(s) captured during the meeting.")

        # De-duplicate highlights while preserving order.
        dedup_highlights: list[str] = []
        seen_highlights: set[str] = set()
        for item in important_highlights:
            key = item.lower().strip()
            if not key or key in seen_highlights:
                continue
            seen_highlights.add(key)
            dedup_highlights.append(item)
        important_highlights = dedup_highlights[:8]

        # ── Key timestamps: first 8 segment starts ───────────────────────
        key_timestamps = [
            f"[{_format_seconds(segment.start)}] {self._normalize_text(segment.text, max_len=100)}"
            for segment in transcript.segments[:8]
            if segment.text.strip()
        ]

        transcription_backend = (metadata.get("transcription_backend") or "unknown").strip()
        analysis_backend = (metadata.get("analysis_backend") or "rule-based").strip()

        summary_note = (
            f"Transcription backend: {transcription_backend}. "
            f"Analysis backend: {analysis_backend}. "
            "This report used deterministic rule-based extraction, so decisions/action items may be conservative. "
            "Set OPENAI_API_KEY or GEMINI_API_KEY for richer, LLM-generated summaries."
        )

        return MeetingReport(
            important_highlights=important_highlights,
            chronological_summary=chronological_summary,
            speaker_highlights=speaker_highlights,
            decisions=decisions,
            action_items=action_items,
            key_timestamps=key_timestamps,
            transcript_language=transcript.language,
            summary_note=summary_note,
        )