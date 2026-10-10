#!/usr/bin/env python3
"""
Manhwa Reader: a tiny local webtoon reader for CBZ/ZIP files.

Library layout (one folder per series, one CBZ/ZIP per chapter):

    library/
      Solo Leveling/
        cover.jpg            (optional; otherwise page 1 of chapter 1 is used)
        Chapter 001.cbz
        Chapter 002.cbz
      Tower of God/
        ch1.cbz
        ch2.cbz

Run:
    python manhwa_reader.py                      # uses ./library next to this file
    python manhwa_reader.py --lib "D:\\Manhwa"    # custom folder
    python manhwa_reader.py --lan                # also reachable from phone/tablet on same Wi-Fi

No packages needed (Python 3.8+). Optional: `pip install pillow` makes cover thumbnails smaller/faster.
"""
import argparse
import io
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from PIL import Image  # optional
except Exception:  # pragma: no cover
    Image = None

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
CH_EXT = {".cbz", ".zip"}
MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif",
}
COVER_NAMES = {"cover", "folder", "poster", "thumbnail"}

ROOT = ""
PROGRESS_FILE = ""
PROGRESS = {}
_plock = threading.Lock()
_clock = threading.Lock()
_page_cache = {}
_thumbs = {}


class HttpError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg or str(code))
        self.code = code


# ----------------------------------------------------------------- helpers

def nat_key(s):
    return [int(t) if i % 2 else t.lower() for i, t in enumerate(re.split(r"(\d+)", s))]


def safe(*parts):
    """Join names under ROOT. Names must be plain file/folder names (no separators)."""
    for p in parts:
        if not p or "/" in p or "\\" in p or p in (".", "..") or "\x00" in p:
            raise HttpError(404, "not found")
    return os.path.join(ROOT, *parts)


