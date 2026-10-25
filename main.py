import os, uuid, sqlite3, subprocess, base64, shutil, traceback
from pathlib import Path
import httpx
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent
DATA = Path(os.getenv("DATA_DIR", ROOT / "data")); DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "app.db"
KEY = os.getenv("OPENAI_API_KEY", "")
BASE = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")

DEFAULT_PROMPT = """Buat foto produk ultra-HD, sangat realistis, dan tajam berdasarkan foto produk asli yang diunggah. Pertahankan desain, warna, motif, logo, tulisan, tekstur, jahitan, bentuk, potongan, dan detail produk semirip mungkin dengan aslinya.

Letakkan baju secara flat lay di atas karpet silver tebal dengan latar lantai kayu cokelat natural. Tambahkan cahaya matahari alami dari jendela yang menghasilkan bayangan jendela realistis. Dekorasi ruangan minimalis di sekitar berupa rak sepatu, meja terbuka, dan cermin berdiri yang memantulkan cahaya lembut.

Ciptakan suasana hangat, clean, modern, dan profesional seperti fotografi produk brand fashion. Produk menjadi fokus utama, pencahayaan seimbang, tekstur kain terlihat jelas, bayangan alami, dan perspektif kamera dari atas.

Jangan mengubah identitas produk, menambahkan logo baru, atau membuat detail produk yang tidak ada pada foto asli. Rasio vertikal 9:16 untuk TikTok, kualitas visual setinggi mungkin."""


def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def q(sql, args=(), one=False):
    with db() as c:
        cur = c.execute(sql, args); rows = cur.fetchall()
    return (rows[0] if rows else None) if one else [dict(r) for r in rows]

def x(sql, args=()):
    with db() as c: c.execute(sql, args)

def init():
    with db() as c:
        c.executescript("""
        create table if not exists products(id text primary key, name text, price text, description text, link text,
            photo text, ai_photo text, ai_status text default 'belum', approved integer default 0);
        create table if not exists templates(id text primary key, name text, path text, x int, y int, w int, h int, t_start real, t_end real);
        create table if not exists videos(id text primary key, product_id text, template_id text, path text, caption text);
        create table if not exists jobs(id text primary key, kind text, status text, error text, ref text);
        create table if not exists settings(k text primary key, v text);
        """)
init()

def get_prompt():
    r = q("select v from settings where k='prompt'", one=True)
    return r["v"] if r else DEFAULT_PROMPT

def save_upload(f: UploadFile, sub: str) -> str:
    d = DATA / sub; d.mkdir(exist_ok=True)
    p = d / f"{uuid.uuid4().hex}{Path(f.filename or 'x').suffix.lower()}"
    with open(p, "wb") as o: shutil.copyfileobj(f.file, o)
    return str(p.relative_to(DATA))

def run_job(job_id, fn, *a):
    x("update jobs set status='berjalan' where id=?", (job_id,))
    try:
        fn(*a); x("update jobs set status='selesai' where id=?", (job_id,))
    except Exception as e:
        traceback.print_exc()
        x("update jobs set status='gagal', error=? where id=?", (str(e)[:500], job_id))

def new_job(kind, ref):
    jid = uuid.uuid4().hex[:10]
    x("insert into jobs(id,kind,status,ref) values(?,?,?,?)", (jid, kind, "antre", ref)); return jid

# ---------- AI image ----------
def gen_image(pid, prompt):
    p = q("select * from products where id=?", (pid,), one=True)
    if not KEY: raise RuntimeError("OPENAI_API_KEY belum diatur di backend/.env")
    src = DATA / p["photo"]
    with open(src, "rb") as fh:
        r = httpx.post(f"{BASE}/images/edits", headers={"Authorization": f"Bearer {KEY}"},
                       data={"model": MODEL, "prompt": prompt, "size": "1024x1536"},
                       files={"image": (src.name, fh)}, timeout=300)
    if r.status_code != 200: raise RuntimeError(f"API gambar {r.status_code}: {r.text[:300]}")
    item = r.json()["data"][0]
    out = DATA / "ai"; out.mkdir(exist_ok=True)
    dst = out / f"{uuid.uuid4().hex}.png"
    if item.get("b64_json"): dst.write_bytes(base64.b64decode(item["b64_json"]))
    else: dst.write_bytes(httpx.get(item["url"], timeout=120).content)
    x("update products set ai_photo=?, ai_status='siap', approved=0 where id=?", (str(dst.relative_to(DATA)), pid))

