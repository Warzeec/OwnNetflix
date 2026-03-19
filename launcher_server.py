"""
OwnNetflix -- Multi-show / movie VLC launcher server
Launch with:  python launcher_server.py
Opens browser automatically at http://localhost:8899
"""

import os
import re
import json
import subprocess
import threading
import time
import webbrowser
import ctypes
import ctypes.wintypes as wt
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
from urllib.parse import urlparse, quote, parse_qs

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

MEDIA_ROOT = os.path.dirname(os.path.abspath(__file__))
VIDEO_EXT = (".mp4", ".mkv", ".avi")
DATA_DIR = os.path.join(MEDIA_ROOT, "data")
PROGRESS_FILE = os.path.join(DATA_DIR, "progress.json")
TMDB_CACHE_FILE = os.path.join(DATA_DIR, "tmdb_cache.json")
POSTER_DIR = os.path.join(DATA_DIR, "posters")
PORT = 8899

SKIP_DIRS = {".", ".git", ".idea", "data", "backup", "__pycache__", "node_modules"}

# ---------------------------------------------------------------------------
# Ensure data directories exist
# ---------------------------------------------------------------------------

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(POSTER_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# VLC detection
# ---------------------------------------------------------------------------

VLC_CANDIDATES = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
]
VLC_PATH = None
for _p in VLC_CANDIDATES:
    if os.path.isfile(_p):
        VLC_PATH = _p
        break

# ---------------------------------------------------------------------------
# .env loading (no external dependency)
# ---------------------------------------------------------------------------

def load_env():
    env_path = os.path.join(MEDIA_ROOT, ".env")
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()


load_env()
TMDB_TOKEN = os.environ.get("TMDB_TOKEN", "")

# ---------------------------------------------------------------------------
# Slugify helper
# ---------------------------------------------------------------------------

def slugify(name):
    """Convert a human-readable name to a URL-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---------------------------------------------------------------------------
# Episode number extraction
# ---------------------------------------------------------------------------

def get_episode_number(filename):
    match = re.search(r"S\d+E(\d+)", filename, re.IGNORECASE)
    return int(match.group(1)) if match else -1


def get_episode_code(filename):
    match = re.search(r"(S\d+E\d+)", filename, re.IGNORECASE)
    return match.group(1).upper() if match else None


# ---------------------------------------------------------------------------
# Subtitle track detection via ffprobe
# ---------------------------------------------------------------------------

def find_sub_track(filepath, lang_codes=("fre", "fra", "fr")):
    """Find the first non-forced subtitle track matching the given language.

    Returns the VLC sub-track index (0-based among subtitle tracks) or None.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "s", filepath],
            capture_output=True, text=True, timeout=10,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except Exception as exc:
        print(f"  ffprobe error: {exc}")
        return None

    sub_index = 0
    for s in streams:
        lang = s.get("tags", {}).get("language", "")
        forced = s.get("disposition", {}).get("forced", 0)
        if lang in lang_codes and not forced:
            return sub_index
        sub_index += 1

    return None


# ---------------------------------------------------------------------------
# Library scanning
# ---------------------------------------------------------------------------

_library_cache = None
_library_cache_lock = threading.Lock()


