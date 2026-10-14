from __future__ import annotations
import json, re, shutil, subprocess, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import edge_tts, httpx
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    groq_api_key: str = ""
    gemini_api_key: str = ""
    data_dir: str = "data"
    max_upload_mb: int = 1500
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
settings = Settings()
ROOT = Path(settings.data_dir).resolve(); UPLOADS, JOBS = ROOT / "uploads", ROOT / "jobs"
for folder in (UPLOADS, JOBS): folder.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="AI MM Recap", version="2.0.0")
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
STAGES = ["upload", "transcript", "script", "voice", "subtitles", "render"]

def now() -> str: return datetime.now(timezone.utc).isoformat()
def folder_for(job_id: str) -> Path:
    folder = JOBS / job_id
    if not folder.is_dir() or not (folder / "job.json").is_file(): raise HTTPException(404, "Project not found")
    return folder
def read_job(job_id: str) -> dict[str, Any]: return json.loads((folder_for(job_id) / "job.json").read_text(encoding="utf-8"))
def save_job(job_id: str, **changes: Any) -> dict[str, Any]:
    path = JOBS / job_id / "job.json"; data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"job_id": job_id}
    data.update(changes, updated_at=now()); path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"); return data
def command(args: list[str], timeout: int = 7200) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode: raise RuntimeError((result.stderr or "Command failed")[-2400:])
    return result
def ffprobe(path: Path) -> dict[str, Any]:
    info = json.loads(command(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)], 120).stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {}); audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {"duration": float(info.get("format", {}).get("duration") or 0), "size": int(info.get("format", {}).get("size") or path.stat().st_size), "width": video.get("width", 0), "height": video.get("height", 0), "has_audio": audio is not None, "video_codec": video.get("codec_name", "")}
def srt_timestamp(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000))); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000); return f"{h:02}:{m:02}:{s:02},{ms:03}"
def write_srt(items: list[dict[str, Any]], path: Path) -> None:
    path.write_text("\n\n".join(f"{i+1}\n{srt_timestamp(float(x['start']))} --> {srt_timestamp(float(x['end']))}\n{x['text']}" for i, x in enumerate(items)) + "\n", encoding="utf-8")
def split_text(text: str, max_chars: int = 70) -> list[str]:
    chunks: list[str] = []; current = ""
    for word in text.replace("\n", " ").split():
        if current and len(current) + len(word) + 1 > max_chars: chunks.append(current); current = word
        else: current = (current + " " + word).strip()
    if current: chunks.append(current)
    return chunks
async def groq_transcribe(audio: Path, out: Path) -> str:
    if not settings.groq_api_key: raise RuntimeError("GROQ_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=900) as client:
        with audio.open("rb") as f: r = await client.post("https://api.groq.com/openai/v1/audio/transcriptions", headers={"Authorization": f"Bearer {settings.groq_api_key}"}, data={"model": "whisper-large-v3-turbo", "response_format": "srt"}, files={"file": (audio.name, f, "audio/wav")})
    if r.is_error: raise RuntimeError(f"Transcription provider error ({r.status_code})")
    out.write_text(r.text, encoding="utf-8"); return r.text
async def gemini_recap(transcript: str, length: str, out: Path) -> str:
    if not settings.gemini_api_key: raise RuntimeError("GEMINI_API_KEY is not configured")
    target = {"short": "800", "medium": "1600", "long": "2800"}.get(length, "1600")
    prompt = f"Write a natural spoken Burmese movie recap of about {target} Burmese words. Keep chronology, relationships, twists and ending. Use only facts supported by this transcript; do not invent. Return only the narration script.\n\nTRANSCRIPT:\n{transcript[:120000]}"
    async with httpx.AsyncClient(timeout=300) as client: r = await client.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent", params={"key": settings.gemini_api_key}, json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": .35, "maxOutputTokens": 12000}})
    if r.is_error: raise RuntimeError(f"Recap provider error ({r.status_code})")
    try: script = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError): raise RuntimeError("Recap provider returned an empty response")
    out.write_text(script, encoding="utf-8"); return script
async def generate_voice(script: str, out: Path, voice: str = "my-MM-NilarNeural") -> None:
    chunks = split_text(script, 1800); temp = out.parent / "tts-chunks"; temp.mkdir(exist_ok=True); paths = []
    for i, chunk in enumerate(chunks):
        p = temp / f"{i:04}.mp3"; await edge_tts.Communicate(chunk, voice=voice).save(str(p)); paths.append(p)
    concat = temp / "concat.txt"; concat.write_text("\n".join(f"file '{p.name}'" for p in paths), encoding="utf-8"); command(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(out)])