def image_size(data):
    """Read (width, height) from the header bytes of PNG/JPEG/GIF/WebP. None if unknown."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if kind == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            return None
        if data[:2] == b"\xff\xd8":
            i, n = 2, len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                m = data[i + 1]
                if m == 0xFF:
                    i += 1
                    continue
                if m == 0x01 or 0xD0 <= m <= 0xD8:
                    i += 2
                    continue
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except Exception:
        pass
    return None


def list_series():
    out = []
    with os.scandir(ROOT) as it:
        for e in it:
            if e.is_dir() and not e.name.startswith("."):
                out.append(e.name)
    return sorted(out, key=nat_key)


def list_chapters(series):
    d = safe(series)
    if not os.path.isdir(d):
        raise HttpError(404, "series not found")
    names = []
    with os.scandir(d) as it:
        for f in it:
            if f.is_file() and not f.name.startswith(".") and os.path.splitext(f.name)[1].lower() in CH_EXT:
                names.append(f.name)
    return sorted(names, key=nat_key)


def chapter_pages(series, chapter):
    """Sorted image list of a chapter with dimensions (cached, so the reader can reserve space)."""
    path = safe(series, chapter)
    try:
        st = os.stat(path)
    except OSError:
        raise HttpError(404, "chapter not found")
    key = (path, st.st_mtime_ns)
    with _clock:
        hit = _page_cache.get(key)
    if hit is not None:
        return hit
    pages = []
    with zipfile.ZipFile(path) as zf:
        infos = []
        for i in zf.infolist():
            if i.is_dir():
                continue
            parts = i.filename.replace("\\", "/").split("/")
            if any(p.startswith(".") or p == "__MACOSX" for p in parts):
                continue
            if os.path.splitext(i.filename)[1].lower() in IMG_EXT:
                infos.append(i)
        infos.sort(key=lambda i: nat_key(i.filename))
        for i in infos:
            w = h = 0
            try:
                with zf.open(i) as f:
                    head = f.read(65536)
                size = image_size(head)
                if size is None and i.file_size > 65536:
                    with zf.open(i) as f:
                        size = image_size(f.read())
                if size and size[0] > 0 and size[1] > 0:
                    w, h = size
            except Exception:
                pass
            pages.append({"name": i.filename, "w": w, "h": h})
    with _clock:
        if len(_page_cache) > 300:
            _page_cache.clear()
        _page_cache[key] = pages
    return pages


def read_page(series, chapter, idx):
    pages = chapter_pages(series, chapter)
    if not 0 <= idx < len(pages):
        raise HttpError(404, "page not found")
    name = pages[idx]["name"]
    with zipfile.ZipFile(safe(series, chapter)) as zf:
        return zf.read(name), MIME.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def make_thumb(data):
    if Image is None:
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = im.size
        if h > w * 1.5:  # tall webtoon strip: keep the top 2:3 part
            im = im.crop((0, 0, w, int(w * 1.5)))
        im.thumbnail((360, 540))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        return buf.getvalue()
    except Exception:
        return None


def cover_for(series):
    d = safe(series)
    key = (series, int(os.stat(d).st_mtime))
    with _clock:
        if key in _thumbs:
            return _thumbs[key]
    data = ctype = None
    for f in sorted(os.listdir(d), key=nat_key):
        base, ext = os.path.splitext(f)
        if base.lower() in COVER_NAMES and ext.lower() in IMG_EXT:
            with open(os.path.join(d, f), "rb") as fh:
                data, ctype = fh.read(), MIME[ext.lower()]
            break
    if data is None:
        chs = list_chapters(series)
        if chs:
            pages = chapter_pages(series, chs[0])
            if pages:
                data, ctype = read_page(series, chs[0], 0)
    if data is None:
        raise HttpError(404, "no cover")
    t = make_thumb(data)
    result = (t, "image/jpeg") if t else (data, ctype)
    with _clock:
        if len(_thumbs) > 500:
            _thumbs.clear()
        _thumbs[key] = result
    return result


# ---------------------------------------------------------------- progress

def load_progress():
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def set_progress(series, chapter, page, offset, done):
    with _plock:
        p = PROGRESS.setdefault(series, {"read": []})
        p.update(chapter=chapter, page=page, offset=offset, done=bool(done), t=int(time.time()))
        if done and chapter not in p["read"]:
            p["read"].append(chapter)
        tmp = PROGRESS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(PROGRESS, f, ensure_ascii=False)
        os.replace(tmp, PROGRESS_FILE)


# --------------------------------------------------------------------- API

def api_library():
    items = []
    for s in list_series():
        chs = list_chapters(s)
        if not chs:
            continue
        p = PROGRESS.get(s) or {}
        items.append({
            "name": s,
            "chapters": len(chs),
            "read": len([c for c in p.get("read", []) if c in chs]),
            "t": p.get("t", 0) if p.get("chapter") else 0,
            "v": int(os.stat(safe(s)).st_mtime),
        })
    return items


def api_series(s):
    chs = list_chapters(s)
    p = PROGRESS.get(s) or {}
    read = set(p.get("read", []))
    last = {"chapter": p["chapter"], "done": bool(p.get("done"))} if p.get("chapter") in chs else None
    return {
        "name": s,
        "chapters": [{"file": c, "title": os.path.splitext(c)[0], "read": c in read} for c in chs],
        "last": last,
    }


def api_chapter(s, c):
    chs = list_chapters(s)
    if c not in chs:
        raise HttpError(404, "chapter not found")
    k = chs.index(c)
    pages = chapter_pages(s, c)
    p = PROGRESS.get(s) or {}
    pos = None
    if p.get("chapter") == c and not p.get("done"):
        pos = {"page": p.get("page", 0), "offset": p.get("offset", 0)}
    return {
        "title": os.path.splitext(c)[0],
        "pages": [[x["w"], x["h"]] for x in pages],
        "v": int(os.stat(safe(s, c)).st_mtime),
        "prev": chs[k - 1] if k > 0 else None,
        "next": chs[k + 1] if k + 1 < len(chs) else None,
        "pos": pos,
    }


# ------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ManhwaReader"

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", ctype="application/octet-stream", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _guard(self, fn):
        try:
            fn()
        except HttpError as e:
            self._json({"error": str(e)}, e.code)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except zipfile.BadZipFile:
            self._json({"error": "This file is not a valid CBZ/ZIP."}, 500)
        except Exception as e:  # keep the server alive no matter what
            try:
                self._json({"error": repr(e)}, 500)
            except Exception:
                pass

    def do_GET(self):
        self._guard(self._get)

    do_HEAD = do_GET

    def do_POST(self):
        self._guard(self._post)

    def _get(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p in ("/", "/index.html"):
            return self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8",
                              {"Cache-Control": "no-cache"})
        if p == "/api/library":
            return self._json(api_library())
        if p == "/api/series":
            return self._json(api_series(q.get("s", "")))
        if p == "/api/chapter":
            return self._json(api_chapter(q.get("s", ""), q.get("c", "")))
        if p == "/img":
            try:
                idx = int(q.get("i", "-1"))
            except ValueError:
                raise HttpError(400, "bad index")
            data, ctype = read_page(q.get("s", ""), q.get("c", ""), idx)
            return self._send(200, data, ctype, {"Cache-Control": "public, max-age=31536000, immutable"})
        if p == "/cover":
            data, ctype = cover_for(q.get("s", ""))
            return self._send(200, data, ctype, {"Cache-Control": "public, max-age=86400"})
        raise HttpError(404, "not found")

    def _post(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if urlparse(self.path).path != "/api/progress":
            raise HttpError(404, "not found")
        try:
            d = json.loads(raw or b"{}")
            s, c = str(d["s"]), str(d["c"])
            page, offset, done = int(d.get("page", 0)), float(d.get("offset", 0)), bool(d.get("done"))
        except Exception:
            raise HttpError(400, "bad request")
        if c not in list_chapters(s):
            raise HttpError(404, "chapter not found")
        set_progress(s, c, max(0, page), min(1.0, max(0.0, offset)), done)
        self._json({"ok": True})


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def main():
    global ROOT, PROGRESS_FILE, PROGRESS
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Local webtoon reader for CBZ/ZIP files")
    ap.add_argument("--lib", default=os.path.join(here, "library"), help="library folder (default: ./library)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--lan", action="store_true", help="allow phones/tablets on your network to connect")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    a = ap.parse_args()

    ROOT = os.path.abspath(a.lib)
    os.makedirs(ROOT, exist_ok=True)
    PROGRESS_FILE = os.path.join(here, "progress.json")
    PROGRESS = load_progress()

    ThreadingHTTPServer.request_queue_size = 64
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    try:
        srv = ThreadingHTTPServer((host, a.port), Handler)
    except OSError as e:
        sys.exit(f"Could not start on port {a.port}: {e}\nTry another one: --port 8080")

    url = f"http://localhost:{a.port}"
    print(f"Library : {ROOT}")
    print(f"Open    : {url}")
    if a.lan:
        ip = lan_ip()
        if ip:
            print(f"Phone   : http://{ip}:{a.port}")
    print("Press Ctrl+C to stop.")
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Manhwa Reader</title>
<style>
:root{--bg:#14161c;--sf:#1d2029;--ln:#2b2f3b;--tx:#e8e5dd;--mu:#8d92a1;--ac:#9db0ff;color-scheme:dark}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--bg)}
body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--tx);background:var(--bg)}
body.reading{background:#0b0c0f}
a{color:inherit;text-decoration:none}
h1,h2,.ct{font-family:"Iowan Old Style",Palatino,"Palatino Linotype",Georgia,serif}
a:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 60px}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px}
h1{font-size:28px;margin:0;font-weight:600;letter-spacing:-.01em}
h2{font-size:18px;margin:28px 0 12px;font-weight:600}
.muted{color:var(--mu);font-weight:400}
.center{text-align:center;padding:80px 0}
input[type=search]{background:var(--sf);border:1px solid var(--ln);color:var(--tx);padding:9px 12px;border-radius:8px;width:min(100%,260px);font:inherit}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:20px 14px}
.row{display:flex;gap:14px;overflow-x:auto;padding-bottom:8px}
.row .card{flex:0 0 140px}
.cv{aspect-ratio:2/3;background:var(--sf);border-radius:6px;overflow:hidden}
.cv img{width:100%;height:100%;object-fit:cover;object-position:top;display:block}
.ct{margin-top:8px;font-size:15px;line-height:1.25;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.cm{font-size:12.5px;color:var(--mu);margin-top:2px}
.card:hover .ct{color:var(--ac)}
.back{color:var(--mu);font-size:14px}
.back:hover{color:var(--tx)}
.sh{display:flex;gap:18px;margin:16px 0 6px;align-items:flex-end}
.sh .cv{width:110px;flex:none}
.acts{display:flex;gap:10px;margin:18px 0 22px;flex-wrap:wrap}
.btn{display:inline-block;background:var(--ac);color:#10131c;border:1px solid var(--ac);border-radius:8px;padding:10px 16px;font:600 14px system-ui,sans-serif;cursor:pointer}
.btn.ghost{background:transparent;color:var(--tx);border-color:var(--ln)}
.chs{border-top:1px solid var(--ln)}
.ch{display:flex;justify-content:space-between;gap:12px;padding:13px 6px;border-bottom:1px solid var(--ln)}
.ch:hover{background:var(--sf)}
.ch.read{color:var(--mu)}
.ch.cur{box-shadow:inset 3px 0 0 var(--ac);padding-left:14px}

#bar{position:fixed;inset:0 0 auto 0;z-index:10;display:flex;align-items:center;gap:4px;padding:calc(6px + env(safe-area-inset-top)) 10px 6px;background:rgba(20,22,28,.94);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--ln);transition:transform .2s}
#bar.off{transform:translateY(-115%)}
.bt{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px;padding:0 6px}
.b{width:38px;height:38px;display:grid;place-items:center;border-radius:8px;background:none;border:0;color:var(--tx);font:20px/1 system-ui,sans-serif;cursor:pointer}
.b:hover{background:var(--sf)}
.b.dis{opacity:.3;pointer-events:none}
#panel{position:absolute;top:100%;right:10px;margin-top:6px;background:var(--sf);border:1px solid var(--ln);border-radius:10px;padding:12px 14px;display:flex;align-items:center;gap:10px;font-size:14px}
#panel input{width:150px;accent-color:var(--ac)}
#wv{width:56px;color:var(--mu);text-align:right}
#strip{max-width:var(--w,800px);margin:0 auto;-webkit-tap-highlight-color:transparent}
.pg{position:relative;width:100%;background:#15171c}
.pg img{position:absolute;inset:0;width:100%;height:100%;display:block}
.pg.nodim{min-height:70vh}
.pg.nodim img{position:static;height:auto}
.pg.err{min-height:140px;cursor:pointer}
.pg.err::after{content:"Image failed to load. Tap to retry.";position:absolute;inset:0;display:grid;place-items:center;color:var(--mu);font-size:14px}
.end{text-align:center;padding:64px 16px 140px;color:var(--mu)}
.end .btn{margin-top:14px}
</style>
</head>
<body>
<div id="bar" hidden></div>
<div id="app"></div>
<script>
const $=(s,e=document)=>e.querySelector(s);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const qs=o=>new URLSearchParams(o).toString();
const getJSON=async u=>{const r=await fetch(u);if(!r.ok){let m=r.status;try{m=(await r.json()).error||m}catch(_){}throw new Error(m)}return r.json()};
const app=$('#app'),bar=$('#bar');
let cleanup=null,nav=0;

const fail=e=>{app.innerHTML=`<div class="wrap"><a class="back" href="#/">&larr; Library</a><h2>Couldn't load this page</h2><p class="muted">${esc(e.message||e)}</p></div>`};

function route(){
  if(cleanup){cleanup();cleanup=null}
  const my=++nav;
  const [path,query='']=location.hash.slice(1).split('?');
  const p=new URLSearchParams(query);
  const reading=path==='/read';
  document.body.classList.toggle('reading',reading);
  bar.hidden=true;bar.classList.remove('off');
  scrollTo(0,0);
  if(path==='/series')return seriesView(p.get('s'),my);
  if(reading)return readView(p.get('s'),p.get('c'),my);
  return libraryView(my);
}
addEventListener('hashchange',route);

/* ---------- Library ---------- */
async function libraryView(my){
  document.title='Manhwa Reader';
  app.innerHTML='<div class="center muted">Loading…</div>';
  let lib;try{lib=await getJSON('/api/library')}catch(e){return fail(e)}
  if(my!==nav)return;
  const cont=lib.filter(x=>x.t).sort((a,b)=>b.t-a.t).slice(0,12);
  const card=x=>`<a class="card" href="#/series?${qs({s:x.name})}"><div class="cv"><img loading="lazy" decoding="async" alt="" src="/cover?${qs({s:x.name,v:x.v})}" onerror="this.remove()"></div><div class="ct">${esc(x.name)}</div><div class="cm">${x.read} of ${x.chapters} read</div></a>`;
  app.innerHTML=`<div class="wrap">
    <div class="top"><h1>Library</h1>${lib.length?'<input id="q" type="search" placeholder="Search series" aria-label="Search series">':''}</div>
    ${cont.length?`<h2>Continue reading</h2><div class="row">${cont.map(card).join('')}</div>`:''}
    ${lib.length?`<h2>All series <span class="muted">${lib.length}</span></h2><div class="grid" id="grid">${lib.map(card).join('')}</div>`
      :'<h2>Nothing here yet</h2><p class="muted">Add a folder per series to your library folder, with one .cbz or .zip file per chapter, then refresh.</p>'}
  </div>`;
  const q=$('#q');
  if(q)q.oninput=e=>{const t=e.target.value.toLowerCase();for(const c of $('#grid').children)c.hidden=!$('.ct',c).textContent.toLowerCase().includes(t)};
}

/* ---------- Series ---------- */
async function seriesView(s,my){
  document.title=s+' – Manhwa Reader';
  app.innerHTML='<div class="center muted">Loading…</div>';
  let d;try{d=await getJSON('/api/series?'+qs({s}))}catch(e){return fail(e)}
  if(my!==nav)return;
  const ch=d.chapters;let desc=true;
  let target=ch[0],label='Start reading';
  if(d.last){
    const i=ch.findIndex(c=>c.file===d.last.chapter);
    if(i>=0){
      if(d.last.done&&i+1<ch.length){target=ch[i+1];label='Continue with '+ch[i+1].title}
      else{target=ch[i];label=d.last.done?'Read again':'Continue '+ch[i].title}
    }
  }
  app.innerHTML=`<div class="wrap"><a class="back" href="#/">&larr; Library</a>
    <div class="sh"><div class="cv"><img alt="" src="/cover?${qs({s})}" onerror="this.remove()"></div>
    <div><h1>${esc(d.name)}</h1><div class="muted">${ch.length} chapters</div></div></div>
    <div class="acts">${target?`<a class="btn" href="#/read?${qs({s,c:target.file})}">${esc(label)}</a>`:''}<button class="btn ghost" id="sort"></button></div>
    <div class="chs" id="list"></div></div>`;
  const draw=()=>{
    $('#sort').textContent=desc?'Newest first':'Oldest first';
    const list=desc?[...ch].reverse():ch;
    $('#list').innerHTML=list.map(c=>`<a class="ch${c.read?' read':''}${d.last&&d.last.chapter===c.file?' cur':''}" href="#/read?${qs({s,c:c.file})}"><span>${esc(c.title)}</span><span>${c.read?'✓':''}</span></a>`).join('');
  };
  $('#sort').onclick=()=>{desc=!desc;draw()};
  draw();
}

/* ---------- Reader ---------- */
async function readView(s,c,my){
  app.innerHTML='<div class="center muted">Loading…</div>';
  let d;try{d=await getJSON('/api/chapter?'+qs({s,c}))}catch(e){return fail(e)}
  if(my!==nav)return;
  document.title=d.title+' – '+s;
  const link=f=>'#/read?'+qs({s,c:f});
  const seriesHref='#/series?'+qs({s});
  let W=localStorage.getItem('mr_w')||'800';

  bar.innerHTML=`<a class="b" href="${seriesHref}" title="Chapters" aria-label="Chapters">&larr;</a>
    <div class="bt">${esc(s)} · ${esc(d.title)}</div>
    <a class="b${d.prev?'':' dis'}" ${d.prev?`href="${link(d.prev)}"`:''} title="Previous chapter" aria-label="Previous chapter">&lsaquo;</a>
    <a class="b${d.next?'':' dis'}" ${d.next?`href="${link(d.next)}"`:''} title="Next chapter" aria-label="Next chapter">&rsaquo;</a>
    <button class="b" id="gear" title="Settings" aria-label="Settings">&#9881;</button>
    <div id="panel" hidden><label for="wr">Width</label><input id="wr" type="range" min="300" max="1400" step="50" value="${W}"><span id="wv">${W}px</span></div>`;
  bar.hidden=false;

  if(!d.pages.length){
    app.innerHTML=`<div class="end"><p>No images were found in this chapter.</p><a class="btn" href="${seriesHref}">Back to chapters</a></div>`;
    cleanup=()=>{};return;
  }

  app.innerHTML=`<div id="strip" style="--w:${W}px">${d.pages.map(([w,h],i)=>
    `<div class="pg${w?'':' nodim'}"${w?` style="aspect-ratio:${w}/${h}"`:''}><img alt="" decoding="async" data-src="/img?${qs({s,c,i,v:d.v})}"></div>`).join('')}
    <div class="end"><p>End of ${esc(d.title)}</p>${d.next?`<a class="btn" href="${link(d.next)}">Next chapter</a>`:`<a class="btn" href="${seriesHref}">Back to chapters</a>`}</div></div>`;

  const strip=$('#strip'),pages=[...strip.querySelectorAll('.pg')];

  /* Lazy load ahead, release images that are far away (keeps memory flat on huge chapters). */
  const loadIO=new IntersectionObserver(es=>{for(const e of es)if(e.isIntersecting){const im=e.target.firstElementChild;if(!im.getAttribute('src'))im.src=im.dataset.src}},{rootMargin:'250% 0px'});
  const dropIO=new IntersectionObserver(es=>{for(const e of es)if(!e.isIntersecting){const im=e.target.firstElementChild;if(im.getAttribute('src'))im.removeAttribute('src')}},{rootMargin:'1000% 0px'});
  pages.forEach(p=>{loadIO.observe(p);dropIO.observe(p)});

  strip.addEventListener('load',e=>{
    const im=e.target,pg=im.parentNode;
    if(pg.classList.contains('nodim')&&im.naturalWidth){pg.style.aspectRatio=im.naturalWidth+'/'+im.naturalHeight;pg.classList.remove('nodim')}
    pg.classList.remove('err');
  },true);
  strip.addEventListener('error',e=>{const im=e.target;if(im.tagName==='IMG'&&im.getAttribute('src'))im.parentNode.classList.add('err')},true);
  strip.addEventListener('click',e=>{
    const pg=e.target.closest('.pg.err');
    if(pg){const im=pg.firstElementChild,u=im.dataset.src;pg.classList.remove('err');im.removeAttribute('src');setTimeout(()=>{im.src=u+'&r='+Date.now()},50);return}
    if(e.target.closest('a'))return;
    $('#panel').hidden=true;bar.classList.toggle('off');
  });

  /* Position helpers: page index + fraction scrolled into that page (layout is stable thanks to reserved heights). */
  const cur=()=>{let lo=0,hi=pages.length-1;while(lo<hi){const m=(lo+hi)>>1;if(pages[m].getBoundingClientRect().bottom>1)hi=m;else lo=m+1}const r=pages[lo].getBoundingClientRect();return{page:lo,offset:Math.min(1,Math.max(0,-r.top/Math.max(1,r.height)))}};
  const goto=(p,o)=>{const pg=pages[Math.min(p,pages.length-1)];if(!pg)return;const r=pg.getBoundingClientRect();scrollTo(0,scrollY+r.top+o*r.height)};
  if(d.pos)goto(d.pos.page,d.pos.offset);

  /* Save progress (debounced; also on tab hide / leaving) */
  let timer=null,last='';
  const atEnd=()=>innerHeight+scrollY>=document.documentElement.scrollHeight-80;
  const flush=beacon=>{
    clearTimeout(timer);timer=null;
    const p=cur(),b=JSON.stringify({s,c,page:p.page,offset:+p.offset.toFixed(4),done:atEnd()});
    if(b===last)return;last=b;
    if(beacon&&navigator.sendBeacon)navigator.sendBeacon('/api/progress',new Blob([b],{type:'application/json'}));
    else fetch('/api/progress',{method:'POST',body:b,keepalive:true}).catch(()=>{});
  };
  timer=setTimeout(()=>flush(false),1000);

  /* Toolbar hides while scrolling down, returns when scrolling up */
  let ly=scrollY,tick=false;
  const onScroll=()=>{
    if(tick)return;tick=true;
    requestAnimationFrame(()=>{
      tick=false;const y=scrollY,dy=y-ly;
      if(Math.abs(dy)>10){if(dy>0&&y>80){bar.classList.add('off');$('#panel').hidden=true}else if(dy<0)bar.classList.remove('off');ly=y}
      if(y<80)bar.classList.remove('off');
      clearTimeout(timer);timer=setTimeout(()=>flush(false),700);
    });
  };
  const onHide=()=>{if(document.visibilityState==='hidden')flush(true)};
  addEventListener('scroll',onScroll,{passive:true});
  document.addEventListener('visibilitychange',onHide);
  addEventListener('pagehide',onHide);

  $('#gear').onclick=()=>{const p=$('#panel');p.hidden=!p.hidden};
  $('#wr').oninput=e=>{
    const a=cur();W=e.target.value;
    strip.style.setProperty('--w',W+'px');$('#wv').textContent=W+'px';
    try{localStorage.setItem('mr_w',W)}catch(_){}
    goto(a.page,a.offset);
  };

  cleanup=()=>{
    flush(false);
    removeEventListener('scroll',onScroll);
    document.removeEventListener('visibilitychange',onHide);
    removeEventListener('pagehide',onHide);
    loadIO.disconnect();dropIO.disconnect();
    clearTimeout(timer);
  };
}

route();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
