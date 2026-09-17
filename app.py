import os
import json
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
from pathlib import Path
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"
SERVERS_DIR = BASE_DIR / "servers"
SERVERS_DIR.mkdir(exist_ok=True)

NORMAL_PASSWORD = os.environ.get("NORMAL_PASSWORD", "Rixor2")
DEFAULT_THEME = "#ff2222"

RUNNING_PROCESSES = {}


def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {
        "servers": {},
        "users": {},
        "settings": {
            "maintenance": False,
            "maintenance_msg": "System under maintenance.",
            "theme_color": DEFAULT_THEME,
            "normal_password": NORMAL_PASSWORD,
            "site_name": "RIXOR HOST",
            "auto_restart_interval": 300
        }
    }


def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))


def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def get_theme_color():
    data = load_data()
    return data.get("settings", {}).get("theme_color", DEFAULT_THEME)


@app.context_processor
def inject_theme():
    return {"theme_color": get_theme_color()}


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        data = load_data()
        settings = data.get("settings", {})
        if settings.get("maintenance"):
            return render_template(
                "maintenance.html",
                message=settings.get("maintenance_msg", "Under maintenance"),
                site_name=settings.get("site_name", "RIXOR HOST"),
                theme_color=get_theme_color()
            )
        return f(*args, **kwargs)
    return decorated


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
    if existing_pythonpath:
        env["PYTHONPATH"] = new_pythonpath + os.pathsep + existing_pythonpath
    else:
        env["PYTHONPATH"] = new_pythonpath

    try:
        if runtime == "node":
            pkg_json = extract_dir / "package.json"
            node_modules = extract_dir / "node_modules"
            if pkg_json.exists() and not node_modules.exists():
                with open(log_path, "a") as lf:
                    lf.write("\n[SYSTEM] Installing Node.js dependencies via npm install...\n")
                subprocess.run(["npm", "install"], cwd=str(extract_dir),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            req_file = extract_dir / "requirements.txt"
            if req_file.exists():
                with open(log_path, "a") as lf:
                    lf.write("\n[SYSTEM] Installing Python dependencies via pip install...\n")
                subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                               cwd=str(extract_dir),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        with open(log_path, "a") as lf:
            lf.write("\n[SYSTEM WARNING] Dependency auto-install error: " + str(e) + "\n")

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


def _ensure_theme():
    data = load_data()
    if data.get("settings", {}).get("theme_color") != DEFAULT_THEME:
        data.setdefault("settings", {})["theme_color"] = DEFAULT_THEME
        save_data(data)


_ensure_theme()


# ==================== KEEP ALIVE ====================

def render_keep_alive():
    while True:
        try:
            time.sleep(600)
            port = os.environ.get("PORT", 5000)
            url = "http://127.0.0.1:" + str(port) + "/api/ping"
            req = urllib.request.Request(url, headers={"User-Agent": "Render-KeepAlive/1.0"})
            urllib.request.urlopen(req, timeout=10)

            external_url = os.environ.get("RENDER_EXTERNAL_URL")
            if external_url:
                ping_url = external_url + "/api/ping"
                req2 = urllib.request.Request(ping_url, headers={"User-Agent": "Render-KeepAlive/1.0"})
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
            url = os.environ.get("RENDER_EXTERNAL_URL")
            if url:
                ping_url = url + "/api/ping"
            else:
                port = os.environ.get("PORT", 5000)
                ping_url = "http://127.0.0.1:" + str(port) + "/api/ping"
            req = urllib.request.Request(ping_url, headers={"User-Agent": "KeepAlive-Bot/1.0"})
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass


threading.Thread(target=keep_alive, daemon=True).start()


# ==================== AUTO RESTART ====================

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
        env["PORT"] = str(cfg.get("port", 8080))

        with open(log_path, "a") as lf:
            lf.write("\n" + "=" * 50 + "\n")
            lf.write("[" + datetime.now().isoformat() + "] AUTO-RESTART triggered\n")
            lf.write("=" * 50 + "\n")

        log_file = open(log_path, "a")
        proc = subprocess.Popen(cmd, cwd=str(extract_dir),
                                stdout=log_file, stderr=log_file,
                                env=env, preexec_fn=os.setsid)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        data["servers"][name] = cfg
        save_data(data)
    except Exception as e:
        print("Auto-restart error for " + name + ": " + str(e))


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


# ==================== LOGIN ====================

@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("username"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        password = request.form.get("password", "").strip()
        data = load_data()
        settings = data.get("settings", {})
        normal_pass = settings.get("normal_password", NORMAL_PASSWORD)

        if password != normal_pass:
            return render_template(
                "login.html",
                error="Wrong password",
                theme_color=get_theme_color(),
                site_name=settings.get("site_name", "RIXOR HOST")
            )

        username = "admin"
        user = data["users"].get(username)
        if not user:
            data["users"][username] = {
                "joined": datetime.now().isoformat(),
                "password_hash": hash_password(password)
            }
            save_data(data)
        else:
            if user.get("password_hash") != hash_password(password):
                data["users"][username]["password_hash"] = hash_password(password)
                save_data(data)

        session["username"] = username
        return redirect(url_for("dashboard"))

    data = load_data()
    settings = data.get("settings", {})
    return render_template(
        "login.html",
        error=None,
        theme_color=get_theme_color(),
        site_name=settings.get("site_name", "RIXOR HOST")
    )


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
    site_name = settings.get("site_name", "RIXOR HOST")
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
    return render_template(
        "dashboard.html",
        servers=user_servers,
        running=running,
        total=len(user_servers),
        username=username,
        site_name=site_name,
        theme_color=get_theme_color()
    )


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
    if not name:
        return redirect(url_for("dashboard"))

    data = load_data()
    if name in data["servers"]:
        return redirect(url_for("dashboard"))

    cfg = {
        "name": name,
        "owner": session["username"],
        "runtime": runtime,
        "status": "stopped",
        "main_file": "",
        "main_command": "",
        "port": 8080,
        "pid": None,
        "created": datetime.now().isoformat()
    }
    data["servers"][name] = cfg
    save_data(data)
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
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


def list_files(directory, base=""):
    result = []
    if not directory.exists():
        return result
    try:
        for entry in sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name)):
            rel = base + "/" + entry.name if base else entry.name
            if entry.is_dir():
                result.append({"name": entry.name, "path": rel, "type": "dir", "size": 0})
                result.extend(list_files(entry, rel))
            else:
                result.append({"name": entry.name, "path": rel, "type": "file", "size": entry.stat().st_size})
    except Exception:
        pass
    return result


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

    return render_template(
        "server.html",
        server_name=name,
        config=cfg,
        files=files,
        theme_color=get_theme_color()
    )


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

    upload_path = SERVERS_DIR / name / ("upload_" + f.filename)
    f.save(upload_path)

    if f.filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(upload_path, "r") as z:
                for member in z.infolist():
                    if member.filename.startswith(("/", "\\", "..", "../")):
                        upload_path.unlink(missing_ok=True)
                        return jsonify({"success": False, "error": "Invalid zip path"})
                z.extractall(extract_dir)
            upload_path.unlink(missing_ok=True)
        except Exception as e:
            upload_path.unlink(missing_ok=True)
            return jsonify({"success": False, "error": "Zip extraction failed: " + str(e)}), 500
    else:
        dest = extract_dir / f.filename
        shutil.move(str(upload_path), str(dest))

    if not cfg.get("main_file"):
        cfg["main_file"] = auto_detect_main_file(extract_dir)
        data["servers"][name] = cfg
        save_data(data)

    return redirect(url_for("server_detail", name=name))