async def render_job(job_id: str) -> None:
    job = read_job(job_id); folder = folder_for(job_id); out = folder / "final.mp4"; vf = []
    if job.get("aspect_ratio") == "vertical": vf.append("scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2")
    elif job.get("aspect_ratio") == "square": vf.append("scale=720:720:force_original_aspect_ratio=decrease,pad=720:720:(ow-iw)/2:(oh-ih)/2")
    subtitle = folder / "recap_subtitles.srt"
    if job.get("subtitles", True) and subtitle.exists(): vf.append(f"subtitles={subtitle}:force_style='FontName=Noto Sans Myanmar,FontSize={job.get('font_size',20)},Outline=2,Shadow=1,MarginV=40'")
    args = ["ffmpeg", "-y", "-i", job["input_path"], "-i", str(folder / "narration.mp3"), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart"]
    if vf: args += ["-vf", ",".join(vf)]
    command(args + [str(out)], 7200)
    if not out.is_file() or out.stat().st_size < 1024: raise RuntimeError("Final MP4 was not created or validated")
async def process(job_id: str, start_stage: str = "transcript") -> None:
    folder = JOBS / job_id; job = read_job(job_id); video = Path(job["input_path"])
    try:
        index = STAGES.index(start_stage)
        if index <= 1:
            save_job(job_id, status="processing", stage="transcript", progress=12); audio = folder / "original_audio.wav"; command(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)]); await groq_transcribe(audio, folder / "transcript.srt"); save_job(job_id, progress=28, stage="script"); index = 2
        if index <= 2:
            script = await gemini_recap((folder / "transcript.srt").read_text(encoding="utf-8"), job.get("length", "medium"), folder / "recap_script.txt"); save_job(job_id, progress=48, stage="voice", script_preview=script[:300]); index = 3
        if index <= 3:
            await generate_voice((folder / "recap_script.txt").read_text(encoding="utf-8"), folder / "narration.mp3", job.get("voice", "my-MM-NilarNeural")); save_job(job_id, progress=66, stage="subtitles"); index = 4
        if index <= 4:
            duration = float(command(["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(folder / "narration.mp3")], 120).stdout.strip() or 0); chunks = split_text((folder / "recap_script.txt").read_text(encoding="utf-8"), int(job.get("max_chars", 42))); per = duration / max(1, len(chunks)); write_srt([{"start": i * per, "end": min(duration, (i + 1) * per - .05), "text": c} for i, c in enumerate(chunks)], folder / "recap_subtitles.srt"); save_job(job_id, progress=78, stage="render"); index = 5
        if index <= 5: await render_job(job_id); save_job(job_id, status="completed", progress=100, stage="ready")
    except Exception as exc: save_job(job_id, status="failed", error=str(exc))
class EditRequest(BaseModel):
    length: str | None = None; voice: str | None = None; aspect_ratio: str | None = None; subtitles: bool | None = None; font_size: int | None = None; max_chars: int | None = None; script: str | None = None
@app.get("/", response_class=HTMLResponse)
async def home() -> str: return HTML
@app.get("/health")
async def health() -> dict[str, str]: return {"status": "ok", "app": "AI MM Recap"}
@app.get("/api/projects")
async def projects() -> list[dict[str, Any]]: return sorted([read_job(p.name) for p in JOBS.iterdir() if (p / "job.json").exists()], key=lambda x: x.get("created_at", ""), reverse=True)
@app.post("/api/jobs", status_code=202)
async def create_job(background_tasks: BackgroundTasks, file: UploadFile = File(...), length: str = "medium", voice: str = "my-MM-NilarNeural") -> dict[str, str]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS: raise HTTPException(400, "Unsupported video format")
    job_id = uuid.uuid4().hex; folder = JOBS / job_id; folder.mkdir(); destination = UPLOADS / f"{job_id}{suffix}"; total = 0
    try:
        with destination.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_mb * 1024 * 1024: raise HTTPException(413, f"Maximum upload is {settings.max_upload_mb} MB")
                out.write(chunk)
        media = ffprobe(destination)
        if not media["has_audio"]: raise HTTPException(400, "The video has no audio track")
        data = {"job_id": job_id, "filename": Path(file.filename or "video").name, "input_path": str(destination), "media": media, "length": length, "voice": voice, "subtitles": True, "aspect_ratio": "original", "max_chars": 42, "status": "queued", "stage": "upload", "progress": 2, "created_at": now(), "updated_at": now()}; (folder / "job.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except HTTPException: destination.unlink(missing_ok=True); shutil.rmtree(folder, ignore_errors=True); raise
    except Exception as exc: destination.unlink(missing_ok=True); shutil.rmtree(folder, ignore_errors=True); raise HTTPException(400, f"Could not validate media: {exc}")
    background_tasks.add_task(process, job_id); return {"job_id": job_id, "status": "queued"}
@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    data = read_job(job_id); folder = JOBS / job_id; data["files"] = {name: f"/jobs/{job_id}/{name}" for name in ["original_audio.wav", "transcript.srt", "recap_script.txt", "narration.mp3", "recap_subtitles.srt", "final.mp4"] if (folder / name).is_file()}; return data
@app.patch("/api/jobs/{job_id}")
async def edit_job(job_id: str, request: EditRequest) -> dict[str, Any]:
    folder = folder_for(job_id); changes = request.model_dump(exclude_none=True)
    if "script" in changes: (folder / "recap_script.txt").write_text(changes.pop("script"), encoding="utf-8")
    return save_job(job_id, **changes, status="editing")
@app.post("/api/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    job = read_job(job_id); stage = job.get("stage", "transcript"); start = stage if stage in STAGES[1:] else "transcript"; save_job(job_id, status="queued", error=None, progress=5); background_tasks.add_task(process, job_id, start); return {"status": "queued"}
@app.post("/api/jobs/{job_id}/render", status_code=202)
async def render_endpoint(job_id: str, background_tasks: BackgroundTasks) -> dict[str, str]:
    folder_for(job_id); save_job(job_id, status="processing", stage="render", progress=86); background_tasks.add_task(render_job, job_id); return {"status": "queued"}
@app.get("/jobs/{job_id}/{filename}")
async def download(job_id: str, filename: str) -> FileResponse:
    allowed = {"original_audio.wav", "transcript.srt", "recap_script.txt", "narration.mp3", "recap_subtitles.srt", "final.mp4"}
    if filename not in allowed: raise HTTPException(404, "File not found")
    path = folder_for(job_id) / filename
    if not path.is_file(): raise HTTPException(404, "File not ready")
    return FileResponse(path, filename=filename)
HTML = '''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI MM Recap</title><style>:root{--bg:#0b1020;--panel:#141c31;--line:#273452;--muted:#93a4c5;--accent:#f3c969}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% -10%,#24345d 0,#0b1020 45%);color:#f6f8ff;font:15px system-ui}header{height:72px;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 5%;justify-content:space-between}nav{display:flex;gap:22px;color:var(--muted)}main{max-width:1120px;margin:auto;padding:38px 18px}.hero{display:flex;justify-content:space-between;align-items:end}.eyebrow{color:var(--accent);font-weight:700;letter-spacing:.12em;text-transform:uppercase;font-size:12px}h1{font-size:clamp(32px,5vw,58px);line-height:1.04;margin:10px 0;max-width:680px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:20px;margin-top:30px}.panel{background:rgba(20,28,49,.92);border:1px solid var(--line);border-radius:20px;padding:24px}.drop{display:block;border:1px dashed #6376a6;border-radius:16px;padding:34px 18px;text-align:center;cursor:pointer;background:#ffffff05}input[type=file]{display:none}button,select,input,textarea{font:inherit}button{border:0;border-radius:10px;padding:12px 16px;font-weight:750;cursor:pointer;background:var(--accent);color:#181306}button.secondary{background:#273452;color:#fff}select,textarea{width:100%;background:#0c1426;color:#fff;border:1px solid var(--line);border-radius:9px;padding:10px}textarea{min-height:180px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.steps{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}.step{padding:8px 11px;border-radius:999px;background:#202c49;color:var(--muted);font-size:13px}.step.active{background:#54492a;color:#ffe8a0}.step.done{background:#173c36;color:#8ff0c7}.bar{height:8px;background:#25304a;border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:18px 0}.meta div{background:#0f172b;padding:12px;border-radius:10px}.meta b{display:block;font-size:18px}.meta small{color:var(--muted)}label{display:block;color:var(--muted);margin:14px 0 6px}video{width:100%;border-radius:14px;background:#05070d;max-height:420px}.links{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.links a{color:#ffe08a;background:#273452;text-decoration:none;padding:9px;border-radius:8px;font-size:13px}.hidden{display:none}.error{color:#ff7d88}@media(max-width:780px){.grid{grid-template-columns:1fr}.hero{display:block}nav{display:none}.meta{grid-template-columns:1fr 1fr}}</style></head><body><header><strong>◈ AI MM Recap</strong><nav><span>Create Recap</span><span>My Projects</span><span>AI Settings</span></nav></header><main><section class="hero"><div><div class="eyebrow">Burmese movie studio</div><h1>Turn a movie into a story worth sharing.</h1><p class="muted">Upload your video, generate a Burmese recap, synchronize subtitles, and export a polished MP4.</p></div><button class="secondary" onclick="location.reload()">+ New Project</button></section><div class="steps" id="steps"></div><div class="grid"><section class="panel"><h2>1. Upload movie</h2><p class="muted">MP4, MOV, MKV, WebM or AVI</p><label class="drop" for="file"><span id="dropText">Choose a video</span><input id="file" type="file" accept=".mp4,.mov,.mkv,.webm,.avi"></label><div class="row"><div style="flex:1"><label>Recap length</label><select id="length"><option value="short">Short</option><option value="medium" selected>Medium</option><option value="long">Long</option></select></div><div style="flex:1"><label>Voice</label><select id="voice"><option value="my-MM-NilarNeural">Myanmar · Nilar</option></select></div></div><button id="start" style="margin-top:20px" disabled>Start recap</button><div id="status" class="muted">Select a movie to begin.</div><div class="bar"><i id="progress"></i></div><div id="media" class="meta hidden"></div></section><section class="panel"><h2>Studio preview</h2><div id="empty" class="muted">Your final preview will appear here after processing.</div><video id="preview" controls class="hidden"></video><div id="settings" class="hidden"><div class="row"><div style="flex:1"><label>Aspect ratio</label><select id="ratio"><option value="original">Original</option><option value="vertical">9:16 vertical</option><option value="square">1:1 square</option></select></div><div style="flex:1"><label>Subtitle size</label><input id="font" type="number" min="12" max="48" value="20" style="width:100%;padding:10px"></div></div><label><input id="subs" type="checkbox" checked> Subtitles on</label><button id="render" class="secondary">Render final MP4</button></div><div id="links" class="links"></div></section></div><section id="editor" class="panel hidden" style="margin-top:20px"><h2>Recap script editor</h2><textarea id="script"></textarea><div class="row" style="margin-top:12px"><button id="save">Save script</button><button id="retry" class="secondary">Retry failed step</button></div></section></main><script>let job,timer;const $=x=>document.getElementById(x),names=['Upload','Transcript','Recap script','AI voice','Subtitles','Final render'],idx=s=>({upload:0,transcript:1,script:2,voice:3,subtitles:4,render:5,ready:6}[s]??0);function draw(){ $('steps').innerHTML=names.map((n,i)=>`<span class="step ${job&&i<idx(job.stage)?'done':''} ${job&&i===idx(job.stage)?'active':''}">${i+1} ${n}</span>`).join('')}function show(d){job=d;draw();$('progress').style.width=(d.progress||0)+'%';$('status').innerHTML=d.status==='failed'?`<span class="error">${d.error||'Processing failed'}</span>`:`${d.stage||d.status} · ${d.progress||0}%`;if(d.media){$('media').classList.remove('hidden');$('media').innerHTML=`<div><b>${d.media.width}×${d.media.height}</b><small>Resolution</small></div><div><b>${Math.round(d.media.duration)}s</b><small>Duration</small></div><div><b>${(d.media.size/1048576).toFixed(1)} MB</b><small>File size</small></div>`}if(d.files?.['final.mp4']){$('empty').classList.add('hidden');$('preview').classList.remove('hidden');$('preview').src=d.files['final.mp4'];$('settings').classList.remove('hidden')}if(d.files?.['recap_script.txt']){$('editor').classList.remove('hidden');fetch(d.files['recap_script.txt']).then(r=>r.text()).then(t=>$('script').value=t)}$('links').innerHTML=Object.entries(d.files||{}).filter(([k])=>k!=='original_audio.wav').map(([k,v])=>`<a href="${v}" download>${k==='final.mp4'?'Download Final MP4':'Download '+k}</a>`).join('')}async function poll(){if(!job)return;let r=await fetch('/api/jobs/'+job.job_id);if(r.ok){show(await r.json());if(['completed','failed'].includes(job.status)){clearInterval(timer)}}}$('file').onchange=e=>{if(e.target.files[0]){$('dropText').textContent=e.target.files[0].name;$('start').disabled=false}};$('start').onclick=async()=>{let fd=new FormData();fd.append('file',$('file').files[0]);fd.append('length',$('length').value);fd.append('voice',$('voice').value);let r=await fetch('/api/jobs',{method:'POST',body:fd}),d=await r.json();if(!r.ok){$('status').textContent=d.detail||'Upload failed';return}show(d);timer=setInterval(poll,2000);poll()};$('save').onclick=async()=>{let r=await fetch('/api/jobs/'+job.job_id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({script:$('script').value})});show(await r.json())};$('render').onclick=async()=>{await fetch('/api/jobs/'+job.job_id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({aspect_ratio:$('ratio').value,font_size:+$('font').value,subtitles:$('subs').checked})});await fetch('/api/jobs/'+job.job_id+'/render',{method:'POST'});timer=setInterval(poll,2000)};$('retry').onclick=async()=>{await fetch('/api/jobs/'+job.job_id+'/retry',{method:'POST'});timer=setInterval(poll,2000)};draw();</script></body></html>'''
