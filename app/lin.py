from pathlib import Path
from datetime import datetime, timezone
import json, os, subprocess, uuid
import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Header
from fastapi.responses import HTMLResponse, FileResponse

ROOT=Path(os.getenv('DATA_DIR','data')); JOBS=ROOT/'jobs'; UPLOADS=ROOT/'uploads'
JOBS.mkdir(parents=True,exist_ok=True); UPLOADS.mkdir(parents=True,exist_ok=True)
MAX_MB=int(os.getenv('MAX_UPLOAD_MB','1500'))
app=FastAPI(title='Lin Movie Recap')

def now(): return datetime.now(timezone.utc).isoformat()
def read(j): return json.loads((JOBS/j/'job.json').read_text(encoding='utf8'))
def save(j,**x):
 d=read(j); d.update(x); d['updated_at']=now(); (JOBS/j/'job.json').write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf8'); return d
def run(a):
 r=subprocess.run(a,capture_output=True,text=True,timeout=3600)
 if r.returncode: raise RuntimeError(r.stderr[-2000:])
 return r.stdout
async def transcribe(path,key):
 if not key: raise RuntimeError('Groq API key is missing')
 async with httpx.AsyncClient(timeout=900) as c:
  with path.open('rb') as f:
   r=await c.post('https://api.groq.com/openai/v1/audio/transcriptions',headers={'Authorization':'Bearer '+key},data={'model':'whisper-large-v3-turbo','response_format':'verbose_json','timestamp_granularities[]':'segment'},files={'file':(path.name,f,'audio/wav')})
 if r.is_error: raise RuntimeError('Groq '+str(r.status_code)+': '+r.text[:500])
 return r.json()
async def make_script(text,key):
 if not key: raise RuntimeError('Gemini API key is missing')
 prompt='Write a natural spoken Burmese movie recap from this transcript. Keep chronology, characters, twists and ending. Do not invent events. Return only Burmese narration.\n\n'+text[:120000]
 async with httpx.AsyncClient(timeout=300) as c:
  r=await c.post('https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent',params={'key':key},json={'contents':[{'parts':[{'text':prompt}]}],'generationConfig':{'temperature':0.35,'maxOutputTokens':12000}})
 if r.is_error: raise RuntimeError('Gemini '+str(r.status_code)+': '+r.text[:500])
 try:return r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
 except Exception: raise RuntimeError('Gemini returned no script')
async def process(j,gkey,mkey):
 d=JOBS/j
 try:
  save(j,status='processing',stage='transcript',progress=10)
  wav=d/'audio.wav'; run(['ffmpeg','-y','-i',read(j)['input_path'],'-vn','-ac','1','-ar','16000',str(wav)])
  tr=await transcribe(wav,gkey); text=tr.get('text','').strip(); (d/'transcript.txt').write_text(text,encoding='utf8')
  seg=tr.get('segments',[]); s='\n\n'.join(f"{i+1}\n00:00:{int(x.get('start',0)):02},000 --> 00:00:{int(x.get('end',0)):02},000\n{x.get('text','').strip()}" for i,x in enumerate(seg) if x.get('text')); (d/'transcript.srt').write_text(s+'\n',encoding='utf8')
  save(j,stage='script',progress=45)
  script=await make_script(text,mkey); (d/'burmese_script.txt').write_text(script,encoding='utf8'); save(j,status='completed',stage='step1-ready',progress=100)
 except Exception as e: save(j,status='failed',error=str(e))

