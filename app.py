import os
import json
import urllib.error
import string
import re
import hmac
import secrets
import signal
import subprocess
import shutil
import zipfile
import hashlib
import psutil
import threading
import time
import urllib.request
import sys
import shlex
import re
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, Response

app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent))
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # Railway/Cloudflare er pichone thaka https thik kore
# Session lifetime: user logout na korle login thakbe (browser/app bondho korleo)
app.permanent_session_lifetime = timedelta(days=365)

BASE_DIR = Path(__file__).parent
# Railway volume er path DATA_DIR env diye set kora hoy (jemon /data). Na thakle code er folder e.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "data.json"
SERVERS_DIR = DATA_DIR / "servers"
SERVERS_DIR.mkdir(parents=True, exist_ok=True)

# Secret key ekbar generate kore file e save thake, jate app restart er pore session na haray.
def _load_secret_key():
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_file = DATA_DIR / ".secret_key"
    try:
        if key_file.exists():
            k = key_file.read_text().strip()
            if k:
                return k
        k = secrets.token_hex(32)
        key_file.write_text(k)
        return k
    except OSError:
        return secrets.token_hex(32)

app.secret_key = _load_secret_key()

# Volume e data.json na thakle, code er sathe ja ache sheta copy hobe (prothombar)
_SEED_FILE = BASE_DIR / "data.json"
if not DATA_FILE.exists() and _SEED_FILE.exists() and DATA_DIR != BASE_DIR:
    shutil.copy(_SEED_FILE, DATA_FILE)

NORMAL_PASSWORD = os.environ.get("NORMAL_PASSWORD", "APURBODEV987")

RUNNING_PROCESSES = {}

def load_data():
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
            # SITE_NAME env set thakle sheta sob jaygay dekhabe (purono data.json er naam overwrite kore)
            env_name = os.environ.get("SITE_NAME")
            if env_name:
                data.setdefault("settings", {})["site_name"] = env_name
            return data
        except Exception:
            pass
    return {
        "servers": {},
        "users": {},
        "settings": {
            "maintenance": False,
            "maintenance_msg": "System under maintenance.",
            "theme_color": "#9333ea",
            "normal_password": NORMAL_PASSWORD,
            "site_name": "ApurboCloud",
            "auto_restart_interval": 300
        }
    }

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

THEME_COLOR = "#9333ea"  # Purple theme (code theke force kora hoy, purono data.json er red value override kore)

def get_theme_color():
    return THEME_COLOR

@app.context_processor
def inject_theme():
    return {"theme_color": get_theme_color()}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        if _account_expired(session["username"]):
            session.clear()
            return redirect(url_for("login"))
        data = load_data()
        settings = data.get("settings", {})
        if settings.get("maintenance"):
            return render_template("maintenance.html", message=settings.get("maintenance_msg", "Under maintenance"), site_name=settings.get("site_name", "ApurboCloud"), theme_color=get_theme_color())
        return f(*args, **kwargs)
    return decorated

# ==================== PLANS (RAM / STORAGE / CPU) ====================
# cpu_pct: 100 = 1 CPU core. 200 = 2 cores.
PLANS = {
    "256":  {"label": "RAM-256MB  STORAGE-1GB  CPU-25%",   "ram_mb": 256,  "storage_gb": 1,  "cpu_pct": 25},
    "512":  {"label": "RAM-512MB  STORAGE-2GB  CPU-50%",   "ram_mb": 512,  "storage_gb": 2,  "cpu_pct": 50},
    "1024": {"label": "RAM-1GB  STORAGE-4GB  CPU-100%",    "ram_mb": 1024, "storage_gb": 4,  "cpu_pct": 100},
    "2048": {"label": "RAM-2GB  STORAGE-8GB  CPU-125%",    "ram_mb": 2048, "storage_gb": 8,  "cpu_pct": 125},
    "4096": {"label": "RAM-4GB  STORAGE-12GB  CPU-150%",   "ram_mb": 4096, "storage_gb": 12, "cpu_pct": 150},
    "6144": {"label": "RAM-6GB  STORAGE-16GB  CPU-200%",   "ram_mb": 6144, "storage_gb": 16, "cpu_pct": 200},
}
DEFAULT_PLAN = "256"   # lifetime free plan
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "APURBODEV")
BKASH_NUMBER = os.environ.get("BKASH_NUMBER", "01611990789")
app.jinja_env.globals["PLANS"] = PLANS

def _get_plan(cfg):
    return PLANS.get(str(cfg.get("plan", DEFAULT_PLAN)), PLANS[DEFAULT_PLAN])

def _dir_size(path):
    total = 0
    if path.exists():
        for root_dir, _, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root_dir, fn))
                except OSError:
                    pass
    return total

def _plan_price(data, plan_id):
    prices = data.get("settings", {}).get("plan_prices", {})
    try:
        return int(prices.get(plan_id, 0))
    except (TypeError, ValueError):
        return 0

def _plan_list(data):
    return [{"id": pid, "label": pl["label"], "free": pid == DEFAULT_PLAN,
             "price": _plan_price(data, pid)} for pid, pl in PLANS.items()]

def _log_line(name, msg):
    try:
        with open(SERVERS_DIR / name / "logs.txt", "a") as lf:
            lf.write(f"\n[PLAN LIMIT] {msg}\n")
    except OSError:
        pass

def _tree_procs(root):
    procs = [root]
    try:
        procs += root.children(recursive=True)
    except psutil.Error:
        pass
    return procs

def _tree_cpu_seconds(root):
    total = 0.0
    for p in _tree_procs(root):
        try:
            ct = p.cpu_times()
            total += ct.user + ct.system
        except psutil.Error:
            pass
    return total

def _signal_tree(root, sig):
    for p in _tree_procs(root):
        try:
            os.kill(p.pid, sig)
        except OSError:
            pass

LIVE_STATS = {}  # server name -> latest RAM/CPU usage (limiter update kore)