# ---------- Render ----------
def render(pid, tid, vid):
    p = q("select * from products where id=?", (pid,), one=True)
    t = q("select * from templates where id=?", (tid,), one=True)
    img = DATA / (p["ai_photo"] if p["approved"] and p["ai_photo"] else p["photo"])
    out = DATA / "videos"; out.mkdir(exist_ok=True)
    dst = out / f"{vid}.mp4"
    end = t["t_end"] if t["t_end"] and t["t_end"] > 0 else 99999
    w, h = t["w"], t["h"]
    fc = (f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}[im];"
          f"[0:v][im]overlay={t['x']}:{t['y']}:enable='between(t,{t['t_start']},{end})':shortest=1[v]")
    cmd = ["ffmpeg", "-y", "-i", str(DATA / t["path"]), "-loop", "1", "-i", str(img),
           "-filter_complex", fc, "-map", "[v]", "-map", "0:a?", "-c:a", "copy",
           "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0: raise RuntimeError("FFmpeg: " + r.stderr[-400:])
    x("insert into videos(id,product_id,template_id,path,caption) values(?,?,?,?,?)",
      (vid, pid, tid, str(dst.relative_to(DATA)), p["name"]))

# ---------- API ----------
app = FastAPI(title="AI Affiliate Video Maker")

import secrets
from fastapi import Request
from fastapi.responses import Response
PW = os.getenv("APP_PASSWORD", "")

@app.middleware("http")
async def auth(request: Request, call_next):
    if PW:
        ok = False
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                user_pw = base64.b64decode(h[6:]).decode().split(":", 1)
                ok = len(user_pw) == 2 and secrets.compare_digest(user_pw[1], PW)
            except Exception:
                pass
        if not ok:
            return Response("Login diperlukan", 401, headers={"WWW-Authenticate": 'Basic realm="app"'})
    return await call_next(request)

@app.get("/api/status")
def status():
    return {"ai_image": bool(KEY), "ffmpeg": bool(shutil.which("ffmpeg")),
            "tiktok": "belum dikonfigurasi (Content Posting API belum disambungkan)"}

@app.get("/api/settings/prompt")
def prompt_get(): return {"prompt": get_prompt()}

class P(BaseModel): prompt: str
@app.put("/api/settings/prompt")
def prompt_set(b: P):
    x("insert or replace into settings(k,v) values('prompt',?)", (b.prompt,)); return {"ok": True}

@app.get("/api/products")
def products(): return q("select * from products order by rowid desc")

@app.post("/api/products")
def product_add(name: str = Form(...), price: str = Form(""), description: str = Form(""),
                link: str = Form(""), photo: UploadFile = File(...)):
    pid = uuid.uuid4().hex[:10]
    x("insert into products(id,name,price,description,link,photo) values(?,?,?,?,?,?)",
      (pid, name, price, description, link, save_upload(photo, "photos")))
    return {"id": pid}

@app.delete("/api/products/{pid}")
def product_del(pid: str): x("delete from products where id=?", (pid,)); return {"ok": True}

@app.post("/api/products/{pid}/generate")
def generate(pid: str, bg: BackgroundTasks, prompt: str = Form("")):
    if not q("select 1 from products where id=?", (pid,), one=True): raise HTTPException(404)
    x("update products set ai_status='proses' where id=?", (pid,))
    jid = new_job("gambar", pid)
    bg.add_task(run_job, jid, gen_image, pid, prompt.strip() or get_prompt()); return {"job": jid}

@app.post("/api/products/{pid}/approve")
def approve(pid: str): x("update products set approved=1 where id=?", (pid,)); return {"ok": True}

@app.get("/api/templates")
def templates(): return q("select * from templates order by rowid desc")

@app.post("/api/templates")
def template_add(name: str = Form(...), x_: int = Form(..., alias="x"), y: int = Form(...), w: int = Form(...), h: int = Form(...),
                 t_start: float = Form(0), t_end: float = Form(0), video: UploadFile = File(...)):
    tid = uuid.uuid4().hex[:10]
    x("insert into templates values(?,?,?,?,?,?,?,?,?)", (tid, name, save_upload(video, "templates"), x_, y, w, h, t_start, t_end))
    return {"id": tid}

class V(BaseModel):
    template_id: str
    product_ids: list[str]

@app.post("/api/videos")
def video_make(b: V, bg: BackgroundTasks):
    jobs = []
    for pid in b.product_ids:  # mode batch = beberapa produk sekaligus
        vid = uuid.uuid4().hex[:10]; jid = new_job("video", vid)
        bg.add_task(run_job, jid, render, pid, b.template_id, vid); jobs.append(jid)
    return {"jobs": jobs}

@app.get("/api/videos")
def videos(): return q("select v.*, p.name as product from videos v left join products p on p.id=v.product_id order by v.rowid desc")

@app.get("/api/jobs")
def jobs(): return q("select * from jobs order by rowid desc limit 30")

@app.get("/files/{path:path}")
def files(path: str):
    f = (DATA / path).resolve()
    if DATA.resolve() not in f.parents or not f.is_file(): raise HTTPException(404)
    return FileResponse(f)

app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