@app.get('/',response_class=HTMLResponse)
async def home(): return HTML
@app.get('/health')
async def health(): return {'status':'ok'}
@app.post('/api/jobs',status_code=202)
async def create(background_tasks:BackgroundTasks,file:UploadFile=File(...),groq_key:str=Header('',alias='X-Groq-Key'),gemini_key:str=Header('',alias='X-Gemini-Key')):
 j=uuid.uuid4().hex; d=JOBS/j; d.mkdir(); dest=UPLOADS/(j+'.mp4'); total=0
 try:
  with dest.open('wb') as out:
   while chunk:=await file.read(1024*1024):
    total+=len(chunk)
    if total>MAX_MB*1024*1024: raise HTTPException(413,'Video too large')
    out.write(chunk)
  info=json.loads(run(['ffprobe','-v','quiet','-print_format','json','-show_format','-show_streams',str(dest)])); video=next((s for s in info['streams'] if s.get('codec_type')=='video'),None); audio=next((s for s in info['streams'] if s.get('codec_type')=='audio'),None)
  if not video or not audio: raise HTTPException(400,'MP4 must contain video and audio')
  data={'job_id':j,'filename':file.filename,'input_path':str(dest),'status':'queued','stage':'upload','progress':2,'created_at':now()}; (d/'job.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8'); background_tasks.add_task(process,j,groq_key,gemini_key); return {'job_id':j}
 except HTTPException: raise
 except Exception as e: raise HTTPException(400,str(e))
@app.get('/api/jobs/{j}')
async def status(j):
 d=JOBS/j; x=read(j); x['files']={n:f'/jobs/{j}/{n}' for n in ['transcript.srt','burmese_script.txt'] if (d/n).exists()}; return x
@app.get('/jobs/{j}/{name}')
async def download(j,name):
 p=JOBS/j/name
 if name not in {'transcript.srt','burmese_script.txt'} or not p.exists(): raise HTTPException(404,'File not ready')
 return FileResponse(p,filename=name)
HTML='''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lin Movie Recap</title><style>body{font-family:system-ui,Noto Sans Myanmar;background:#0b1020;color:#fff;max-width:850px;margin:auto;padding:20px}.card{background:#151d33;padding:20px;border-radius:16px;margin:14px 0}input,button{width:100%;padding:12px;margin:7px 0;border-radius:9px;border:1px solid #38435f;background:#0d1425;color:#fff}button{background:#3478f6;border:0;font-weight:700}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.muted{color:#9ca9c5}</style><h1>Lin Movie Recap</h1><div class="card"><h3>API Keys</h3><div class="row"><input id="g" type="password" placeholder="Groq API Key"><input id="m" type="password" placeholder="Gemini API Key"></div><button onclick="save()">Save keys in this browser</button><p class="muted">Groq → transcript. Gemini 3.8 Flash → Burmese script.</p><p><a href="https://console.groq.com/keys">Groq key</a> · <a href="https://aistudio.google.com/apikey">Gemini key</a></p></div><div class="card"><h2>Step 1 — Upload MP4</h2><input id="f" type="file" accept="video/mp4,video/*"><button onclick="start()">Start Step 1</button><p id="s" class="muted">Waiting...</p><progress id="p" value="0" max="100" style="width:100%"></progress></div><div class="card"><h3>Burmese Script</h3><textarea id="out" rows="14" style="width:100%;background:#0d1425;color:white"></textarea></div><script>onload=()=>{g.value=localStorage.groq||'';m.value=localStorage.gemini||''};function save(){localStorage.groq=g.value;localStorage.gemini=m.value;alert('Saved')};async function start(){if(!f.files[0])return alert('MP4 ရွေးပါ');let fd=new FormData();fd.append('file',f.files[0]);let r=await fetch('/api/jobs',{method:'POST',body:fd,headers:{'X-Groq-Key':g.value,'X-Gemini-Key':m.value}}),x=await r.json();if(!r.ok)return alert(x.detail||'Failed');poll(x.job_id)}async function poll(j){let x=await (await fetch('/api/jobs/'+j)).json();p.value=x.progress||0;s.textContent=x.stage+' '+x.progress+'% '+(x.error||'');if(x.files?.['burmese_script.txt'])out.value=await (await fetch(x.files['burmese_script.txt'])).text();if(x.status==='processing'||x.status==='queued')setTimeout(()=>poll(j),2500)}</script>'''