def _limiter(name, pid):
    """Plan er RAM ar CPU limit enforce kore.
    RAM limit cross korle server bondho hoy. CPU limit cross korle process ke short pause/resume kora hoy (throttle)."""
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    cfg = load_data().get("servers", {}).get(name, {})
    plan = _get_plan(cfg)
    ram_limit = plan["ram_mb"] * 1024 * 1024
    cores_limit = plan["cpu_pct"] / 100.0
    run = 1.0  # fraction of each 1s cycle the process is allowed to run
    while True:
        try:
            if not root.is_running() or root.status() == psutil.STATUS_ZOMBIE:
                break
            _signal_tree(root, signal.SIGCONT)
            c0 = _tree_cpu_seconds(root)
            t0 = time.monotonic()
            time.sleep(run)
            c1 = _tree_cpu_seconds(root)
            elapsed = max(time.monotonic() - t0, 1e-6)

            rss = 0
            for p in _tree_procs(root):
                try:
                    rss += p.memory_info().rss
                except psutil.Error:
                    pass
            if rss > ram_limit:
                _log_line(name, f"RAM limit {plan['ram_mb']}MB exceeded (used {rss // (1024*1024)}MB). Server stopped.")
                _signal_tree(root, signal.SIGKILL)
                break

            if run < 1.0:
                _signal_tree(root, signal.SIGSTOP)
                time.sleep(1.0 - run)

            used_cores = max(0.0, c1 - c0) / elapsed
            LIVE_STATS[name] = {"ram_mb": round(rss / 1048576, 1), "cpu_pct": round(used_cores * 100, 1)}
            run = max(0.05, cores_limit / used_cores) if used_cores > cores_limit else 1.0
        except psutil.Error:
            break
        except Exception:
            break
    LIVE_STATS.pop(name, None)

