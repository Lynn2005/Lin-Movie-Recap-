# Lin Movie Recap

A Python 3 / FastAPI starter for turning an uploaded movie into a Burmese recap script and AI narration.

## Current workflow

1. Upload MP4, MOV, MKV, WEBM, or AVI.
2. FFmpeg extracts audio.
3. Groq Whisper creates an original transcript SRT.
4. Gemini writes a Burmese recap script.
5. Edge TTS attempts to generate Burmese narration.
6. Download the transcript, script, and voice from the results page.

## Deploy on Render

This repository includes a Dockerfile and `render.yaml`.

1. Open Render and create a new Web Service from this repository.
2. Choose Docker as the runtime if asked.
3. Add `GROQ_API_KEY` and `GEMINI_API_KEY` under Environment.
4. Deploy and check `/health`.

Never put API keys in source code or commit a real `.env` file.

## Local run

Requires Python 3.11+ and FFmpeg.

```bash
pip install -r requirements.txt
cp .env.example .env
# Add API keys to .env
uvicorn app.main:app --reload
```

## Important current limitations

- This is the initial working foundation, not the complete editor/export pipeline yet.
- Job status is currently held in process memory; a restart clears status. Use persistent job storage and a queue before relying on long jobs in production.
- The current version creates the narration audio but does not yet merge it with the video or render Burmese subtitles.
- Render disk/persistence and upload limits must be checked against the selected Render plan.
- TTS provider availability and Myanmar Unicode rendering need real deployment testing.

## Environment variables

- `GROQ_API_KEY`: Groq API key for transcription.
- `GEMINI_API_KEY`: Google Gemini API key for recap generation.
- `DATA_DIR`: data directory; defaults to `data`.
- `MAX_UPLOAD_MB`: upload size limit; defaults to 1500 MB.
