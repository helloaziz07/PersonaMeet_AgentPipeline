"""
Overlap-based speaker name attribution engine.

Takes a diarized transcript (with anonymous SPEAKER_00 / SPEAKER_01 labels)
and a list of Google Meet active-speaker intervals captured by the in-page JS
tracker, then assigns real participant names to each segment by finding which
speaker interval overlaps most with the segment's time span.

Confidence  = total_overlap_ms / segment_duration_ms
Segments below min_confidence threshold keep their diarization label but are
marked as "diarization-only" in speaker_source.
"""
from __future__ import annotations


from .models import SpeakerEventInterval, TranscriptData


class SpeakerAttributionEngine:
    """Assigns real names to diarized segments via overlap scoring."""

    _UNATTRIBUTED = "Unattributed Speaker"

    def __init__(self, min_confidence: float = 0.45) -> None:
        self.min_confidence = max(0.0, min(1.0, min_confidence))

    # ------------------------------------------------------------------
    def attribute(
        self,
        transcript: TranscriptData,
        speaker_events: list[dict],
    ) -> TranscriptData:
        """
        Attribute speaker names to transcript segments.

        Args:
            transcript:     TranscriptData whose segments carry diarization labels.
            speaker_events: Raw event dicts from JS stopSpeakerTracking().
                            Each dict: {speaker, start_ms, end_ms, source, confidence}

        Returns:
            The same TranscriptData object with segment.speaker,
            segment.speaker_source, and segment.speaker_confidence updated.
        """
        if not transcript.segments or not speaker_events:
            return transcript

        intervals = self._parse_events(speaker_events)
        if not intervals:
            return transcript

        for segment in transcript.segments:
            seg_start_ms = segment.start * 1000.0
            seg_end_ms = segment.end * 1000.0
            seg_duration_ms = seg_end_ms - seg_start_ms
            if seg_duration_ms <= 0:
                continue

            best_name, best_frac, best_source = self._best_overlap(
                seg_start_ms, seg_end_ms, seg_duration_ms, intervals
            )

            if best_name and best_frac >= self.min_confidence:
                segment.speaker = best_name
                segment.speaker_source = best_source
                segment.speaker_confidence = round(best_frac, 3)
            else:
                # Keep diarization label unchanged; mark as unconfirmed.
                segment.speaker_source = "diarization-only"
                segment.speaker_confidence = round(best_frac, 3) if best_frac > 0 else None

        return transcript

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_events(raw: list[dict]) -> list[SpeakerEventInterval]:
        intervals: list[SpeakerEventInterval] = []
        for ev in raw:
            try:
                name = str(ev.get("speaker") or "").strip()
                if not name:
                    continue
                start_ms = float(ev.get("start_ms", 0))
                end_ms = float(ev.get("end_ms", 0))
                if end_ms <= start_ms:
                    continue
                intervals.append(
                    SpeakerEventInterval(
                        speaker=name,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        source=str(ev.get("source", "unknown")),
                        confidence=float(ev.get("confidence", 0.5)),
                    )
                )
            except (TypeError, ValueError):
                continue
        return intervals

    @staticmethod
    def _best_overlap(
        seg_start: float,
        seg_end: float,
        seg_dur: float,
        intervals: list[SpeakerEventInterval],
    ) -> tuple[str | None, float, str]:
        """Return (best_speaker_name, overlap_fraction, source_tag)."""
        overlap_per_speaker: dict[str, float] = {}
        conf_per_speaker: dict[str, float] = {}
        source_per_speaker: dict[str, str] = {}

        for iv in intervals:
            ov_start = max(seg_start, iv.start_ms)
            ov_end = min(seg_end, iv.end_ms)
            ov_ms = ov_end - ov_start
            if ov_ms <= 0:
                continue
            nm = iv.speaker
            overlap_per_speaker[nm] = overlap_per_speaker.get(nm, 0.0) + ov_ms
            if iv.confidence > conf_per_speaker.get(nm, -1.0):
                conf_per_speaker[nm] = iv.confidence
                source_per_speaker[nm] = iv.source

        if not overlap_per_speaker:
            return None, 0.0, "no-overlap"

        best = max(overlap_per_speaker, key=lambda k: overlap_per_speaker[k])
        frac = overlap_per_speaker[best] / seg_dur
        return best, frac, source_per_speaker.get(best, "overlap")
