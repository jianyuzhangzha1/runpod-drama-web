import io, json, os, re, secrets, sqlite3, subprocess, threading, time, uuid
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlparse
import requests
from PIL import Image, ImageDraw
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
DATA = Path(os.getenv('DATA_DIR', str(ROOT / 'data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
MEDIA = DATA / 'media'; MEDIA.mkdir(exist_ok=True)
DB = DATA / 'studio.db'
DEMO = os.getenv('DEMO_MODE', 'true').lower() == 'true'
KEY = os.getenv('RUNPOD_API_KEY', '')
PUBLIC = os.getenv('PUBLIC_MEDIA_BASE_URL', '').rstrip('/')
BASE = 'https://api.runpod.ai/v2'
ENDPOINTS = {'image': 'black-forest-labs-flux-1-schnell', 'video': 'wan-2-5'}
lock = threading.RLock()
wake = threading.Event()
stop = threading.Event()

def db():
    c = sqlite3.connect(DB, timeout=30); c.row_factory = sqlite3.Row
    return c

def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, title TEXT, style TEXT);
        CREATE TABLE IF NOT EXISTS shots(id TEXT PRIMARY KEY, project TEXT, position INTEGER, title TEXT, image_prompt TEXT, video_prompt TEXT, narration TEXT, duration INTEGER, image TEXT, video TEXT, source_url TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project TEXT, shot TEXT, kind TEXT, state TEXT, remote_id TEXT, payload TEXT, output TEXT, error TEXT, created REAL, cost REAL);
        ''')
        c.execute("UPDATE jobs SET state='uncertain', error='提交时服务中断，请在 RunPod 核对后处理；不会自动重复计费提交' WHERE state='submitting'")
        c.execute("UPDATE jobs SET state='queued' WHERE state='working'")
init()

def row(table, id):
    with db() as c: r = c.execute(f'SELECT * FROM {table} WHERE id=?', (id,)).fetchone()
    if not r: raise HTTPException(404, '记录不存在')
    return dict(r)

def update_job(id, **kw):
    with db() as c: c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in kw)+' WHERE id=?', (*kw.values(), id))

def media_name(ext): return uuid.uuid4().hex + ext

def run(args):
    p = subprocess.run(args, capture_output=True, timeout=600)
    if p.returncode: raise RuntimeError('FFmpeg 处理失败：'+p.stderr.decode(errors='replace')[-800:])

def download(url, suffix):
    # Only public HTTPS destinations, checked at every redirect. API credentials never accompany media requests.
    import socket, ipaddress
    name = media_name(suffix); tmp = MEDIA / (name+'.part')
    try:
        for _ in range(5):
            u = urlparse(url)
            if u.scheme != 'https' or not u.hostname or u.username: raise ValueError('素材 URL 必须为公网 HTTPS')
            if any(not ipaddress.ip_address(a[4][0]).is_global for a in socket.getaddrinfo(u.hostname, u.port or 443)):
                raise ValueError('不允许下载内网地址')
            with requests.get(url, stream=True, timeout=(10,90), allow_redirects=False) as r:
                if r.is_redirect:
                    from urllib.parse import urljoin
                    url = urljoin(url, r.headers['Location']); continue
                r.raise_for_status(); size=0
                with tmp.open('wb') as f:
                    for chunk in r.iter_content(1024*1024):
                        size += len(chunk)
                        if size > 512*1024*1024: raise ValueError('素材超过 512MB')
                        f.write(chunk)
                tmp.replace(MEDIA/name); return name
        raise ValueError('重定向过多')
    finally: tmp.unlink(missing_ok=True)

def execute(j):
    p = json.loads(j['payload'])
    if j['kind'] == 'assemble':
        update_job(j['id'], state='working')
        parts=[]; subs=[]; cursor=0
        def stamp(t):
            ms=int(t*1000); return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
        for i,s in enumerate(p['shots']):
            name=media_name('.mp4'); parts.append(name)
            source=MEDIA/s['video']
            probe=subprocess.run(['ffprobe','-v','error','-select_streams','a','-show_entries','stream=index','-of','csv=p=0',str(source)],capture_output=True,check=True)
            args=['ffmpeg','-y','-i',str(source)]
            if not probe.stdout.strip(): args+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
            args+=['-map','0:v:0','-map','0:a:0' if probe.stdout.strip() else '1:a:0','-vf','scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,tpad=stop_mode=clone:stop_duration=10','-af','apad','-t',str(s['duration']),'-r','24','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2',str(MEDIA/name)]
            run(args)
            if s['narration']: subs.append(f'{len(subs)+1}\n{stamp(cursor)} --> {stamp(cursor+s["duration"])}\n{s["narration"]}\n')
            cursor+=s['duration']
        listing=MEDIA/media_name('.txt'); listing.write_text(''.join(f"file '{x}'\n" for x in parts))
        out=media_name('.mp4'); srt=media_name('.srt'); (MEDIA/srt).write_text('\n'.join(subs),encoding='utf-8')
        run(['ffmpeg','-y','-f','concat','-safe','0','-i',str(listing),'-c','copy','-movflags','+faststart',str(MEDIA/out)])
        for x in parts: (MEDIA/x).unlink(missing_ok=True)
        listing.unlink(missing_ok=True)
        update_job(j['id'],state='completed',output=json.dumps({'file':out,'subtitles':srt})); return
    if DEMO:
        update_job(j['id'],state='working')
        if j['kind']=='image':
            name=media_name('.png'); im=Image.new('RGB',(1024,576),'#172b35'); d=ImageDraw.Draw(im)
            d.rectangle((55,55,969,521),outline='#e6b775',width=3); d.text((95,260),'DEMO / STORYBOARD / '+j['shot'][:8],fill='white'); im.save(MEDIA/name)
        else:
            name=media_name('.mp4')
            run(['ffmpeg','-y','-loop','1','-i',str(MEDIA/p['local_image']),'-t',str(p['input']['duration']),'-vf','scale=1280:720','-r','24','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p',str(MEDIA/name)])
        remote_url=''
    else:
        headers={'Authorization':f'Bearer {KEY}'}
        endpoint=ENDPOINTS[j['kind']]
        if not j['remote_id']:
            update_job(j['id'],state='submitting')
            try:
                r=requests.post(f'{BASE}/{endpoint}/run',headers=headers,json={'input':p['input']},timeout=(10,60))
                r.raise_for_status(); remote=r.json()['id']
            except Exception:
                update_job(j['id'],state='uncertain',error='提交结果不确定：请先到 RunPod 查询，填入任务 ID 继续。不会自动重发。'); return
            update_job(j['id'],remote_id=remote,state='polling'); j['remote_id']=remote
        update_job(j['id'],state='polling')
        r=requests.get(f'{BASE}/{endpoint}/status/{j["remote_id"]}',headers=headers,timeout=(10,60)); r.raise_for_status(); result=r.json()
        state=result.get('status')
        if state in ('FAILED','CANCELLED','TIMED_OUT'): raise RuntimeError(f'云端任务 {state}: '+str(result.get('error',''))[:500])
        if state!='COMPLETED': return
        output=result['output']; remote_url=output['image_url' if j['kind']=='image' else 'result']
        name=download(remote_url,'.png' if j['kind']=='image' else '.mp4')
        update_job(j['id'],cost=output.get('cost'))
    with db() as c:
        if j['kind']=='image':
            c.execute('UPDATE shots SET image=?, source_url=?, video=NULL WHERE id=?',(name,remote_url,j['shot']))
        else: c.execute('UPDATE shots SET video=? WHERE id=?',(name,j['shot']))
    update_job(j['id'],state='completed',output=json.dumps({'file':name}),error='')

def worker():
    while not stop.is_set():
        with db() as c: jobs=[dict(r) for r in c.execute("SELECT * FROM jobs WHERE state IN ('queued','polling') ORDER BY created")]
        for j in jobs:
            if stop.is_set(): break
            try: execute(j)
            except Exception as e:
                update_job(j['id'],state='paused' if row('jobs',j['id'])['remote_id'] else 'failed',error=str(e)[:1000])
        wake.wait(4); wake.clear()

@asynccontextmanager
async def lifespan(app):
    stop.clear(); t=threading.Thread(target=worker,daemon=True); t.start()
    yield
    stop.set(); wake.set(); t.join(timeout=2)
app=FastAPI(title='RunPod Drama Studio',lifespan=lifespan)

@app.middleware('http')
async def origin_guard(request: Request, call_next):
    origin=request.headers.get('origin')
    if request.method not in ('GET','HEAD','OPTIONS') and origin and urlparse(origin).netloc != request.headers.get('host'):
        return JSONResponse({'detail':'跨站写入已拒绝'},403)
    return await call_next(request)

app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
@app.get('/')
def home(): return FileResponse(ROOT/'static/index.html')
@app.get('/media/{name}')
def media(name:str):
    if not re.fullmatch(r'[a-f0-9]{32}\.(png|mp4|srt)',name) or not (MEDIA/name).is_file(): raise HTTPException(404)
    return FileResponse(MEDIA/name)
@app.get('/api/config')
def config(): return {'demo':DEMO,'key_configured':bool(KEY),'public_media_configured':bool(PUBLIC),'version':'0.1.0'}

class Project(BaseModel):
    title:str=Field(min_length=1,max_length=120)
    style:str=Field(default='黑白胶片，写实，克制，16:9 横构图',max_length=3000)
class Shot(BaseModel):
    title:str=Field(default='新镜头',max_length=120)
    image_prompt:str=Field(default='',max_length=5000)
    video_prompt:str=Field(default='',max_length=5000)
    narration:str=Field(default='',max_length=2000)
    duration:int=Field(default=5)
    position:int=Field(default=1,ge=1,le=1000)

@app.get('/api/projects')
def projects():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM projects ORDER BY rowid DESC')]
@app.post('/api/projects')
def create_project(p:Project):
    id=uuid.uuid4().hex
    with db() as c: c.execute('INSERT INTO projects VALUES(?,?,?)',(id,p.title,p.style))
    return row('projects',id)
@app.get('/api/projects/{id}')
def project(id:str):
    p=row('projects',id)
    with db() as c:
        p['shots']=[dict(r) for r in c.execute('SELECT * FROM shots WHERE project=? ORDER BY position,rowid',(id,))]
        p['jobs']=[dict(r) for r in c.execute('SELECT * FROM jobs WHERE project=? ORDER BY created DESC',(id,))]
    return p

def idle(shot):
    with db() as c: busy=c.execute("SELECT 1 FROM jobs WHERE shot=? AND state IN ('queued','working','submitting','polling','paused','uncertain')",(shot,)).fetchone()
    if busy: raise HTTPException(409,'此镜头有未结束任务；请先恢复或核对任务后放弃跟踪')
@app.post('/api/projects/{id}/shots')
def add_shot(id:str,s:Shot):
    row('projects',id)
    if s.duration not in (5,10): raise HTTPException(422,'时长仅支持 5 或 10 秒')
    sid=uuid.uuid4().hex
    with db() as c: c.execute('INSERT INTO shots VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL)',(sid,id,s.position,s.title,s.image_prompt,s.video_prompt,s.narration,s.duration))
    return row('shots',sid)
@app.put('/api/shots/{id}')
def edit_shot(id:str,s:Shot):
    with lock:
        row('shots',id); idle(id)
        if s.duration not in (5,10): raise HTTPException(422,'时长仅支持 5 或 10 秒')
        with db() as c: c.execute('UPDATE shots SET '+','.join(k+'=?' for k in s.model_dump())+' WHERE id=?',(*s.model_dump().values(),id))
    return row('shots',id)
@app.delete('/api/shots/{id}')
def delete_shot(id:str):
    with lock:
        idle(id)
        with db() as c:c.execute('DELETE FROM shots WHERE id=?',(id,))
    return {'ok':True}
@app.post('/api/shots/{id}/image')
def upload(id:str,file:UploadFile=File(...)):
    with lock:
        row('shots',id); idle(id)
        content=file.file.read(20*1024*1024+1)
        if len(content)>20*1024*1024: raise HTTPException(413,'图片最大 20MB')
        try:
            im=Image.open(io.BytesIO(content)); im.load(); im=im.convert('RGB'); im.thumbnail((2048,2048))
        except Exception: raise HTTPException(422,'图片无法读取')
        name=media_name('.png'); im.save(MEDIA/name)
        with db() as c:c.execute('UPDATE shots SET image=?,video=NULL,source_url=NULL WHERE id=?',(name,id))
    return {'file':name}

def queue(project,shot,kind,payload):
    id=uuid.uuid4().hex
    with db() as c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,NULL,?,NULL,NULL,?,NULL)',(id,project,shot,kind,'queued',json.dumps(payload,ensure_ascii=False),time.time()))
    wake.set(); return row('jobs',id)
@app.post('/api/shots/{id}/generate/{kind}')
def generate(id:str,kind:str):
    with lock:
        s=row('shots',id); p=row('projects',s['project']); idle(id)
        if kind not in ENDPOINTS: raise HTTPException(404)
        if not DEMO and not KEY: raise HTTPException(400,'请在服务端配置 RUNPOD_API_KEY')
        if kind=='image':
            if not s['image_prompt'].strip(): raise HTTPException(422,'先填写图片提示词')
            inp={'prompt':p['style']+'\n'+s['image_prompt'],'width':1024,'height':576,'num_inference_steps':4}
        else:
            if not s['image']: raise HTTPException(422,'先生成或上传关键帧')
            if not s['video_prompt'].strip(): raise HTTPException(422,'先填写动作提示词')
            image=(PUBLIC+'/media/'+s['image']) if PUBLIC else s['source_url']
            if not DEMO and not image: raise HTTPException(422,'上传的关键帧需要 PUBLIC_MEDIA_BASE_URL 公网 HTTPS 素材地址')
            inp={'image':image,'prompt':s['video_prompt'],'duration':s['duration'],'resolution':'720p','enable_prompt_expansion':False}
        return queue(s['project'],id,kind,{'input':inp,'local_image':s['image']})
@app.post('/api/jobs/{id}/resume')
def resume(id:str):
    with lock:
        j=row('jobs',id)
        if j['state']!='paused' or not j['remote_id']: raise HTTPException(409,'只有已知云端 ID 的暂停任务可以恢复')
        update_job(id,state='polling',error=''); wake.set()
    return {'ok':True}
class Recover(BaseModel):
    remote_id:str=Field(pattern=r'^[A-Za-z0-9_-]{1,160}$')
@app.post('/api/jobs/{id}/recover')
def recover(id:str,r:Recover):
    with lock:
        j=row('jobs',id)
        if j['state']!='uncertain': raise HTTPException(409)
        update_job(id,state='polling',remote_id=r.remote_id,error=''); wake.set()
    return {'ok':True}
@app.post('/api/jobs/{id}/abandon')
def abandon(id:str):
    with lock:
        if row('jobs',id)['state'] not in ('uncertain','paused'): raise HTTPException(409)
        update_job(id,state='abandoned',error='已停止本地跟踪；不代表云端任务取消，可能仍计费')
    return {'ok':True}
@app.post('/api/projects/{id}/assemble')
def assemble(id:str):
    p=project(id)
    if not p['shots'] or any(not s['video'] for s in p['shots']): raise HTTPException(422,'请先完成全部镜头的视频')
    with lock:
        if any(j['kind']=='assemble' and j['state'] in ('queued','working') for j in project(id)['jobs']): raise HTTPException(409,'正在合成')
        return queue(id,None,'assemble',{'shots':p['shots']})

class PromptRequest(BaseModel):
    prompt:str=Field(min_length=1,max_length=6000)
    kind:str=Field(pattern='^(image|video)$')
@app.post('/api/enhance')
def enhance(p:PromptRequest):
    if DEMO: return {'prompt':p.prompt+'\n'+('柔和自然光，清晰主体，16:9 横构图。' if p.kind=='image' else '只表现一个主要动作，保持人物和场景一致，运镜缓慢。')}
    if not KEY: raise HTTPException(400,'请配置 RUNPOD_API_KEY')
    system='Rewrite this '+p.kind+' generation prompt. Preserve all characters, period, actions and constraints. '
    system+=('Describe composition, lighting and style.' if p.kind=='image' else 'Describe a single main action, camera motion and ending state.')
    system+=' Under 150 words, output only the final prompt, no reasoning. /no_think'
    try:
        r=requests.post(BASE+'/qwen3-32b-awq/openai/v1/chat/completions',headers={'Authorization':f'Bearer {KEY}'},json={'model':'Qwen/Qwen3-32B-AWQ','messages':[{'role':'system','content':system},{'role':'user','content':p.prompt}],'max_tokens':1000,'temperature':0.4},timeout=(10,120))
        r.raise_for_status(); result=r.json()
        if result['choices'][0].get('finish_reason')=='length': raise ValueError('模型输出被截断，请缩短输入再试')
        content=result['choices'][0]['message']['content'] or ''
        content=re.sub(r'<think>.*?</think>','',content,flags=re.S)
        content=re.sub(r'<think>.*','',content,flags=re.S).strip()
        if not content: raise ValueError('模型没有返回有效提示词')
        return {'prompt':content}
    except Exception as e: raise HTTPException(502,'提示词优化失败：'+str(e)[:300])