def is_process_alive(pid):
    try:
        if not pid:
            return False
        p = psutil.Process(pid)
        return p.is_running() and p.status() not in [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

def kill_process(pid):
    try:
        p = psutil.Process(pid)
        children = p.children(recursive=True)
        p.terminate()
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except psutil.TimeoutExpired:
            p.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

def auto_detect_main_file(extract_dir):
    candidates = ["main.py", "bot.py", "app.py", "index.js", "bot.js", "app.js", "index.ts", "server.js"]
    for f in candidates:
        if (extract_dir / f).exists():
            return f
    for p in extract_dir.iterdir():
        if p.is_file() and p.suffix in [".py", ".js", ".ts", ".php", ".go", ".sh"]:
            return p.name
    return "main.py"

def prepare_environment_and_deps(extract_dir, log_path, runtime="python"):
    env = os.environ.copy()
    subfolders = [str(extract_dir)]
    for item in extract_dir.iterdir():
        if item.is_dir() and not item.name.startswith((".", "__pycache__", "node_modules")):
            subfolders.append(str(item))
    
    existing_pythonpath = env.get("PYTHONPATH", "")
    new_pythonpath = os.pathsep.join(subfolders)
    env["PYTHONPATH"] = f"{new_pythonpath}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else new_pythonpath

    try:
        if runtime == "node":
            pkg_json = extract_dir / "package.json"
            node_modules = extract_dir / "node_modules"
            if pkg_json.exists() and not node_modules.exists():
                with open(log_path, "a") as lf:
                    lf.write(f"\n[SYSTEM] Installing Node.js dependencies via npm install...\n")
                subprocess.run(["npm", "install"], cwd=str(extract_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            req_file = extract_dir / "requirements.txt"
            if req_file.exists():
                with open(log_path, "a") as lf:
                    lf.write(f"\n[SYSTEM] Installing Python dependencies via pip install...\n")
                subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=str(extract_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        with open(log_path, "a") as lf:
            lf.write(f"\n[SYSTEM WARNING] Dependency auto-install error: {e}\n")

    return env

def get_run_command(runtime, main_file):
    ext = Path(main_file).suffix.lower()
    if runtime == "node" or ext in (".js", ".ts", ".mjs"):
        return ["node", main_file]
    elif runtime == "php" or ext == ".php":
        return ["php", main_file]
    elif runtime == "go" or ext == ".go":
        return ["go", "run", main_file]
    elif runtime == "sh" or ext == ".sh":
        return ["bash", main_file]
    elif runtime == "static":
        return [sys.executable, "-m", "http.server", "8080"]
    else:
        return [sys.executable, "-u", main_file]

def _sync_process_status():
    data = load_data()
    changed = False
    for name, cfg in data["servers"].items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            changed = True
    if changed:
        save_data(data)

_sync_process_status()

def _railway_url():
    dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL")
    if not dom:
        return None
    return dom if dom.startswith("http") else f"https://{dom}"

# ==================== RENDER RESTART PREVENTION ====================
def render_keep_alive():
    while True:
        try:
            time.sleep(600)
            port = os.environ.get("PORT", 5000)
            url = f"http://127.0.0.1:{port}/api/ping"
            
            req = urllib.request.Request(url, headers={'User-Agent': 'Render-KeepAlive/1.0'})
            urllib.request.urlopen(req, timeout=10)
            
            external_url = os.environ.get("RENDER_EXTERNAL_URL") or _railway_url()
            if external_url:
                ping_url = f"{external_url}/api/ping"
                req2 = urllib.request.Request(ping_url, headers={'User-Agent': 'Render-KeepAlive/1.0'})
                urllib.request.urlopen(req2, timeout=10)
        except Exception:
            pass

threading.Thread(target=render_keep_alive, daemon=True).start()

@app.route("/api/ping")
def ping():
    return "pong", 200

def keep_alive():
    while True:
        time.sleep(240)
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL") or _railway_url()
            if url:
                ping_url = f"{url}/api/ping"
            else:
                port = os.environ.get("PORT", 5000)
                ping_url = f"http://127.0.0.1:{port}/api/ping"
            req = urllib.request.Request(ping_url, headers={'User-Agent': 'KeepAlive-Bot/1.0'})
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

threading.Thread(target=keep_alive, daemon=True).start()

# ==================== AUTO RESTART SYSTEM ====================
def auto_restart_server(name):
    try:
        data = load_data()
        cfg = data["servers"].get(name)
        if not cfg:
            return
        
        pid = cfg.get("pid")
        if pid and is_process_alive(pid):
            kill_process(pid)
            if name in RUNNING_PROCESSES:
                try:
                    RUNNING_PROCESSES[name]["proc"].terminate()
                    RUNNING_PROCESSES[name]["log_file"].close()
                except Exception:
                    pass
                del RUNNING_PROCESSES[name]
        
        extract_dir = SERVERS_DIR / name / "extracted"
        main_file = cfg.get("main_file") or auto_detect_main_file(extract_dir)
        main_cmd = cfg.get("main_command") or ""
        main_path = extract_dir / main_file
        
        if not main_path.exists():
            return
        
        log_path = SERVERS_DIR / name / "logs.txt"
        if main_cmd:
            cmd = shlex.split(main_cmd)
        else:
            cmd = get_run_command(cfg.get("runtime", "python"), main_file)
            
        env = prepare_environment_and_deps(extract_dir, log_path, cfg.get("runtime", "python"))
        env["PORT"] = str(_ensure_port_unique(data, name, cfg))
        
        with open(log_path, "a") as lf:
            lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] AUTO-RESTART triggered\n{'='*50}\n")
        log_file = open(log_path, "a")
        proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdout=log_file, stderr=log_file, env=env, preexec_fn=os.setsid)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        threading.Thread(target=_limiter, args=(name, proc.pid), daemon=True).start()
        data["servers"][name] = cfg
        save_data(data)
    except Exception as e:
        print(f"Auto-restart error for {name}: {e}")

def auto_restart_monitor():
    while True:
        try:
            data = load_data()
            settings = data.get("settings", {})
            interval = settings.get("auto_restart_interval", 300)
            
            for name, cfg in data["servers"].items():
                pid = cfg.get("pid")
                if pid and not is_process_alive(pid):
                    cfg["status"] = "stopped"
                    cfg["pid"] = None
                    save_data(data)
                
                if cfg.get("status") == "stopped":
                    extract_dir = SERVERS_DIR / name / "extracted"
                    main_file = cfg.get("main_file") or auto_detect_main_file(extract_dir)
                    if (extract_dir / main_file).exists():
                        threading.Thread(target=auto_restart_server, args=[name], daemon=True).start()
            
            time.sleep(interval)
        except Exception:
            time.sleep(30)

threading.Thread(target=auto_restart_monitor, daemon=True).start()

# ==================== LANDING + LOGIN ====================
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{2,20}$")

def site_name_value():
    return load_data().get("settings", {}).get("site_name", "ApurboCloud")

@app.route("/")
def landing():
    # First page: landing screen. START button -> login page.
    if session.get("username"):
        return redirect(url_for("dashboard"))
    data = load_data()
    return render_template("landing.html", site_name=site_name_value(),
                           total_users=len(data.get("users", {})),
                           total_servers=len(data.get("servers", {})),
                           theme_color=get_theme_color())

def _ensure_user_server(data, username, plan=DEFAULT_PLAN):
    """Prottek user er nijer server thakbe (server er naam = username). Na thakle toiri hobe."""
    servers = data.setdefault("servers", {})
    if username in servers:
        return False
    servers[username] = {
        "name": username,
        "owner": username,
        "runtime": "python",
        "plan": plan,
        "status": "stopped",
        "main_file": "",
        "main_command": "",
        "port": _allocate_port(data),
        "pid": None,
        "created": datetime.now().isoformat()
    }
    (SERVERS_DIR / username / "extracted").mkdir(parents=True, exist_ok=True)
    return True

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("username"):
        return redirect(url_for("dashboard"))

    data = load_data()
    settings = data.get("settings", {})
    site_name = settings.get("site_name", "ApurboCloud")

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "").strip()

        def fail(msg):
            return render_template("login.html", error=msg, username=username,
                                   theme_color=get_theme_color(), site_name=site_name)

        if not USERNAME_RE.match(username):
            return fail("Username must be 2-20 chars (a-z, 0-9, _ . -)")

        user = data.get("users", {}).get(username)
        if not user or user.get("password_hash") != hash_password(password):
            return fail("Wrong username or password")

        if _account_expired(username):
            return fail("Apnar access ti shesh hoye geche. Admin er kache notun access key nin.")
        if _ensure_user_server(data, username):
            save_data(data)
        session.permanent = True
        session["username"] = username
        return redirect(url_for("dashboard"))

    success = None
    if request.args.get("registered"):
        success = "Account create hoyeche! Ekhon username ar password die login korun."
    return render_template("login.html", error=None, success=success, username="",
                           theme_color=get_theme_color(), site_name=site_name)

_REG_LOCK = threading.Lock()  # ekshathe duijon same key use korle jate duitai account na hoy

def _register_impl():
    if session.get("username"):
        return redirect(url_for("dashboard"))

    data = load_data()
    settings = data.get("settings", {})
    site_name = settings.get("site_name", "ApurboCloud")
    access_code = settings.get("normal_password", NORMAL_PASSWORD)

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "").strip()
        code = request.form.get("access_code", "").strip()
        plan = DEFAULT_PLAN

        def fail(msg):
            return render_template("register.html", error=msg, username=username,
                                   theme_color=get_theme_color(), site_name=site_name)

        if not USERNAME_RE.match(username):
            return fail("Username must be 2-20 chars (a-z, 0-9, _ . -)")
        if len(password) < 6:
            return fail("Password must be at least 6 characters")
        key_rec = next((k for k in data.get("access_keys", [])
                        if k.get("key") == code and not k.get("used_by")), None)
        if not key_rec:
            return fail("Access key ভুল ba agei use hoyeche")

        users = data.setdefault("users", {})
        if username in users or username in data.get("servers", {}):
            return fail("This username is already taken")

        users[username] = {
            "joined": datetime.now().isoformat(),
            "password_hash": hash_password(password)
        }
        now = datetime.now()
        expires = (now + timedelta(days=key_rec.get("days", 30))).isoformat()
        key_rec["used_by"] = username
        key_rec["used_at"] = now.isoformat()
        key_rec["expires_at"] = expires
        users[username]["expires_at"] = expires
        _ensure_user_server(data, username, plan)
        save_data(data)

        # Account create hoyeche, kintu auto login korbe na. User nijer username/password die login korbe.
        return redirect(url_for("login", registered="1"))

    return render_template("register.html", error=None, username="",
                           theme_color=get_theme_color(), site_name=site_name)