def scan_library(force=False):
    """Scan MEDIA_ROOT for shows and movies.

    Returns a dict keyed by slug::

        {
            "abbott-elementary": {
                "name": "Abbott Elementary",
                "type": "series",          # or "movie"
                "slug": "abbott-elementary",
                "seasons": {
                    1: {
                        "num": 1,
                        "folder": "<absolute path>",
                        "files": ["file1.mkv", ...],   # sorted by episode
                    },
                },
            },
        }

    Movies use season 0.
    """
    global _library_cache
    with _library_cache_lock:
        if _library_cache is not None and not force:
            return _library_cache

    library = {}

    # Detect loose video files in MEDIA_ROOT (movies not in a subfolder)
    loose_videos = sorted(
        [f for f in os.listdir(MEDIA_ROOT)
         if f.lower().endswith(VIDEO_EXT) and os.path.isfile(os.path.join(MEDIA_ROOT, f))],
    )
    for vfile in loose_videos:
        raw_name = re.split(
            r"[\.\s](?:\d{4}|720p|1080p|2160p|4K|BluRay|BRRip|WEBRip|WEB-DL|HDRip|x264|x265|HEVC|AAC|DTS|REMUX|COMPLETE)",
            os.path.splitext(vfile)[0],
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].replace(".", " ").strip()
        # Strip common torrent site tags like [ OxTorrent.com ]
        raw_name = re.sub(r"\[.*?\]", "", raw_name).strip()
        slug = slugify(raw_name)

        if slug not in library:
            library[slug] = {
                "name": raw_name,
                "type": "movie",
                "slug": slug,
                "seasons": {},
            }
        library[slug]["seasons"][0] = {
            "num": 0,
            "folder": MEDIA_ROOT,
            "files": [vfile],
        }

    for entry in os.listdir(MEDIA_ROOT):
        if entry in SKIP_DIRS or entry.startswith("."):
            continue
        folder_path = os.path.join(MEDIA_ROOT, entry)
        if not os.path.isdir(folder_path):
            continue

        # Collect video files in this folder
        videos = sorted(
            [f for f in os.listdir(folder_path) if f.lower().endswith(VIDEO_EXT)],
            key=get_episode_number,
        )
        if not videos:
            continue

        # Try to detect series pattern from folder name
        series_match = re.match(r"^(.+?)[\.\s]S(\d+)", entry, re.IGNORECASE)

        if series_match:
            # ---- Series ----
            raw_name = series_match.group(1).replace(".", " ").strip()
            season_num = int(series_match.group(2))
            slug = slugify(raw_name)

            if slug not in library:
                library[slug] = {
                    "name": raw_name,
                    "type": "series",
                    "slug": slug,
                    "seasons": {},
                }

            library[slug]["seasons"][season_num] = {
                "num": season_num,
                "folder": folder_path,
                "files": videos,
            }
        else:
            # ---- Movie ----
            # Strip quality / codec info from folder name
            raw_name = re.split(
                r"[\.\s](?:\d{4}|720p|1080p|2160p|4K|BluRay|BRRip|WEBRip|WEB-DL|HDRip|x264|x265|HEVC|AAC|DTS|REMUX|COMPLETE)",
                entry,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].replace(".", " ").strip()
            slug = slugify(raw_name)

            if slug not in library:
                library[slug] = {
                    "name": raw_name,
                    "type": "movie",
                    "slug": slug,
                    "seasons": {},
                }

            library[slug]["seasons"][0] = {
                "num": 0,
                "folder": folder_path,
                "files": videos,
            }

    with _library_cache_lock:
        _library_cache = library

    return library


def invalidate_library_cache():
    global _library_cache
    with _library_cache_lock:
        _library_cache = None


# ---------------------------------------------------------------------------
# TMDB cache helpers
# ---------------------------------------------------------------------------

