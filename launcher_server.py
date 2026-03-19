"""
Abbott Elementary S01 — Serveur Lanceur VLC
Lance avec : python launcher_server.py
Ouvre automatiquement le navigateur sur http://localhost:8899
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
from urllib.parse import urlparse, quote

FOLDER = os.path.dirname(os.path.abspath(__file__))
VIDEO_EXT = (".mp4", ".mkv", ".avi")
PROGRESS_FILE = os.path.join(FOLDER, "progress.txt")
PORT = 8899

# Chercher VLC
VLC_CANDIDATES = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
]
VLC_PATH = None
for path in VLC_CANDIDATES:
    if os.path.isfile(path):
        VLC_PATH = path
        break

# TMDB
def load_env():
    env_path = os.path.join(FOLDER, ".env")
    if os.path.isfile(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()

load_env()
TMDB_TOKEN = os.environ.get("TMDB_TOKEN", "")
TMDB_CACHE = os.path.join(FOLDER, "tmdb_cache.json")

# Etat partage (thread-safe)
state = {
    "playing": False,
    "current_episode": None,
    "current_file": None,
    "queue": [],
    "completed": [],
    "shutdown_after": False,
}
state_lock = threading.Lock()
current_process = None


def get_episode_number(filename):
    match = re.search(r"S\d+E(\d+)", filename, re.IGNORECASE)
    return int(match.group(1)) if match else -1


def load_progress():
    try:
        with open(PROGRESS_FILE, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 1


def save_progress(ep):
    with open(PROGRESS_FILE, "w") as f:
        f.write(str(ep))


def get_video_files():
    files = [f for f in os.listdir(FOLDER) if f.lower().endswith(VIDEO_EXT)]
    files.sort(key=get_episode_number)
    return files


def get_episode_code(filename):
    match = re.search(r'(S\d+E\d+)', filename, re.IGNORECASE)
    return match.group(1).upper() if match else None


def extract_show_info():
    """Extract show name and season number from video filenames."""
    for f in get_video_files():
        match = re.match(r'^(.+?)\.S(\d+)E\d+', f, re.IGNORECASE)
        if match:
            return match.group(1).replace('.', ' '), int(match.group(2))
    return None, None


def load_tmdb_cache():
    try:
        with open(TMDB_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_tmdb_cache(data):
    with open(TMDB_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_tmdb_episodes():
    """Fetch episode titles from TMDB, with local cache."""
    show_name, season_num = extract_show_info()
    if not show_name or not TMDB_TOKEN:
        return show_name, season_num, {}

    cache = load_tmdb_cache()
    cache_key = f"{show_name}_S{season_num:02d}"
    if cache_key in cache:
        return show_name, season_num, cache[cache_key]

    try:
        headers = {
            "Authorization": f"Bearer {TMDB_TOKEN}",
            "Accept": "application/json",
        }

        # Search show
        url = f"https://api.themoviedb.org/3/search/tv?query={quote(show_name)}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read()).get("results", [])

        if not results:
            return show_name, season_num, {}

        show_id = results[0]["id"]

        # Get season details
        url = f"https://api.themoviedb.org/3/tv/{show_id}/season/{season_num}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            season = json.loads(resp.read())

        titles = {}
        for ep in season.get("episodes", []):
            titles[str(ep["episode_number"])] = ep["name"]

        cache[cache_key] = titles
        save_tmdb_cache(cache)
        print(f"  TMDB : {len(titles)} titres recuperes pour {show_name} S{season_num:02d}")

        return show_name, season_num, titles
    except Exception as e:
        print(f"  ⚠  TMDB : {e}")
        return show_name, season_num, {}


def bring_to_front(pid, delay=2):
    """Amene la fenetre VLC au premier plan apres un delai."""
    time.sleep(delay)
    try:
        user32 = ctypes.windll.user32
        # Astuce Windows : simuler ALT pour autoriser SetForegroundWindow
        user32.keybd_event(0x12, 0, 0, 0)  # ALT down
        user32.keybd_event(0x12, 0, 2, 0)  # ALT up

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def callback(hwnd, _):
            proc_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_pid))
            if proc_pid.value == pid and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
                user32.SetForegroundWindow(hwnd)
                return False
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
    except Exception:
        pass


def play_worker(start, count, shutdown):
    global current_process

    files = get_video_files()
    files = [f for f in files if get_episode_number(f) >= start]
    files = files[:count]

    with state_lock:
        state["playing"] = True
        state["queue"] = [get_episode_number(f) for f in files]
        state["completed"] = []
        state["shutdown_after"] = shutdown

    for file in files:
        ep_num = get_episode_number(file)
        full_path = os.path.join(FOLDER, file)

        with state_lock:
            state["current_episode"] = ep_num
            state["current_file"] = file

        print(f"  ▶ Episode {ep_num} : {file}")

        current_process = subprocess.Popen([
            VLC_PATH, full_path,
            "--fullscreen",
            "--sub-language=fr",
            "--audio-language=fr",
            "--play-and-exit"
        ])

        # Amener VLC au premier plan dans un thread separe
        threading.Thread(
            target=bring_to_front,
            args=(current_process.pid,),
            daemon=True,
        ).start()

        current_process.wait()
        current_process = None

        save_progress(ep_num + 1)

        with state_lock:
            state["completed"].append(ep_num)

        print(f"  ✔ Termine : Episode {ep_num}")
        time.sleep(1)

    with state_lock:
        state["playing"] = False
        state["current_episode"] = None
        state["current_file"] = None

    print("  🎉 Session terminee")

    if shutdown:
        print("  💤 Extinction dans 30 secondes...")
        os.system("shutdown /s /t 30")


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            html_path = os.path.join(FOLDER, "launcher_gui.html")
            if not os.path.isfile(html_path):
                self.send_error(404, "launcher_gui.html introuvable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(html_path, "rb") as f:
                self.wfile.write(f.read())

        elif path.startswith("/thumbs/"):
            # Servir les thumbnails
            filename = os.path.basename(path)
            thumb_path = os.path.join(FOLDER, "thumbs", filename)
            if os.path.isfile(thumb_path):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "public, max-age=86400")
                self._cors_headers()
                self.end_headers()
                with open(thumb_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404)

        elif path == "/api/progress":
            ep = load_progress()
            self._json({"next_episode": ep})

        elif path == "/api/status":
            with state_lock:
                # Verifier que VLC tourne encore
                if state["playing"] and current_process is not None:
                    if current_process.poll() is not None:
                        # VLC s'est arrete mais l'etat n'a pas ete mis a jour
                        state["playing"] = False
                        state["current_episode"] = None
                        state["current_file"] = None
                elif state["playing"] and current_process is None:
                    # Etat incoherent : reset
                    state["playing"] = False
                    state["current_episode"] = None
                    state["current_file"] = None
                self._json(dict(state))

        elif path == "/api/episodes":
            files = get_video_files()
            show_name, season_num, titles = fetch_tmdb_episodes()
            eps = []
            for f in files:
                num = get_episode_number(f)
                code = get_episode_code(f) or f"S{(season_num or 1):02d}E{num:02d}"
                title = titles.get(str(num), "")
                eps.append({"num": num, "code": code, "title": title, "file": f})
            self._json({
                "show": show_name or "Unknown",
                "season": season_num or 1,
                "episodes": eps,
            })

        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/play":
            with state_lock:
                if state["playing"]:
                    self._json({"error": "Lecture déjà en cours"}, 409)
                    return

            if not VLC_PATH:
                self._json({"error": "VLC introuvable"}, 500)
                return

            start = body.get("start", 1)
            count = body.get("count", 1)
            shutdown = body.get("shutdown", False)

            t = threading.Thread(
                target=play_worker,
                args=(start, count, shutdown),
                daemon=True,
            )
            t.start()
            self._json({"status": "started", "start": start, "count": count})

        elif path == "/api/progress":
            ep = body.get("episode", 1)
            save_progress(ep)
            self._json({"status": "ok"})

        elif path == "/api/stop":
            with state_lock:
                if not state["playing"]:
                    self._json({"error": "Rien en lecture"}, 400)
                    return
            if current_process:
                current_process.terminate()
            self._json({"status": "stopped"})

        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, format, *args):
        # Log uniquement les erreurs et les thumbs pour debug
        msg = format % args
        if "404" in msg or "thumbs" in msg:
            print(f"  [HTTP] {msg}")


if __name__ == "__main__":
    print()
    print("  🎬 Abbott Elementary — Lanceur VLC")
    print("  ───────────────────────────────────")
    if VLC_PATH:
        print(f"  VLC     : {VLC_PATH}")
    else:
        print("  ⚠  VLC non trouvé ! Installez VLC ou modifiez VLC_CANDIDATES")
    print(f"  Dossier : {FOLDER}")
    print(f"  Videos  : {len(get_video_files())} fichiers")
    print(f"  Serveur : http://localhost:{PORT}")
    print("  Ctrl+C  : Arrêter")
    print()

    webbrowser.open(f"http://localhost:{PORT}")

    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  👋 Serveur arrêté")
        server.server_close()
