from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import edge_tts
import httpx
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    groq_api_key: str = ""
    gemini_api_key: str = ""
    data_dir: str = "data"
    max_upload_mb: int = 1500
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


settings = Settings()
ROOT = Path(settings.data_dir).resolve()
UPLOADS = ROOT / "uploads"
JOBS = ROOT / "jobs"
for folder in (UPLOADS, JOBS):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Lin Movie Recap", version="1.0.0")
JOB_STATE: dict[str, dict[str, Any]] = {}
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def update_job(job_id: str, **values: Any) -> None:
    JOB_STATE.setdefault(job_id, {}).update(values, updated_at=utcnow())


def run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                            capture_output=True, text=True, timeout=7200)
    if result.returncode:
        raise RuntimeError((result.stderr or "FFmpeg failed")[-1800:])


async def transcribe_with_groq(audio_path: Path, output_srt: Path) -> str:
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY မထည့်ရသေးပါ။ Render Environment မှာ ထည့်ပါ။")
    async with httpx.AsyncClient(timeout=600) as client:
        with audio_path.open("rb") as audio:
            response = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                data={"model": "whisper-large-v3-turbo", "response_format": "srt"},
                files={"file": (audio_path.name, audio, "audio/mpeg")},
            )
    if response.status_code >= 400:
        raise RuntimeError(f"Groq transcription error ({response.status_code}): {response.text[:800]}")
    output_srt.write_text(response.text, encoding="utf-8")
    return response.text


async def make_burmese_recap(transcript: str, output_path: Path) -> str:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY မထည့်ရသေးပါ။ Render Environment မှာ ထည့်ပါ။")
    prompt = (
        "You are a professional Burmese movie recap writer. Based only on the transcript below, "
        "write a clear, engaging Burmese-language recap script. Keep the story in chronological order, "
        "preserve important plot points, avoid inventing scenes, and use natural spoken Myanmar. "
        "Return only the Burmese narration script.\n\nTRANSCRIPT:\n" + transcript[:90000]
    )
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": settings.gemini_api_key},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192}},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Gemini recap error ({response.status_code}): {response.text[:800]}")
    data = response.json()
    try:
        script = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Gemini returned an empty or unexpected response.")
    output_path.write_text(script, encoding="utf-8")
    return script


async def make_voice(script: str, output_path: Path) -> None:
    # Myanmar neural voices may not be available in every Edge TTS region; surface failures clearly.
    communicate = edge_tts.Communicate(script, voice="my-MM-NilarNeural")
    await communicate.save(str(output_path))


async def process_job(job_id: str, video_path: Path) -> None:
    folder = JOBS / job_id
    audio_path = folder / "audio.mp3"
    original_srt = folder / "transcript.srt"
    script_path = folder / "burmese_recap.txt"
    voice_path = folder / "burmese_voice.mp3"
    try:
        update_job(job_id, status="processing", progress=5, stage="Extracting audio")
        run_ffmpeg(["-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(audio_path)])
        update_job(job_id, progress=20, stage="Transcribing original audio")
        transcript = await transcribe_with_groq(audio_path, original_srt)
        update_job(job_id, progress=45, stage="Writing Burmese recap")
        script = await make_burmese_recap(transcript, script_path)
        update_job(job_id, progress=70, stage="Generating Burmese voice")
        await make_voice(script, voice_path)
        update_job(job_id, status="completed", progress=100, stage="Ready",
                   files={"transcript": f"/jobs/{job_id}/transcript.srt",
                          "script": f"/jobs/{job_id}/burmese_recap.txt",
                          "voice": f"/jobs/{job_id}/burmese_voice.mp3"})
    except Exception as exc:
        update_job(job_id, status="failed", stage="Error", error=str(exc))


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return """<!doctype html><html lang="my"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lin Movie Recap</title><style>
body{font-family:system-ui,sans-serif;background:#10131a;color:#f5f7fb;margin:0;padding:24px}
main{max-width:760px;margin:auto}.panel{background:#1a202c;border:1px solid #303849;border-radius:18px;padding:22px;margin-top:18px}
h1{margin-bottom:8px}.muted{color:#b4bdcc}button{background:#f3c969;border:0;border-radius:10px;padding:12px 18px;font-weight:700}
input{max-width:100%;margin:14px 0}progress{width:100%;height:16px}a{color:#f3c969}
</style></head><body><main><h1>🎬 Lin Movie Recap</h1>
<p class="muted">Upload a movie → create a Burmese recap script and AI voice.</p>
<section class="panel"><form id="form"><label>Movie video (MP4, MOV, MKV, WEBM, AVI)</label><br>
<input name="file" type="file" accept=".mp4,.mov,.mkv,.webm,.avi" required><br>
<button>Start recap</button></form><p id="message" class="muted"></p><progress id="progress" max="100" value="0"></progress>
<div id="results"></div></section></main><script>
const form=document.querySelector('#form'),msg=document.querySelector('#message'),bar=document.querySelector('#progress'),out=document.querySelector('#results');
form.addEventListener('submit',async e=>{e.preventDefault();out.innerHTML='';msg.textContent='Uploading…';
const fd=new FormData(form);try{const r=await fetch('/api/jobs',{method:'POST',body:fd});const j=await r.json();if(!r.ok)throw Error(j.detail||'Upload failed');
let done=false;while(!done){const s=await fetch('/api/jobs/'+j.job_id);const d=await s.json();bar.value=d.progress||0;msg.textContent=(d.stage||d.status)+' — '+bar.value+'%';
if(d.status==='completed'){out.innerHTML=Object.entries(d.files).map(([k,v])=>'<p><a href="'+v+'">Download '+k+'</a></p>').join('');done=true}
if(d.status==='failed'){throw Error(d.error||'Processing failed')}await new Promise(r=>setTimeout(r,2500))}
}catch(err){msg.textContent='Error: '+err.message}});
</script></body></html>"""


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": "Lin Movie Recap"}


@app.post("/api/jobs", status_code=202)
async def create_job(background_tasks: BackgroundTasks, file: UploadFile = File(...)) -> dict[str, str]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "Unsupported video format.")
    job_id = uuid.uuid4().hex
    folder = JOBS / job_id
    folder.mkdir(parents=True, exist_ok=False)
    destination = UPLOADS / f"{job_id}{suffix}"
    total = 0
    try:
        with destination.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_mb * 1024 * 1024:
                    raise HTTPException(413, f"Upload exceeds {settings.max_upload_mb} MB limit.")
                out.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        shutil.rmtree(folder, ignore_errors=True)
        raise
    update_job(job_id, status="queued", progress=0, stage="Queued", filename=Path(file.filename or "video").name)
    background_tasks.add_task(process_job, job_id, destination)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    if job_id not in JOB_STATE:
        raise HTTPException(404, "Job not found. Jobs are stored in this running process; a server restart clears in-memory status.")
    return JOB_STATE[job_id]


@app.get("/jobs/{job_id}/{filename}")
async def download_output(job_id: str, filename: str) -> FileResponse:
    allowed = {"transcript.srt", "burmese_recap.txt", "burmese_voice.mp3"}
    if filename not in allowed:
        raise HTTPException(404, "File not found.")
    path = JOBS / job_id / filename
    if not path.is_file():
        raise HTTPException(404, "Output not ready.")
    return FileResponse(path, filename=filename)
