# PersonaMeet Agent Pipeline

PersonaMeet is a Google Meet assistant that automatically joins meetings, records audio, captures chat, and generates post-meeting intelligence:

- full transcript
- structured summary
- decisions
- action items
- speaker highlights

It is built for multilingual meetings (English, Hindi, Marathi, and mixed conversations) with provider fallback logic.

## What It Does

1. Joins a Google Meet using Playwright automation.
2. Records in-meeting audio to `.webm`.
3. Captures visible chat messages.
4. Runs a post-meeting pipeline:
	 - transcription
	 - analysis
	 - report generation
5. Writes JSON + Markdown artifacts per meeting session.

## Backend Fallback Strategy

### Transcription

1. OpenAI Whisper API (if `OPENAI_API_KEY` is available)
2. Gemini audio transcription (if `GEMINI_API_KEY` is available)
3. Local `faster-whisper` fallback

### Analysis and Report Generation

1. OpenAI chat model (if `OPENAI_API_KEY` is available)
2. Gemini model (if `GEMINI_API_KEY` is available)
3. Rule-based fallback extraction

### Speaker Labels

If direct speaker names are not available, the pipeline assigns synthetic labels like `Speaker 1`, `Speaker 2` using turn-based segmentation.

## Project Structure

```text
persona_meet/
	persona_meet_bot.py
	inject_scripts.py
	login_profile.py
	requirements.txt
	.env.example
	.gitignore
	scripts/
		setup.sh
		setup.ps1
	meeting_pipeline/
		__init__.py
		config.py
		models.py
		transcription.py
		analyzer.py
		reporting.py
		pipeline.py
```

## Quick Start

### Prerequisites

- Python 3.10+
- Git Bash or PowerShell
- Chromium (installed through Playwright step)

### Option A: One-command setup (recommended)

Git Bash:

```bash
bash scripts/setup.sh
```

PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

### Option B: Manual setup

```bash
pip install -r requirements.txt
playwright install chromium
```

Copy `.env.example` values into your environment.

Git Bash example:

```bash
export OPENAI_API_KEY=""
export GEMINI_API_KEY=""
export PERSONA_LOCAL_WHISPER_MODEL="small"
```

PowerShell example:

```powershell
$env:OPENAI_API_KEY=""
$env:GEMINI_API_KEY=""
$env:PERSONA_LOCAL_WHISPER_MODEL="small"
```

## Running The Bot

Anonymous mode:

```bash
python persona_meet_bot.py "https://meet.google.com/abc-defg-hij" --name "Meeting Agent"
```

Logged-in profile mode:

```bash
python login_profile.py
python persona_meet_bot.py "https://meet.google.com/abc-defg-hij" --profile user_login
```

## Output Artifacts

Each run creates a folder like:

`meeting-session-2026-03-16T10-30-00`

Inside it:

- `meeting-recording-*.webm`
- `chat_messages.json`
- `transcript.json`
- `transcript.md`
- `meeting_analysis.json`
- `meeting_report.md`

If anything fails in post-processing:

- `pipeline_error.txt`
- fallback `meeting_report.md` with error status

## Configuration

### API Keys

- `OPENAI_API_KEY`
- `GEMINI_API_KEY`

### Model Selection

- `PERSONA_TRANSCRIPTION_MODEL` (default: `whisper-1`)
- `PERSONA_SUMMARY_MODEL` (default: `gpt-4o-mini`)
- `PERSONA_GEMINI_MODEL` (default: `gemini-1.5-flash`)
- `PERSONA_LOCAL_WHISPER_MODEL` (default: `small`)

### Local Whisper Performance

- `PERSONA_LOCAL_WHISPER_BEAM_SIZE` (default: `2`)
- `PERSONA_LOCAL_WHISPER_CPU_THREADS` (default: CPU count)

### Speaker Segmentation

- `PERSONA_SYNTHETIC_SPEAKER_COUNT` (default: `2`)
- `PERSONA_SPEAKER_TURN_GAP_SECONDS` (default: `1.6`)

## Accuracy and Speed Notes

- Best quality for Hindi/Marathi: API backends (OpenAI/Gemini).
- Local transcription is fully offline but can be slower and less accurate for multilingual speech.
- `small` is a good local default. `medium` improves quality but increases runtime.

## Troubleshooting

- `429 insufficient_quota` from OpenAI:
	- Your OpenAI credits are exhausted.
	- Add credits or set `GEMINI_API_KEY` so pipeline falls back to Gemini.

- Very slow local transcription:
	- Use API backend keys, or tune local settings (`PERSONA_LOCAL_WHISPER_BEAM_SIZE`, `PERSONA_LOCAL_WHISPER_CPU_THREADS`).

- Report generated but content quality is poor:
	- Prefer API backends for multilingual meetings.
	- Ensure meeting audio is clearly captured (not muted/silent).

## Security

- Never commit real API keys.
- Keep secrets in environment variables or local `.env` only.
- If a key is leaked, revoke and rotate immediately.