# ==================== FILE MANAGER ====================

def _safe_path(server_name, rel_path):
    base = (SERVERS_DIR / server_name / "extracted").resolve()
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        return None
    return target


@app.route("/server/<name>/file/read", methods=["GET"])
@login_required
def read_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    rel = request.args.get("path", "").strip()
    path = _safe_path(name, rel)
    if not path or not path.is_file():
        return jsonify({"success": False, "error": "File not found"}), 404

    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        return jsonify({"success": True, "content": content, "path": rel})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/server/<name>/file/save", methods=["POST"])
@login_required
def save_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json() or {}
    rel = payload.get("path", "").strip()
    content = payload.get("content", "")

    path = _safe_path(name, rel)
    if not path:
        return jsonify({"success": False, "error": "Invalid path"}), 400

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return jsonify({"success": True, "message": "Saved " + rel})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/server/<name>/file/delete", methods=["POST"])
@login_required
def delete_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json() or {}
    rel = payload.get("path", "").strip()
    path = _safe_path(name, rel)
    if not path or not path.exists():
        return jsonify({"success": False, "error": "File not found"}), 404

    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return jsonify({"success": True, "message": "Deleted " + rel})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/server/<name>/file/rename", methods=["POST"])
@login_required
def rename_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json() or {}
    rel = payload.get("path", "").strip()
    new_name = payload.get("new_name", "").strip()

    if not rel or not new_name:
        return jsonify({"success": False, "error": "path and new_name required"}), 400

    if "/" in new_name or "\\" in new_name or ".." in new_name:
        return jsonify({"success": False, "error": "Invalid name"}), 400

    old_path = _safe_path(name, rel)
    if not old_path or not old_path.exists():
        return jsonify({"success": False, "error": "File not found"}), 404

    new_path = old_path.parent / new_name
    if new_path.exists():
        return jsonify({"success": False, "error": "File already exists"}), 400

    try:
        old_path.rename(new_path)
        return jsonify({"success": True, "message": "Renamed to " + new_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ==================== SETTINGS / START / STOP ====================

@app.route("/server/<name>/settings", methods=["POST"])
@login_required
def save_settings(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json() or {}
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
        return jsonify({"success": False, "error": main_file + " not found. Upload files first."})

    log_path = SERVERS_DIR / name / "logs.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if main_cmd:
        cmd = shlex.split(main_cmd)
    else:
        cmd = get_run_command(cfg.get("runtime", "python"), main_file)

    env = prepare_environment_and_deps(extract_dir, log_path, cfg.get("runtime", "python"))
    env["PORT"] = str(cfg.get("port", 8080))

    try:
        with open(log_path, "a") as lf:
            lf.write("\n" + "=" * 50 + "\n")
            lf.write("[" + datetime.now().isoformat() + "] Starting: " + " ".join(cmd) + "\n")
            lf.write("=" * 50 + "\n")

        log_file = open(log_path, "a")
        proc = subprocess.Popen(cmd, cwd=str(extract_dir),
                                stdout=log_file, stderr=log_file,
                                env=env, preexec_fn=os.setsid)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
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
            lf.write("[" + datetime.now().isoformat() + "] Server stopped\n")
    except Exception:
        pass

    cfg["status"] = "stopped"
    cfg["pid"] = None
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})


# ==================== LOGS & EXEC ====================

@app.route("/server/<name>/logs")
@login_required
def get_logs(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"logs": "Server not found"})

    log_path = SERVERS_DIR / name / "logs.txt"
    if not log_path.exists():
        return jsonify({"logs": "No logs yet."})

    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 50000), os.SEEK_SET)
            content = f.read().decode("utf-8", errors="ignore")

        lines = content.splitlines()
        if len(lines) > 200:
            lines = lines[-200:]
            content = "... (last 200 lines) ...\n" + "\n".join(lines)
        return jsonify({"logs": content or "No output yet."})
    except Exception as e:
        return jsonify({"logs": "Error: " + str(e)})


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
        return jsonify({"success": False, "error": "No command"})

    log_path = SERVERS_DIR / name / "logs.txt"
    extract_dir = SERVERS_DIR / name / "extracted"

    try:
        with open(log_path, "a") as lf:
            lf.write("\n$ " + cmd_text + "\n")
            cmd_args = shlex.split(cmd_text)
            subprocess.Popen(cmd_args, cwd=str(extract_dir), stdout=lf, stderr=lf)
        return jsonify({"success": True})
    except Exception as e:
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


