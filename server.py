import os
import re
import time
import shutil
import base64
import threading
import subprocess
import tempfile

from flask import Flask, Response, request, jsonify, send_file, abort
import yt_dlp
import requests

app = Flask(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
MAX_CACHE_MB = int(os.environ.get("MAX_CACHE_MB", 500))
PORT = int(os.environ.get("PORT", 5005))

os.makedirs(CACHE_DIR, exist_ok=True)

# Detect ffmpeg path
FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    winget_ffmpeg = os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe")
    if os.path.exists(winget_ffmpeg):
        FFMPEG_PATH = winget_ffmpeg

print(f"[INIT] ffmpeg disponible: {bool(FFMPEG_PATH)} ({FFMPEG_PATH})", flush=True)

# YouTube cookies desde variable de entorno
COOKIES_PATH = None
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")
if YOUTUBE_COOKIES:
    _cookie_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    _cookie_file.write(YOUTUBE_COOKIES)
    _cookie_file.close()
    COOKIES_PATH = _cookie_file.name
    print(f"[INIT] Cookies de YouTube cargadas ({len(YOUTUBE_COOKIES)} chars)", flush=True)
else:
    print("[INIT] Sin cookies de YouTube (YOUTUBE_COOKIES no definida)", flush=True)

_locks = {}
_locks_guard = threading.Lock()

_url_cache = {}
_url_cache_lock = threading.Lock()
URL_TTL = 55 * 60  # 55 minutes


def lock_for(video_id):
    with _locks_guard:
        lock = _locks.setdefault(video_id, threading.Lock())
    return lock


def ts():
    return time.strftime("[%H:%M:%S]")


def cache_path(video_id):
    return os.path.join(CACHE_DIR, f"{video_id}.mp3")


def is_url(text):
    text = text.strip().strip("\"'")
    return bool(re.match(r"^https?://", text)) or text.startswith("www.") or text.startswith("youtu.be/")


def sanitize_query(q):
    q = q.strip().strip("\"'")
    if q.startswith("www.") or q.startswith("youtu.be/"):
        q = "https://" + q
    return q


def ydl_opts_base(player_client=None):
    opts = {
        "quiet": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "no_warnings": True,
    }
    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": player_client}}
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH
    return opts


# Estrategias: cada una con distintos player clients, se prueba en orden
PLAYER_STRATEGIES = [
    ["tv"],
    ["ios"],
    ["mweb"],
    ["web"],
    ["android"],
    ["tv_embedded"],
    None,  # sin restriccion (default de yt-dlp)
]


def resolve_info(query):
    query = sanitize_query(query)
    target = query if is_url(query) else f"ytsearch1:{query}"
    last_error = None
    for strategy in PLAYER_STRATEGIES:
        try:
            with yt_dlp.YoutubeDL(ydl_opts_base(strategy)) as ydl:
                info = ydl.extract_info(target, download=False)
            if "entries" in info:
                entries = [e for e in info["entries"] if e]
                if not entries:
                    raise RuntimeError("Sin resultados para esa busqueda")
                info = entries[0]
            return info
        except Exception as e:
            last_error = e
            print(f"{ts()} [RESOLVE] Estrategia {strategy} fallo: {e}", flush=True)
    raise last_error


def get_stream_url(video_id):
    with _url_cache_lock:
        entry = _url_cache.get(video_id)
        if entry and entry["expires_at"] > time.time():
            return entry["url"]

    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    last_error = None
    for strategy in PLAYER_STRATEGIES:
        try:
            with yt_dlp.YoutubeDL(ydl_opts_base(strategy)) as ydl:
                info = ydl.extract_info(watch_url, download=False)
            stream_url = info.get("url")
            if stream_url:
                with _url_cache_lock:
                    _url_cache[video_id] = {
                        "url": stream_url,
                        "expires_at": time.time() + URL_TTL,
                    }
                return stream_url
            last_error = RuntimeError("No se obtuvo stream URL")
        except Exception as e:
            last_error = e
            print(f"{ts()} [STREAM] Estrategia {strategy} fallo: {e}", flush=True)
    raise last_error


def enforce_cache_limit():
    files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith(".mp3")]
    total = sum(os.path.getsize(f) for f in files)
    limit = MAX_CACHE_MB * 1024 * 1024
    if total <= limit:
        return
    files.sort(key=lambda f: os.path.getmtime(f))
    for f in files:
        if total <= limit:
            break
        total -= os.path.getsize(f)
        try:
            os.remove(f)
        except Exception:
            pass


def ensure_cached(video_id):
    path = cache_path(video_id)
    with lock_for(video_id):
        if os.path.exists(path) and os.path.getsize(path) > 1024:
            os.utime(path, None)
            return path

        direct_url = get_stream_url(video_id)
        tmp_path = os.path.join(CACHE_DIR, f"{video_id}_temp.mp3")

        if FFMPEG_PATH:
            print(f"{ts()} [CACHE] Convirtiendo {video_id} con ffmpeg...", flush=True)
            ffmpeg_tmp = os.path.abspath(tmp_path).replace("\\", "/")
            result = subprocess.run(
                [FFMPEG_PATH, "-y", "-i", direct_url, "-vn", "-acodec", "libmp3lame", "-b:a", "128k", "-f", "mp3", ffmpeg_tmp],
                capture_output=True,
            )
            if result.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1024:
                os.replace(tmp_path, path)
                enforce_cache_limit()
                print(f"{ts()} [CACHE] Guardado en disco: {video_id}.mp3", flush=True)
                return path
            else:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                err_msg = result.stderr.decode(errors="ignore")[-200:]
                print(f"{ts()} [CACHE] ffmpeg fallo ({err_msg}), usando stream proxy", flush=True)

        return None