@app.route("/register", methods=["GET", "POST"])
def register():
    with _REG_LOCK:
        return _register_impl()

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ==================== DASHBOARD ====================
@app.route("/dashboard")
@login_required
def dashboard():
    username = session["username"]
    data = load_data()
    settings = data.get("settings", {})
    site_name = settings.get("site_name", "ApurboCloud")
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    changed = False
    for name, cfg in user_servers.items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            data["servers"][name] = cfg
            changed = True
    if changed:
        save_data(data)
    running = sum(1 for v in user_servers.values() if v.get("status") == "running")
    return render_template("dashboard.html", servers=user_servers, running=running, total=len(user_servers), username=username, site_name=site_name, theme_color=get_theme_color())

@app.route("/api/stats")
@login_required
def system_stats():
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    return jsonify({"cpu": cpu, "ram": ram, "disk": disk})

# ==================== SERVER CRUD ====================
@app.route("/server/create", methods=["POST"])
@login_required
def create_server():
    name = request.form.get("name", "").strip().replace(" ", "-")
    runtime = request.form.get("runtime", "python")
    plan = request.form.get("plan", DEFAULT_PLAN)
    if plan not in PLANS:
        plan = DEFAULT_PLAN
    requested_plan = plan
    plan = DEFAULT_PLAN  # paid plan payment er por active hobe
    if not name:
        return redirect(url_for("dashboard"))
    data = load_data()
    if name in data["servers"]:
        return redirect(url_for("dashboard"))
    cfg = {
        "name": name,
        "owner": session["username"],
        "runtime": runtime,
        "plan": plan,
        "status": "stopped",
        "main_file": "",
        "main_command": "",
        "port": _allocate_port(data),
        "pid": None,
        "created": datetime.now().isoformat()
    }
    data["servers"][name] = cfg
    save_data(data)
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    if requested_plan != DEFAULT_PLAN:
        return redirect(url_for("upgrade_server", name=name, plan=requested_plan))
    return redirect(url_for("server_detail", name=name))

@app.route("/server/delete/<name>", methods=["POST"])
@login_required
def delete_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if cfg and cfg.get("owner") == session["username"]:
        pid = cfg.get("pid")
        if pid:
            kill_process(pid)
        if name in RUNNING_PROCESSES:
            try:
                RUNNING_PROCESSES[name]["proc"].terminate()
                RUNNING_PROCESSES[name]["log_file"].close()
            except Exception:
                pass
            del RUNNING_PROCESSES[name]
        del data["servers"][name]
        save_data(data)
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
    return redirect(url_for("dashboard"))

@app.route("/server/<name>")
@login_required
def server_detail(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return "Server not found", 404
    if cfg.get("owner") != session["username"]:
        return "Access denied", 403
    pid = cfg.get("pid")
    if pid and not is_process_alive(pid):
        cfg["status"] = "stopped"
        cfg["pid"] = None
        data["servers"][name] = cfg
        save_data(data)
    if "main_command" not in cfg:
        cfg["main_command"] = ""
    extract_dir = SERVERS_DIR / name / "extracted"
    files = list_files(extract_dir)
    return render_template("server.html", server_name=name, config=cfg, files=files, theme_color=get_theme_color())

def list_files(directory, base=""):
    result = []
    if not directory.exists():
        return result
    try:
        for entry in sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name)):
            rel = f"{base}/{entry.name}" if base else entry.name
            if entry.is_dir():
                result.append({"name": entry.name, "path": rel, "type": "dir", "size": 0})
                result.extend(list_files(entry, rel))
            else:
                result.append({"name": entry.name, "path": rel, "type": "file", "size": entry.stat().st_size})
    except Exception:
        pass
    return result

def pip_run(args, cwd=None):
    """Run pip; retry with --break-system-packages on PEP 668 hosts."""
    base = [sys.executable, "-m", "pip", "install"]
    res = subprocess.run(base + args, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0 and "externally-managed" in (res.stderr or ""):
        res = subprocess.run(base + ["--break-system-packages"] + args, cwd=cwd, capture_output=True, text=True)
    return res

def _auto_install_from_files(name):
    """After upload: install modules from requirements.txt / package.json (runs in background, logs to console)."""
    extract_dir = SERVERS_DIR / name / "extracted"
    log_path = SERVERS_DIR / name / "logs.txt"
    def log(msg):
        with open(log_path, "a") as lf:
            lf.write(msg)
    try:
        reqs = [p for p in extract_dir.rglob("requirements.txt") if "node_modules" not in p.parts]
        reqs.sort(key=lambda p: len(p.parts))
        if reqs:
            req = reqs[0]
            log(f"\n[AUTO-INSTALL] requirements.txt found ({req.relative_to(extract_dir)}). Installing modules...\n")
            res = pip_run(["-r", str(req)], cwd=str(req.parent))
            if res.returncode == 0:
                log("[AUTO-INSTALL] DONE: all modules installed successfully\n")
            else:
                log((res.stderr or res.stdout or "")[-1500:] + "\n[AUTO-INSTALL] FAILED: some modules could not be installed\n")
        pkgs = [p for p in extract_dir.rglob("package.json") if "node_modules" not in p.parts]
        pkgs.sort(key=lambda p: len(p.parts))
        if pkgs:
            pj = pkgs[0]
            log(f"\n[AUTO-INSTALL] package.json found. Running npm install...\n")
            res = subprocess.run(["npm", "install"], cwd=str(pj.parent), capture_output=True, text=True)
            if res.returncode == 0:
                log("[AUTO-INSTALL] DONE: npm modules installed successfully\n")
            else:
                log((res.stderr or res.stdout or "")[-1500:] + "\n[AUTO-INSTALL] FAILED: npm install error\n")
    except Exception as e:
        log(f"[AUTO-INSTALL] ERROR: {e}\n")

@app.route("/server/<name>/upload", methods=["POST"])
@login_required
def upload_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400
    
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"success": False, "error": "Empty filename"}), 400
    
    extract_dir = SERVERS_DIR / name / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    
    plan_cfg = _get_plan(cfg)
    limit = plan_cfg["storage_gb"] * 1024 ** 3
    incoming = request.content_length or 0
    if _dir_size(SERVERS_DIR / name) + incoming > limit:
        return jsonify({"success": False, "error": f"Storage full ({plan_cfg['storage_gb']}GB). Plan upgrade korun ba purono file delete korun."})

    upload_path = SERVERS_DIR / name / f"upload_{f.filename}"
    f.save(upload_path)
    
    extracted_files = []
    
    if f.filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(upload_path, "r") as z:
                for member in z.infolist():
                    if member.filename.startswith(("/", "\\", "..", "../")):
                        upload_path.unlink(missing_ok=True)
                        return jsonify({"success": False, "error": "Invalid zip path"})
                
                z.extractall(extract_dir)
                for member in z.infolist():
                    if not member.is_dir():
                        extracted_files.append(member.filename)
            upload_path.unlink(missing_ok=True)
        except Exception as e:
            upload_path.unlink(missing_ok=True)
            return jsonify({"success": False, "error": f"Zip extraction failed: {str(e)}"}), 500
    else:
        dest = extract_dir / f.filename
        shutil.move(str(upload_path), str(dest))
        extracted_files = [f.filename]
        
    if not cfg.get("main_file"):
        cfg["main_file"] = auto_detect_main_file(extract_dir)
        data['servers'][name] = cfg
        save_data(data)

    threading.Thread(target=_auto_install_from_files, args=(name,), daemon=True).start()
    return redirect(url_for('server_detail', name=name))


