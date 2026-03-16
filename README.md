# PersonaMeet

PersonaMeet joins a Google Meet, records meeting audio, captures chat messages, and generates post-meeting artifacts including transcript and meeting report.

## Features

- Join Google Meet automatically (anonymous or logged-in profile mode)
- Record meeting audio to `.webm`
- Capture in-meeting chat messages
- Transcription backend fallback chain:
	- OpenAI Whisper API (if `OPENAI_API_KEY` is set)
	- Gemini audio transcription (if `GEMINI_API_KEY` is set)
	- Local `faster-whisper` model (offline fallback)
- Meeting analysis/report backend fallback chain:
	- OpenAI chat model (if `OPENAI_API_KEY` is set)
	- Gemini model (if `GEMINI_API_KEY` is set)
	- Rule-based fallback
- Speaker tagging support (`Speaker 1`, `Speaker 2`, etc.) when direct names are unavailable

## Project Structure

```
persona_meet/
	persona_meet_bot.py
	inject_scripts.py
	login_profile.py
	requirements.txt
	meeting_pipeline/
		transcription.py
		analyzer.py
		reporting.py
		pipeline.py
		models.py
		config.py
```

## Prerequisites

- Python 3.10+
- Chromium (installed via Playwright)
- Windows/Linux/macOS terminal

## Setup

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

1. Install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
```

2. Configure environment variables:

Copy `.env.example` values into your shell environment (or your own `.env` workflow).

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

3. Optional: Save a logged-in Chrome profile for meetings requiring admission from an account:

```bash
python login_profile.py
```

## Publish To Existing Repo

If your local folder is already cloned from the target repository:

```bash
git add .
git commit -m "Add meeting pipeline, Gemini fallback, speaker labels, and setup scripts"
git push origin main
```

## Run

Anonymous mode:

```bash
python persona_meet_bot.py "https://meet.google.com/abc-defg-hij" --name "Meeting Agent"
```

Logged-in profile mode:

```bash
python persona_meet_bot.py "https://meet.google.com/abc-defg-hij" --profile user_login
```

## Output

Each run creates a session folder named like `meeting-session-2026-03-16T10-30-00` containing:

- `meeting-recording-*.webm`
- `chat_messages.json`
- `transcript.json`
- `transcript.md`
- `meeting_analysis.json`
- `meeting_report.md`

On pipeline failures, error artifacts are generated:

- `pipeline_error.txt`
- fallback `meeting_report.md` with error status

## Environment Variables

- `OPENAI_API_KEY`: enables OpenAI transcription + analysis
- `GEMINI_API_KEY`: enables Gemini transcription + analysis fallback
- `PERSONA_GEMINI_MODEL` (default: `gemini-1.5-flash`)
- `PERSONA_TRANSCRIPTION_MODEL` (default: `whisper-1`)
- `PERSONA_SUMMARY_MODEL` (default: `gpt-4o-mini`)
- `PERSONA_LOCAL_WHISPER_MODEL` (default: `small`)
- `PERSONA_LOCAL_WHISPER_BEAM_SIZE` (default: `2`)
- `PERSONA_LOCAL_WHISPER_CPU_THREADS` (default: CPU count)
- `PERSONA_SYNTHETIC_SPEAKER_COUNT` (default: `2`)
- `PERSONA_SPEAKER_TURN_GAP_SECONDS` (default: `1.6`)

## Notes

- Do not commit API keys. Keep them in environment variables.
- If you accidentally exposed a key, revoke and rotate it immediately.