# ─── Rutas ──────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    import yt_dlp
    return jsonify(
        ok=True,
        ffmpeg=bool(FFMPEG_PATH),
        ytdlp_version=yt_dlp.version.__version__,
        ts=int(time.time()),
    )


@app.route("/resolve", methods=["GET", "POST"])
def resolve():
    json_data = request.get_json(silent=True) or {}
    raw = (
        request.args.get("q")
        or request.args.get("url")
        or request.args.get("link")
        or request.args.get("query")
        or json_data.get("q")
        or json_data.get("url")
        or json_data.get("link")
        or json_data.get("query")
        or request.form.get("q")
        or request.form.get("url")
        or ""
    )
    query = sanitize_query(raw)

    print(f"{ts()} [RESOLVE] Consulta recibida: {query!r}", flush=True)

    if not query:
        return jsonify(error="Falta el parametro q"), 400

    try:
        info = resolve_info(query)
    except Exception as e:
        print(f"{ts()} [RESOLVE] Error yt-dlp: {e}", flush=True)
        return jsonify(error=str(e)), 502

    video_id = info.get("id")
    print(f"{ts()} [RESOLVE] OK -> {video_id} | {info.get('title', '?')}", flush=True)

    # Pre-fetch thumbnail and encode as data URI for instant offline rendering in CEF
    thumb_data = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    try:
        r = requests.get(thumb_data, timeout=5)
        if r.status_code == 200:
            b64 = base64.b64encode(r.content).decode("ascii")
            thumb_data = f"data:image/jpeg;base64,{b64}"
            print(f"{ts()} [RESOLVE] Thumbnail base64 generado ({len(b64)} chars)", flush=True)
    except Exception as err:
        print(f"{ts()} [RESOLVE] Thumbnail error: {err}", flush=True)

    return jsonify(
        id=video_id,
        title=info.get("title"),
        artist=info.get("uploader"),
        thumbnail=thumb_data,
        duration=info.get("duration") or 0,
    )


@app.route("/thumbnail/<video_id>")
def thumbnail(video_id):
    if not re.match(r"^[a-zA-Z0-9_\-]{6,25}$", video_id):
        abort(400)
    yt_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
    try:
        r = requests.get(yt_url, timeout=6)
        if r.status_code == 200:
            resp = Response(r.content, mimetype="image/jpeg")
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
    except Exception as e:
        print(f"{ts()} [THUMBNAIL] Error: {e}", flush=True)
    abort(404)


@app.route("/warm/<video_id>")
def warm(video_id):
    if not re.match(r"^[a-zA-Z0-9_\-]{6,25}$", video_id):
        return jsonify(error="ID de video invalido"), 400

    print(f"{ts()} [WARM] Preparando {video_id}", flush=True)
    try:
        # Si hay ffmpeg, intentamos cachear en background/segundo plano o inmediato
        # Pero retornamos ready de inmediato para no bloquear a MTA
        threading.Thread(target=ensure_cached, args=(video_id,), daemon=True).start()
        # Aseguramos que la URL directa de stream esté lista
        get_stream_url(video_id)
    except Exception as e:
        print(f"{ts()} [WARM] Error: {e}", flush=True)
        return jsonify(error=str(e)), 502

    print(f"{ts()} [WARM] Listo para reproducir: {video_id}", flush=True)
    return jsonify(ready=True, stream_url=f"/stream/{video_id}")


@app.route("/stream/<video_id>")
def stream(video_id):
    if not re.match(r"^[a-zA-Z0-9_\-]{6,25}$", video_id):
        abort(400)

    # 1. Si existe en cache de disco MP3, servir archivo local
    path = cache_path(video_id)
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        os.utime(path, None)
        return send_file(path, mimetype="audio/mpeg", conditional=True)

    # 2. Si no esta en disco, servir en streaming proxy directo desde YouTube
    try:
        yt_url = get_stream_url(video_id)
    except Exception as e:
        print(f"{ts()} [STREAM] Error al obtener URL: {e}", flush=True)
        abort(502)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    try:
        yt_resp = requests.get(yt_url, headers=headers, stream=True, timeout=12)
    except Exception as e:
        print(f"{ts()} [STREAM] Error en proxy: {e}", flush=True)
        abort(502)

    if yt_resp.status_code in (403, 410):
        with _url_cache_lock:
            _url_cache.pop(video_id, None)
        try:
            yt_url = get_stream_url(video_id)
            yt_resp = requests.get(yt_url, headers=headers, stream=True, timeout=12)
        except Exception:
            abort(502)

    content_type = yt_resp.headers.get("Content-Type", "audio/mpeg")

    def generate():
        try:
            for chunk in yt_resp.iter_content(chunk_size=32 * 1024):
                if chunk:
                    yield chunk
        finally:
            yt_resp.close()

    resp_headers = {"Content-Type": content_type, "Accept-Ranges": "bytes"}
    for h in ("Content-Length", "Content-Range"):
        if h in yt_resp.headers:
            resp_headers[h] = yt_resp.headers[h]

    return Response(generate(), status=yt_resp.status_code, headers=resp_headers)


if __name__ == "__main__":
    print(f"{ts()} Servidor de audio listo en puerto {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