@app.route("/server/<name>/settings", methods=["POST"])
@login_required
def save_settings(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    payload = request.get_json()
    cfg["main_file"] = payload.get("main_file", cfg.get("main_file", ""))
    cfg["main_command"] = payload.get("main_command", cfg.get("main_command", ""))
    cfg["port"] = payload.get("port", cfg.get("port", 8080))
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

@app.route("/server/<name>/start", methods=["POST"])
@login_required
def start_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    pid = cfg.get("pid")
    if pid and is_process_alive(pid):
        return jsonify({"success": False, "error": "Already running"})
    
    extract_dir = SERVERS_DIR / name / "extracted"
    main_file = cfg.get("main_file") or auto_detect_main_file(extract_dir)
    main_cmd = cfg.get("main_command") or ""
    main_path = extract_dir / main_file
    
    if not main_path.exists():
        return jsonify({"success": False, "error": f"{main_file} not found. Upload your files first."})
    
    log_path = SERVERS_DIR / name / "logs.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    if main_cmd:
        cmd = shlex.split(main_cmd)
    else:
        cmd = get_run_command(cfg.get("runtime", "python"), main_file)
    
    env = prepare_environment_and_deps(extract_dir, log_path, cfg.get("runtime", "python"))
    env["PORT"] = str(_ensure_port_unique(data, name, cfg))
    
    try:
        with open(log_path, "a") as lf:
            lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] Starting: {' '.join(cmd)}\n{'='*50}\n")
        log_file = open(log_path, "a")
        proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdout=log_file, stderr=log_file, env=env, preexec_fn=os.setsid)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        threading.Thread(target=_limiter, args=(name, proc.pid), daemon=True).start()
        cfg["main_file"] = main_file
        data["servers"][name] = cfg
        save_data(data)
        return jsonify({"success": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/server/<name>/stop", methods=["POST"])
@login_required
def stop_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False}), 403
    
    pid = cfg.get("pid")
    stopped = False
    
    if name in RUNNING_PROCESSES:
        entry = RUNNING_PROCESSES[name]
        proc = entry["proc"]
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            entry["log_file"].close()
        except Exception:
            pass
        del RUNNING_PROCESSES[name]
        stopped = True
    
    if pid and not stopped:
        kill_process(pid)
    
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        with open(log_path, "a") as lf:
            lf.write(f"[{datetime.now().isoformat()}] Server stopped\n")
    except Exception:
        pass
    
    cfg["status"] = "stopped"
    cfg["pid"] = None
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

# ==================== LOGS & EXECUTION ====================
@app.route("/server/<name>/logs")
@login_required
def get_logs(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"logs": "Server not found"})
    
    log_path = SERVERS_DIR / name / "logs.txt"
    if not log_path.exists():
        return jsonify({"logs": "No logs yet. Start the server to see output."})
    
    try:
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 50000), os.SEEK_SET)
            content = f.read().decode('utf-8', errors='ignore')
            
        lines = content.splitlines()
        if len(lines) > 200:
            lines = lines[-200:]
            content = "... (showing last 200 lines) ...\n" + "\n".join(lines)
        return jsonify({"logs": content or "No output yet."})
    except Exception as e:
        return jsonify({"logs": f"Error reading logs: {e}"})

@app.route("/server/<name>/exec", methods=["POST"])
@login_required
def exec_command(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json() or {}
    cmd_text = payload.get("command", "").strip()

    if not cmd_text:
        return jsonify({"success": False, "error": "No command specified"})

    log_path = SERVERS_DIR / name / "logs.txt"
    extract_dir = SERVERS_DIR / name / "extracted"

    try:
        with open(log_path, "a") as lf:
            lf.write(f"\n$ {cmd_text}\n")

        # shlex ব্যবহার করে নিরাপদে আর্গুমেন্ট পার্স করা হচ্ছে
        cmd_args = shlex.split(cmd_text)
        with open(log_path, "a") as lf:
            subprocess.Popen(
                cmd_args,
                cwd=str(extract_dir),
                stdout=lf,
                stderr=lf
            )
        return jsonify({"success": True})
    except Exception as e:
        with open(log_path, "a") as lf:
            lf.write(f"Command execution error: {e}\n")
        return jsonify({"success": False, "error": str(e)})

@app.route("/server/<name>/logs/clear", methods=["POST"])
@login_required
def clear_logs(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False})
    
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        log_path.write_text("")
    except Exception:
        pass
    return jsonify({"success": True})

# ==================== PACKAGE MANAGER ENDPOINTS ====================