def load_tmdb_cache():
    ensure_dirs()
    try:
        with open(TMDB_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_tmdb_cache(data):
    ensure_dirs()
    with open(TMDB_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# TMDB API helpers
# ---------------------------------------------------------------------------

def tmdb_request(path):
    """Make a GET request to the TMDB v3 API. Returns parsed JSON or None."""
    if not TMDB_TOKEN:
        return None
    sep = "&" if "?" in path else "?"
    url = f"https://api.themoviedb.org/3{path}{sep}language=fr-FR"
    headers = {
        "Authorization": f"Bearer {TMDB_TOKEN}",
        "Accept": "application/json",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"  TMDB request error ({path}): {exc}")
        return None


def fetch_tmdb_show_info(show_name):
    """Search TMDB for a TV show and return key metadata (cached)."""
    slug = slugify(show_name)
    cache_key = f"show_{slug}"
    cache = load_tmdb_cache()
    if cache_key in cache:
        return cache[cache_key]

    data = tmdb_request(f"/search/tv?query={quote(show_name)}")
    if not data or not data.get("results"):
        return None

    hit = data["results"][0]
    info = {
        "tmdb_id": hit["id"],
        "name": hit["name"],
        "poster_path": hit.get("poster_path"),
        "backdrop_path": hit.get("backdrop_path"),
        "overview": hit.get("overview", ""),
        "vote_average": hit.get("vote_average", 0),
        "first_air_date": hit.get("first_air_date", ""),
        "original_language": hit.get("original_language", ""),
    }

    cache[cache_key] = info
    save_tmdb_cache(cache)
    print(f"  TMDB: cached show info for '{show_name}'")
    return info


def fetch_tmdb_movie_info(movie_name):
    """Search TMDB for a movie and return key metadata (cached)."""
    slug = slugify(movie_name)
    cache_key = f"movie_{slug}"
    cache = load_tmdb_cache()
    if cache_key in cache:
        return cache[cache_key]

    data = tmdb_request(f"/search/movie?query={quote(movie_name)}")
    if not data or not data.get("results"):
        return None

    hit = data["results"][0]
    info = {
        "tmdb_id": hit["id"],
        "name": hit["title"],
        "poster_path": hit.get("poster_path"),
        "backdrop_path": hit.get("backdrop_path"),
        "overview": hit.get("overview", ""),
        "vote_average": hit.get("vote_average", 0),
        "release_date": hit.get("release_date", ""),
        "original_language": hit.get("original_language", ""),
    }

    cache[cache_key] = info
    save_tmdb_cache(cache)
    print(f"  TMDB: cached movie info for '{movie_name}'")
    return info


def fetch_tmdb_season_titles(tmdb_id, season_num):
    """Fetch episode titles for a given TMDB show season (cached).

    Returns ``{str(ep_number): title}``.
    """
    cache_key = f"season_{tmdb_id}_S{season_num:02d}"
    cache = load_tmdb_cache()
    if cache_key in cache:
        return cache[cache_key]

    data = tmdb_request(f"/tv/{tmdb_id}/season/{season_num}")
    if not data:
        return {}

    titles = {}
    for ep in data.get("episodes", []):
        titles[str(ep["episode_number"])] = ep["name"]

    cache[cache_key] = titles
    save_tmdb_cache(cache)
    print(f"  TMDB: cached {len(titles)} episode titles for show {tmdb_id} S{season_num:02d}")
    return titles


def get_poster_bytes(poster_path):
    """Download a poster from TMDB and cache it locally. Returns bytes or None."""
    if not poster_path:
        return None

    ensure_dirs()
    safe_name = poster_path.lstrip("/").replace("/", "_")
    local_path = os.path.join(POSTER_DIR, safe_name)

    if os.path.isfile(local_path):
        with open(local_path, "rb") as f:
            return f.read()

    url = f"https://image.tmdb.org/t/p/w500{poster_path}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            img_bytes = resp.read()
        with open(local_path, "wb") as f:
            f.write(img_bytes)
        return img_bytes
    except Exception as exc:
        print(f"  Poster download error: {exc}")
        return None


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------

def load_progress():
    """Load progress data. Returns dict ``{slug: {season_key: next_ep}}``."""
    ensure_dirs()
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_progress_data(data):
    ensure_dirs()
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_show_progress(slug):
    """Return progress dict for a given slug, e.g. ``{"S01": 6, "S02": 1}``."""
    return load_progress().get(slug, {})


def set_season_progress(slug, season_num, next_ep):
    """Set the next episode to play for a given slug + season."""
    data = load_progress()
    if slug not in data:
        data[slug] = {}
    season_key = f"S{season_num:02d}"
    data[slug][season_key] = next_ep
    save_progress_data(data)


# ---------------------------------------------------------------------------
# Playback state (thread-safe)
# ---------------------------------------------------------------------------

state = {
    "playing": False,
    "show_slug": None,
    "season": None,
    "current_episode": None,
    "current_file": None,
    "queue": [],
    "completed": [],
    "shutdown_after": False,
}
state_lock = threading.Lock()
current_process = None


# ---------------------------------------------------------------------------
# Bring VLC to foreground (Windows)
# ---------------------------------------------------------------------------

def bring_to_front(pid, delay=2, retries=5):
    """Bring the VLC window to the foreground after a short delay, with retries."""
    time.sleep(delay)
    user32 = ctypes.windll.user32

    for attempt in range(retries):
        try:
            # Attach to foreground thread to gain permission
            fore_hwnd = user32.GetForegroundWindow()
            fore_tid = user32.GetWindowThreadProcessId(fore_hwnd, None)
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            user32.AttachThreadInput(cur_tid, fore_tid, True)

            # ALT trick to allow SetForegroundWindow
            user32.keybd_event(0x12, 0, 0, 0)  # ALT down
            user32.keybd_event(0x12, 0, 2, 0)  # ALT up

            found = False
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

            def callback(hwnd, _):
                nonlocal found
                proc_pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_pid))
                if proc_pid.value == pid and user32.IsWindowVisible(hwnd):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                    found = True
                    return False
                return True

            user32.EnumWindows(WNDENUMPROC(callback), 0)
            user32.AttachThreadInput(cur_tid, fore_tid, False)

            if found:
                break
        except Exception:
            pass
        time.sleep(1)


