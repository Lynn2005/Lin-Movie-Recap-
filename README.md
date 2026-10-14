# AI MM Recap

A production-oriented FastAPI application for creating Burmese movie recap videos. The workflow uploads and validates a movie, extracts audio, creates a timestamped original transcript with Groq Whisper, writes a Burmese recap with Gemini, generates Myanmar narration with Edge TTS, synchronizes subtitles to the actual narration duration, and renders a downloadable H.264/AAC MP4 with FFmpeg.

## Run locally

Requires Python 3.11+ and FFmpeg.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# add GROQ_API_KEY and GEMINI_API_KEY to .env
uvicorn app.main:app --reload
```

Open `http://localhost:8000`. The health check is `/health`.

## Deployment

The included Dockerfile installs FFmpeg and Myanmar-compatible fonts. Render can deploy the repository using the included `render.yaml`; add `GROQ_API_KEY` and `GEMINI_API_KEY` as secret environment variables. For real production persistence, attach a persistent disk and set `DATA_DIR` to its mount path. The free Render filesystem is otherwise ephemeral.

## API workflow

- `POST /api/jobs` — streamed upload plus FFprobe validation
- `GET /api/jobs/{job_id}` — persistent job status and available files
- `PATCH /api/jobs/{job_id}` — edit script and rendering settings
- `POST /api/jobs/{job_id}/retry` — retry from the failed stage
- `POST /api/jobs/{job_id}/render` — render after editing
- `GET /jobs/{job_id}/{filename}` — download validated intermediate/final files
- `GET /api/projects` — list saved projects

Jobs are stored as `data/jobs/{job_id}/job.json` with media files retained until the data directory is explicitly removed. API keys are only read server-side and are never sent to browser JavaScript.

## Provider notes

Groq and Gemini usage is subject to their current account limits and pricing. Edge TTS availability can change; provider errors are surfaced as actionable job failures. The implementation does not claim any provider is unlimited or permanently free.