@app.route("/server/<name>/packages", methods=["GET"])
@login_required
def list_packages(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    runtime = cfg.get("runtime", "python")
    installed = []

    try:
        if runtime == "node":
            extract_dir = SERVERS_DIR / name / "extracted"
            pkg_json = extract_dir / "package.json"
            if pkg_json.exists():
                pdata = json.loads(pkg_json.read_text())
                deps = pdata.get("dependencies", {})
                for pkg, ver in deps.items():
                    installed.append({"name": pkg, "version": ver})
        else:
            req_file = SERVERS_DIR / name / "extracted" / "requirements.txt"
            if req_file.exists():
                lines = req_file.read_text().splitlines()
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("==")
                        pkg_name = parts[0].strip()
                        ver = parts[1].strip() if len(parts) > 1 else "latest"
                        installed.append({"name": pkg_name, "version": ver})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": True, "packages": installed, "runtime": runtime})

PKG_RE = re.compile(r"^[A-Za-z0-9@/_.\-]+$")

def _run_install(name, runtime, pkg_name, version):
    extract_dir = SERVERS_DIR / name / "extracted"
    log_path = SERVERS_DIR / name / "logs.txt"
    label = f"{pkg_name}=={version}" if (version and runtime != "node") else (f"{pkg_name}@{version}" if version else pkg_name)
    def log(msg):
        with open(log_path, "a") as lf:
            lf.write(msg)
    try:
        log(f"\n[PACKAGE] Installing {label} ...\n")
        if runtime == "node":
            cmd = ["npm", "install", label]
            res = subprocess.run(cmd, cwd=str(extract_dir), capture_output=True, text=True)
        else:
            res = pip_run([label])
        if res.returncode != 0:
            log((res.stderr or res.stdout or "")[-1500:] + f"\n[PACKAGE] FAILED: {label}\n")
            return
        if runtime != "node":
            req_file = extract_dir / "requirements.txt"
            lines = req_file.read_text().splitlines() if req_file.exists() else []
            lines = [l for l in lines if not re.match(rf"^{re.escape(pkg_name)}(==|$)", l.strip(), re.I)]
            lines.append(label)
            req_file.write_text("\n".join(lines) + "\n")
        log(f"[PACKAGE] DONE: {label} installed successfully\n")
    except Exception as e:
        log(f"[PACKAGE] ERROR: {e}\n")

@app.route("/server/<name>/packages/install", methods=["POST"])
@login_required
def install_package(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json() or {}
    pkg_name = payload.get("name", "").strip()
    version = payload.get("version", "").strip()

    if not pkg_name or not PKG_RE.match(pkg_name) or (version and not PKG_RE.match(version)):
        return jsonify({"success": False, "error": "Invalid package name"}), 400

    runtime = cfg.get("runtime", "python")
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_run_install, args=(name, runtime, pkg_name, version), daemon=True).start()
    return jsonify({"success": True, "message": f"Installing {pkg_name}... check LOGS"})

def _install_requirements_file(name, req_path=None):
    """Install modules from requirements.txt (background), logging to the server's logs."""
    extract_dir = SERVERS_DIR / name / "extracted"
    log_path = SERVERS_DIR / name / "logs.txt"
    def log(msg):
        with open(log_path, "a") as lf:
            lf.write(msg)
    try:
        if req_path is None:
            reqs = [p for p in extract_dir.rglob("requirements.txt") if "node_modules" not in p.parts]
            reqs.sort(key=lambda p: len(p.parts))
            req_path = reqs[0] if reqs else None
        if not req_path or not Path(req_path).exists():
            log("\n[REQUIREMENTS] requirements.txt not found. Upload your files first.\n")
            return
        req_path = Path(req_path)
        log(f"\n[REQUIREMENTS] Installing from {req_path.relative_to(extract_dir)} ...\n")
        res = pip_run(["-r", str(req_path)], cwd=str(req_path.parent))
        if res.returncode == 0:
            log("[REQUIREMENTS] DONE: all modules installed successfully\n")
        else:
            log((res.stderr or res.stdout or "")[-1500:] + "\n[REQUIREMENTS] FAILED: some modules could not be installed\n")
    except Exception as e:
        log(f"[REQUIREMENTS] ERROR: {e}\n")

@app.route("/server/<name>/requirements/install", methods=["POST"])
@login_required
def install_requirements(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    extract_dir = SERVERS_DIR / name / "extracted"
    if not (extract_dir / "requirements.txt").exists() and not any(
        "node_modules" not in p.parts for p in extract_dir.rglob("requirements.txt")
    ):
        return jsonify({"success": False, "error": "requirements.txt not found. Upload your files first."})

    threading.Thread(target=_install_requirements_file, args=(name,), daemon=True).start()
    return jsonify({"success": True, "message": "Installing requirements.txt... check LOGS"})

def _safe_target(base, rel):
    """extracted folder er bhitore valid path ney, na holey None."""
    rel = str(rel or "").strip().strip("/")
    if not rel:
        return None
    target = (base / rel).resolve()
    if target == base or base not in target.parents:
        return None
    return target

@app.route("/server/<name>/file/delete", methods=["POST"])
@login_required
def delete_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    base = (SERVERS_DIR / name / "extracted").resolve()
    target = _safe_target(base, (request.get_json() or {}).get("filename", ""))
    if not target or not target.exists():
        return jsonify({"success": False, "error": "Not found"}), 404
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True})