# ---------------------------------------------------------------------------
# Playback worker
# ---------------------------------------------------------------------------

def play_worker(slug, season_num, start, count, shutdown):
    """Play a sequence of episodes/files via VLC in a background thread."""
    global current_process

    library = scan_library()
    show = library.get(slug)
    if not show:
        print(f"  ERROR: slug '{slug}' not found in library")
        return

    season = show["seasons"].get(season_num)
    if not season:
        print(f"  ERROR: season {season_num} not found for '{slug}'")
        return

    folder = season["folder"]
    all_files = season["files"]
    is_movie = show["type"] == "movie"

    # Filter files
    if is_movie:
        files = all_files[start - 1: start - 1 + count]
    else:
        files = [f for f in all_files if get_episode_number(f) >= start]
        files = files[:count]

    if not files:
        print(f"  No files to play for {slug} from episode {start}")
        return

    # Determine original language from TMDB
    if is_movie:
        tmdb_info = fetch_tmdb_movie_info(show["name"])
    else:
        tmdb_info = fetch_tmdb_show_info(show["name"])
    original_lang = (tmdb_info or {}).get("original_language", "")

    # Map ISO 639-1 (TMDB) to ISO 639-2 (VLC)
    lang_map = {"en": "eng", "fr": "fre", "es": "spa", "de": "ger", "it": "ita",
                "pt": "por", "ja": "jpn", "ko": "kor", "zh": "chi", "ar": "ara",
                "ru": "rus", "nl": "dut", "sv": "swe", "da": "dan", "no": "nor"}
    audio_lang = lang_map.get(original_lang, original_lang)

    with state_lock:
        state["playing"] = True
        state["show_slug"] = slug
        state["season"] = season_num
        state["queue"] = list(range(start, start + len(files))) if is_movie else [get_episode_number(f) for f in files]
        state["completed"] = []
        state["shutdown_after"] = shutdown

    for idx, file in enumerate(files):
        ep_num = (start + idx) if is_movie else get_episode_number(file)
        full_path = os.path.join(folder, file)

        with state_lock:
            state["current_episode"] = ep_num
            state["current_file"] = file

        label = f"{show['name']}" if is_movie else f"{slug} S{season_num:02d}E{ep_num:02d}"
        print(f"  > Playing {label}: {file} (audio={audio_lang})")

        vlc_args = [VLC_PATH, full_path, "--fullscreen", "--play-and-exit"]
        if audio_lang:
            vlc_args.append(f"--audio-language={audio_lang}")
        if original_lang != "fr":
            sub_track = find_sub_track(full_path)
            if sub_track is not None:
                vlc_args.append(f"--sub-track={sub_track}")
                print(f"    Subtitle track: {sub_track} (non-forced FR)")
            else:
                vlc_args.append("--sub-language=fre,fra,fr")

        current_process = subprocess.Popen(vlc_args)

        # Bring VLC to front in a separate thread
        threading.Thread(
            target=bring_to_front,
            args=(current_process.pid,),
            daemon=True,
        ).start()

        current_process.wait()
        current_process = None

        # Update progress to the next episode
        set_season_progress(slug, season_num, ep_num + 1)

        with state_lock:
            state["completed"].append(ep_num)

        print(f"  * Finished: {slug} S{season_num:02d}E{ep_num:02d}")
        time.sleep(1)

    with state_lock:
        state["playing"] = False
        state["current_episode"] = None
        state["current_file"] = None
        # Keep show_slug, season, and completed so the GUI can read them

    print("  Session complete")

    if shutdown:
        print("  Shutdown in 30 seconds...")
        os.system("shutdown /s /t 30")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    # ---- GET routes -------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._serve_html()

        elif path == "/api/library":
            self._handle_library()

        elif path == "/api/status":
            self._handle_status()

        elif path.startswith("/api/poster/"):
            self._handle_poster(path)

        elif path.startswith("/api/show/"):
            self._handle_show_route(path)

        elif path.startswith("/thumbs/"):
            self._handle_thumbs(path)

        else:
            self.send_error(404)

    # ---- POST routes ------------------------------------------------------

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/play":
            self._handle_play(body)
        elif path == "/api/progress":
            self._handle_set_progress(body)
        elif path == "/api/stop":
            self._handle_stop()
        else:
            self.send_error(404)

    # ---- OPTIONS (CORS preflight) -----------------------------------------

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    # ---- Route implementations --------------------------------------------

    def _serve_html(self):
        html_path = os.path.join(MEDIA_ROOT, "launcher_gui.html")
        if not os.path.isfile(html_path):
            self.send_error(404, "launcher_gui.html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._cors_headers()
        self.end_headers()
        with open(html_path, "rb") as f:
            self.wfile.write(f.read())

    def _handle_library(self):
        library = scan_library()
        result = []

        for slug, show in sorted(library.items(), key=lambda kv: kv[1]["name"].lower()):
            # Fetch TMDB info
            if show["type"] == "series":
                tmdb_info = fetch_tmdb_show_info(show["name"]) or {}
            else:
                tmdb_info = fetch_tmdb_movie_info(show["name"]) or {}

            seasons_list = []
            for snum in sorted(show["seasons"]):
                season_data = show["seasons"][snum]
                seasons_list.append({
                    "num": snum,
                    "episode_count": len(season_data["files"]),
                })

            progress = get_show_progress(slug)

            entry = {
                "slug": slug,
                "name": tmdb_info.get("name", show["name"]),
                "type": show["type"],
                "poster_path": tmdb_info.get("poster_path"),
                "overview": tmdb_info.get("overview", ""),
                "vote_average": tmdb_info.get("vote_average", 0),
                "seasons": seasons_list,
                "progress": progress,
            }
            result.append(entry)

        self._json(result)

    def _handle_show_route(self, path):
        """Handle /api/show/{slug}/season/{n}/episodes"""
        match = re.match(r"^/api/show/([^/]+)/season/(\d+)/episodes$", path)
        if not match:
            self.send_error(404)
            return

        slug = match.group(1)
        season_num = int(match.group(2))

        library = scan_library()
        show = library.get(slug)
        if not show:
            self._json({"error": f"Show '{slug}' not found"}, 404)
            return

        season = show["seasons"].get(season_num)
        if not season:
            self._json({"error": f"Season {season_num} not found"}, 404)
            return

        # Fetch episode titles from TMDB
        titles = {}
        if show["type"] == "series":
            tmdb_info = fetch_tmdb_show_info(show["name"])
            if tmdb_info and tmdb_info.get("tmdb_id"):
                titles = fetch_tmdb_season_titles(tmdb_info["tmdb_id"], season_num)

        episodes = []
        for idx, f in enumerate(season["files"], start=1):
            if show["type"] == "movie":
                num = idx
                code = f"Film {idx}" if len(season["files"]) > 1 else "Film"
                title = ""
            else:
                num = get_episode_number(f)
                code = get_episode_code(f) or f"S{season_num:02d}E{num:02d}"
                title = titles.get(str(num), "")
            episodes.append({
                "num": num,
                "code": code,
                "title": title,
                "file": f,
            })

        progress = get_show_progress(slug)
        season_key = f"S{season_num:02d}"
        next_episode = progress.get(season_key, 1)

        self._json({
            "show": show["name"],
            "season": season_num,
            "episodes": episodes,
            "next_episode": next_episode,
        })

    def _handle_poster(self, path):
        """Serve cached TMDB poster for /api/poster/{slug}"""
        slug = path.split("/api/poster/", 1)[-1].strip("/")
        if not slug:
            self.send_error(404)
            return

        library = scan_library()
        show = library.get(slug)
        if not show:
            self.send_error(404)
            return

        # Get poster_path from TMDB info
        if show["type"] == "series":
            tmdb_info = fetch_tmdb_show_info(show["name"])
        else:
            tmdb_info = fetch_tmdb_movie_info(show["name"])

        if not tmdb_info or not tmdb_info.get("poster_path"):
            self.send_error(404, "No poster available")
            return

        img_bytes = get_poster_bytes(tmdb_info["poster_path"])
        if not img_bytes:
            self.send_error(502, "Failed to fetch poster")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(img_bytes)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(img_bytes)

    def _handle_thumbs(self, path):
        """Serve /thumbs/{slug}/{season_num}/{filename}"""
        parts = path.strip("/").split("/")
        # Expected: thumbs / slug / season_num / filename
        if len(parts) != 4:
            self.send_error(404)
            return

        _, slug, season_str, filename = parts

        library = scan_library()
        show = library.get(slug)
        if not show:
            self.send_error(404)
            return

        try:
            season_num = int(season_str)
        except ValueError:
            self.send_error(400)
            return

        season = show["seasons"].get(season_num)
        if not season:
            self.send_error(404)
            return

        thumb_path = os.path.join(season["folder"], "thumbs", filename)
        if not os.path.isfile(thumb_path):
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "public, max-age=86400")
        self._cors_headers()
        self.end_headers()
        with open(thumb_path, "rb") as f:
            self.wfile.write(f.read())

    def _handle_status(self):
        with state_lock:
            self._json(dict(state))

    def _handle_play(self, body):
        with state_lock:
            if state["playing"]:
                self._json({"error": "Playback already in progress"}, 409)
                return

        if not VLC_PATH:
            self._json({"error": "VLC not found"}, 500)
            return

        slug = body.get("show")
        season_num = body.get("season", 1)
        start = body.get("start", 1)
        count = body.get("count", 1)
        shutdown = body.get("shutdown", False)

        if not slug:
            self._json({"error": "Missing 'show' parameter"}, 400)
            return

        t = threading.Thread(
            target=play_worker,
            args=(slug, season_num, start, count, shutdown),
            daemon=True,
        )
        t.start()

        self._json({
            "status": "started",
            "show": slug,
            "season": season_num,
            "start": start,
            "count": count,
        })

    def _handle_set_progress(self, body):
        slug = body.get("show")
        season_num = body.get("season", 1)
        episode = body.get("episode", 1)

        if not slug:
            self._json({"error": "Missing 'show' parameter"}, 400)
            return

        set_season_progress(slug, season_num, episode)
        self._json({"status": "ok"})

    def _handle_stop(self):
        global current_process
        with state_lock:
            if not state["playing"]:
                self._json({"error": "Nothing is playing"}, 400)
                return
        if current_process:
            current_process.terminate()
        self._json({"status": "stopped"})

    # ---- Shared helpers ---------------------------------------------------

    def _json(self, data, code=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, format, *args):
        # Suppress all HTTP logs except errors
        msg = format % args
        if any(code in msg for code in ("404", "500", "502", "400", "409")):
            print(f"  [HTTP] {msg}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ensure_dirs()

    # Scan library for startup summary
    library = scan_library()

    print()
    print("  OwnNetflix -- Multi-show VLC Launcher")
    print("  ======================================")
    if VLC_PATH:
        print(f"  VLC        : {VLC_PATH}")
    else:
        print("  WARNING: VLC not found! Install VLC or edit VLC_CANDIDATES.")
    print(f"  Media root : {MEDIA_ROOT}")

    series_items = {s: v for s, v in library.items() if v["type"] == "series"}
    movie_items = {s: v for s, v in library.items() if v["type"] == "movie"}

    if series_items:
        print(f"  Series     : {len(series_items)}")
        for slug, show in sorted(series_items.items(), key=lambda kv: kv[1]["name"].lower()):
            season_nums = sorted(show["seasons"].keys())
            season_strs = [f"S{sn:02d} ({len(show['seasons'][sn]['files'])} ep)" for sn in season_nums]
            print(f"    - {show['name']}: {', '.join(season_strs)}")

    if movie_items:
        print(f"  Movies     : {len(movie_items)}")
        for slug, show in sorted(movie_items.items(), key=lambda kv: kv[1]["name"].lower()):
            file_count = len(show["seasons"][0]["files"])
            print(f"    - {show['name']} ({file_count} file{'s' if file_count != 1 else ''})")

    if not library:
        print("  (no media found)")

    print(f"  Server     : http://localhost:{PORT}")
    print("  Ctrl+C     : Stop server")
    print()

    webbrowser.open(f"http://localhost:{PORT}")

    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()