# ==================== PACKAGES ====================

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
                for line in req_file.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("==")
                        installed.append({
                            "name": parts[0].strip(),
                            "version": parts[1].strip() if len(parts) > 1 else "latest"
                        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": True, "packages": installed, "runtime": runtime})


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

    if not pkg_name:
        return jsonify({"success": False, "error": "Package name required"}), 400

    runtime = cfg.get("runtime", "python")
    extract_dir = SERVERS_DIR / name / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        if runtime == "node":
            cmd = ["npm", "install", pkg_name + "@" + version if version else pkg_name]
            res = subprocess.run(cmd, cwd=str(extract_dir), capture_output=True, text=True)
            if res.returncode != 0:
                return jsonify({"success": False, "error": res.stderr or "NPM install failed"})
        else:
            target_pkg = pkg_name + "==" + version if version else pkg_name
            cmd = [sys.executable, "-m", "pip", "install", target_pkg]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                return jsonify({"success": False, "error": res.stderr or "PIP install failed"})

            req_file = extract_dir / "requirements.txt"
            lines = req_file.read_text().splitlines() if req_file.exists() else []
            new_entry = pkg_name + "==" + version if version else pkg_name
            lines = [l for l in lines if not l.startswith(pkg_name + "==") and l != pkg_name]
            lines.append(new_entry)
            req_file.write_text("\n".join(lines))

        return jsonify({"success": True, "message": "Installed " + pkg_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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
        return jsonify({"success": False, "error": "Package name required"}), 400

    runtime = cfg.get("runtime", "python")
    extract_dir = SERVERS_DIR / name / "extracted"

    try:
        if runtime == "node":
            cmd = ["npm", "uninstall", pkg_name]
            subprocess.run(cmd, cwd=str(extract_dir), capture_output=True, text=True)
        else:
            req_file = extract_dir / "requirements.txt"
            if req_file.exists():
                lines = req_file.read_text().splitlines()
                lines = [l for l in lines if not l.startswith(pkg_name + "==") and l != pkg_name]
                req_file.write_text("\n".join(lines))

        return jsonify({"success": True, "message": "Removed " + pkg_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)