@app.route("/server/<name>/files/delete-bulk", methods=["POST"])
@login_required
def delete_files_bulk(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    names = (request.get_json() or {}).get("filenames", [])
    if not isinstance(names, list) or not names:
        return jsonify({"success": False, "error": "Kono file select kora nai"}), 400

    base = (SERVERS_DIR / name / "extracted").resolve()
    # Parent (chhoto path age) theke shuru; folder age delete hole tar bhitorer item-er alada delete lagbe na
    rels = sorted({str(r).strip().strip("/") for r in names if str(r).strip()}, key=len)
    deleted, failed, removed_dirs = 0, [], []
    for rel in rels:
        if any(rel.startswith(d + "/") for d in removed_dirs):
            continue  # already parent folder er sathe delete hoye geche
        target = _safe_target(base, rel)
        if not target or not target.exists():
            failed.append(rel)
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
                removed_dirs.append(rel)
            else:
                target.unlink()
            deleted += 1
        except OSError:
            failed.append(rel)
    return jsonify({"success": True, "deleted": deleted, "failed": failed})

@app.route("/server/<name>/packages/remove", methods=["POST"])
@login_required
def remove_package(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json() or {}
    pkg_name = payload.get("name", "").strip()

    if not pkg_name:
        return jsonify({"success": False, "error": "Package name is required"}), 400

    runtime = cfg.get("runtime", "python")
    extract_dir = SERVERS_DIR / name / "extracted"

    try:
        if runtime == "node":
            cmd = ["npm", "uninstall", pkg_name]
            res = subprocess.run(cmd, cwd=str(extract_dir), capture_output=True, text=True)
            if res.returncode != 0:
                return jsonify({"success": False, "error": res.stderr or "Failed to remove NPM package"})
        else:
            req_file = extract_dir / "requirements.txt"
            if req_file.exists():
                lines = req_file.read_text().splitlines()
                lines = [l for l in lines if not l.startswith(pkg_name + "==") and l != pkg_name]
                req_file.write_text("\n".join(lines))

        return jsonify({"success": True, "message": f"Package {pkg_name} removed successfully!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ==================== PUBLIC URL (server er web/API proxy) ====================
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_HOP_HEADERS = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer",
                "upgrade", "proxy-authorization", "proxy-authenticate", "content-length", "content-encoding"}

def _public_base():
    custom = os.environ.get("PUBLIC_BASE_URL")  # jemon https://host.apurbo.indevs.in
    if custom:
        return custom.rstrip("/")
    dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if dom:
        return dom if dom.startswith("http") else f"https://{dom}"
    return request.host_url.rstrip("/")

def _public_url(name):
    return f"{_public_base()}/s/{name}/"

app.jinja_env.globals["public_url"] = _public_url

@app.route("/s/<name>/", defaults={"subpath": ""}, methods=PROXY_METHODS)
@app.route("/s/<name>/<path:subpath>", methods=PROXY_METHODS)
def public_proxy(name, subpath):
    data = load_data()
    cfg = data.get("servers", {}).get(name)
    if not cfg:
        return "Server not found", 404
    pid = cfg.get("pid")
    port = cfg.get("port")
    if not pid or not port or not is_process_alive(pid):
        return "Server offline ache. Owner ke bolun Start korte.", 503

    target = f"http://127.0.0.1:{port}/{subpath}"
    if request.query_string:
        target += "?" + request.query_string.decode()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
    headers["X-Forwarded-For"] = request.remote_addr or ""
    headers["X-Forwarded-Proto"] = request.scheme
    headers["X-Forwarded-Host"] = request.host
    body = request.get_data() if request.method not in ("GET", "HEAD") else None
    req = urllib.request.Request(target, data=body, headers=headers, method=request.method)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        resp = e
    except Exception:
        return "Server e connect kora jacche na. App ti port listen korche kina check korun.", 502

    try:
        content = resp.read() if request.method != "HEAD" else b""
        status = getattr(resp, "status", None) or resp.code
        out_headers = []
        for k, v in resp.headers.items():
            kl = k.lower()
            if kl in _HOP_HEADERS:
                continue
            if kl == "location" and v.startswith("/") and not v.startswith("//"):
                v = f"/s/{name}{v}"
            out_headers.append((k, v))
        return Response(content, status=status, headers=out_headers)
    finally:
        resp.close()

# ==================== PAYMENT (bKash, manual verify) ====================
def _owned_server(data, name):
    cfg = data.get("servers", {}).get(name)
    if not cfg:
        return None, ("Server not found", 404)
    if cfg.get("owner") != session.get("username"):
        return None, ("Access denied", 403)
    return cfg, None

@app.route("/server/<name>/upgrade")
@login_required
def upgrade_server(name):
    data = load_data()
    cfg, err = _owned_server(data, name)
    if err:
        return err
    plans = _plan_list(data)
    history = [o for o in data.get("orders", []) if o.get("server") == name]
    history.sort(key=lambda o: o["created"], reverse=True)
    return render_template("upgrade.html", name=name, current_plan=cfg.get("plan", DEFAULT_PLAN),
                           paid_plans=[p for p in plans if not p["free"]], bkash=BKASH_NUMBER,
                           selected=request.args.get("plan", ""), sent=request.args.get("sent"),
                           error=request.args.get("error"), history=history, site_name=site_name_value())

@app.route("/server/<name>/order", methods=["POST"])
@login_required
def create_order(name):
    data = load_data()
    cfg, err = _owned_server(data, name)
    if err:
        return err
    def back(msg):
        return redirect(url_for("upgrade_server", name=name, error=msg))
    plan = request.form.get("plan", "")
    sender = re.sub(r"\D", "", request.form.get("sender", ""))
    trx = request.form.get("trx", "").strip().upper()
    if plan not in PLANS or plan == DEFAULT_PLAN:
        return back("Ekta paid plan select korun")
    if _plan_price(data, plan) <= 0:
        return back("Ei plan er daam ekhono set kora hoyni")
    if not re.fullmatch(r"01[3-9]\d{8}", sender):
        return back("bKash number thik nai (01XXXXXXXXX)")
    if not re.fullmatch(r"[A-Z0-9]{6,20}", trx):
        return back("TrxID thik nai")
    orders = data.setdefault("orders", [])
    if any(o.get("trx") == trx for o in orders):
        return back("Ei TrxID agei use hoyeche")
    orders.append({
        "id": secrets.token_hex(4), "username": session["username"], "server": name,
        "plan": plan, "amount": _plan_price(data, plan), "sender": sender, "trx": trx,
        "status": "pending", "created": datetime.now().isoformat()
    })
    save_data(data)
    return redirect(url_for("upgrade_server", name=name, sent=1))

ACCESS_KEY_CHARS = string.digits + string.ascii_lowercase   # 1234567890 + abcdefghijklmnopqrstuvwxyz
ACCESS_KEY_DAYS = (7, 15, 30)

PORT_RANGE = range(20000, 30000)  # prottek server er internal port (public e proxy hoy)

def _used_ports(data, exclude=None):
    return {c.get("port") for n, c in data.get("servers", {}).items() if n != exclude}

def _allocate_port(data, exclude=None):
    used = _used_ports(data, exclude)
    for port in PORT_RANGE:
        if port not in used:
            return port
    raise RuntimeError("No free port left")

def _ensure_port_unique(data, name, cfg):
    if not cfg.get("port") or cfg["port"] in _used_ports(data, exclude=name):
        cfg["port"] = _allocate_port(data, exclude=name)
        save_data(data)
    return cfg["port"]

def _new_access_key():
    return "APURBO" + "".join(secrets.choice(ACCESS_KEY_CHARS) for _ in range(12))

def _account_expired(username):
    user = load_data().get("users", {}).get(username)
    if not user or not user.get("expires_at"):
        return False
    try:
        return datetime.now() > datetime.fromisoformat(user["expires_at"])
    except ValueError:
        return False

@app.route("/admin/keys/generate", methods=["POST"])
def admin_generate_keys():
    if not _is_admin():
        return redirect(url_for("admin_page"))
    try:
        count = max(1, min(50, int(request.form.get("count", "1"))))
    except ValueError:
        count = 1
    note = request.form.get("note", "").strip()[:40]
    try:
        days = int(request.form.get("days", "0"))
    except ValueError:
        days = 0
    if days not in ACCESS_KEY_DAYS:
        return redirect(url_for("admin_page", error="7, 15 ba 30 din select korun"))
    data = load_data()
    keys = data.setdefault("access_keys", [])
    existing = {k["key"] for k in keys}
    for _ in range(count):
        k = _new_access_key()
        while k in existing:
            k = _new_access_key()
        existing.add(k)
        keys.append({"key": k, "note": note, "days": days, "created": datetime.now().isoformat(),
                     "used_by": None, "used_at": None, "expires_at": None})
    save_data(data)
    return redirect(url_for("admin_page"))

def _is_admin():
    return session.get("is_admin") is True

@app.route("/admin")
def admin_page():
    data = load_data()
    site = site_name_value()
    if not _is_admin():
        return render_template("admin.html", logged_in=False, error=request.args.get("error"),
                               site_name=site, plans=[], orders=[], disabled=not ADMIN_PASSWORD)
    orders = sorted(data.get("orders", []), key=lambda o: o["created"], reverse=True)
    paid = [p for p in _plan_list(data) if not p["free"]]
    keys = sorted(data.get("access_keys", []), key=lambda k: k["created"], reverse=True)
    return render_template("admin.html", logged_in=True, error=request.args.get("error"),
                           site_name=site, plans=paid, orders=orders, keys=keys, disabled=False)

@app.route("/admin/login", methods=["POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        return redirect(url_for("admin_page", error="Admin disabled: ADMIN_PASSWORD set kora nai"))
    u = request.form.get("username", "").strip()
    pw = request.form.get("password", "")
    if u == ADMIN_USERNAME and hmac.compare_digest(pw, ADMIN_PASSWORD):
        session["is_admin"] = True
        return redirect(url_for("admin_page"))
    return redirect(url_for("admin_page", error="Wrong admin login"))

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_page"))

@app.route("/admin/order/<oid>/<action>", methods=["POST"])
def admin_order(oid, action):
    if not _is_admin():
        return redirect(url_for("admin_page"))
    data = load_data()
    order = next((o for o in data.get("orders", []) if o.get("id") == oid), None)
    if not order or order.get("status") != "pending" or action not in ("approve", "reject"):
        return redirect(url_for("admin_page"))
    if action == "approve":
        cfg = data.get("servers", {}).get(order["server"])
        if cfg:
            cfg["plan"] = order["plan"]
        order["status"] = "approved"
    else:
        order["status"] = "rejected"
    order["handled"] = datetime.now().isoformat()
    save_data(data)
    return redirect(url_for("admin_page"))

@app.route("/admin/prices", methods=["POST"])
def admin_prices():
    if not _is_admin():
        return redirect(url_for("admin_page"))
    data = load_data()
    prices = data.setdefault("settings", {}).setdefault("plan_prices", {})
    for pid in PLANS:
        if pid == DEFAULT_PLAN:
            continue
        v = re.sub(r"\D", "", request.form.get(pid, ""))
        if v:
            prices[pid] = int(v)
    save_data(data)
    return redirect(url_for("admin_page"))

@app.route("/server/<name>/usage")
@login_required
def server_usage(name):
    data = load_data()
    cfg, err = _owned_server(data, name)
    if err:
        return jsonify({"error": err[0]}), err[1]
    plan = _get_plan(cfg)
    pid = cfg.get("pid")
    running = bool(pid and is_process_alive(pid))
    live = LIVE_STATS.get(name) if running else None
    storage_mb = _dir_size(SERVERS_DIR / name) / 1048576
    return jsonify({
        "running": running,
        "ram_used_mb": live["ram_mb"] if live else 0,
        "ram_limit_mb": plan["ram_mb"],
        "cpu_used_pct": live["cpu_pct"] if live else 0,
        "cpu_limit_pct": plan["cpu_pct"],
        "storage_used_mb": round(storage_mb, 2),
        "storage_limit_mb": plan["storage_gb"] * 1024,
        "plan_label": plan["label"],
    })

# ==================== PWA (app install) ====================
@app.route("/manifest.webmanifest")
def pwa_manifest():
    return send_from_directory(BASE_DIR, "manifest.webmanifest", mimetype="application/manifest+json")

PWA_FILES = {"icon-192.png", "icon-512.png", "maskable-512.png", "apple-touch-icon.png", "favicon-64.png", "logo.png"}

@app.route("/<filename>")
def pwa_image(filename):
    if filename not in PWA_FILES:
        return "Not found", 404
    return send_from_directory(BASE_DIR, filename, mimetype="image/png")

@app.route("/sw.js")
def pwa_sw():
    resp = send_from_directory(BASE_DIR, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
