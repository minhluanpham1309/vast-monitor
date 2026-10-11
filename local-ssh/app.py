"""Vast Monitor — theo dõi tiến trình thực nghiệm trên các máy vast.ai qua SSH.

Chạy:  ./run.sh   (hoặc: uvicorn app:app --host 127.0.0.1 --port 8765)
Mở:    http://127.0.0.1:8765
"""
import json
import os
import re
import shlex
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import paramiko
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
KEYS = DATA / "keys"
MFILE = DATA / "machines.json"

DEFAULT_PATTERN = r"python|torchrun|accelerate|deepspeed"
DEFAULT_EXCLUDE = r"jupyter-lab|jupyter-notebook|jupyter lab|jupyter notebook|tensorboard|supervisord|unattended-upgr"

DATA.mkdir(exist_ok=True)
KEYS.mkdir(exist_ok=True)
os.chmod(KEYS, 0o700)

# ---------------------------------------------------------------- storage
_store_lock = threading.Lock()


def load_machines() -> list:
    if not MFILE.exists():
        return []
    try:
        return json.loads(MFILE.read_text("utf-8"))
    except Exception:
        return []


def save_machines(ms: list) -> None:
    tmp = MFILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(ms, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(MFILE)


def get_machine(mid: str) -> dict:
    for m in load_machines():
        if m["id"] == mid:
            return m
    raise HTTPException(404, "Không tìm thấy máy")


def public(m: dict) -> dict:
    out = {k: v for k, v in m.items() if k not in ("passphrase",)}
    out["has_pasted_key"] = bool(m.get("key_file"))
    out["has_passphrase"] = bool(m.get("passphrase"))
    return out


def key_file_for(m: dict) -> Optional[str]:
    if m.get("key_file") and Path(m["key_file"]).exists():
        return m["key_file"]
    if m.get("key_path"):
        return os.path.expanduser(m["key_path"].strip())
    return None


def write_pasted_key(mid: str, text: str) -> str:
    text = text.replace("\r\n", "\n").strip() + "\n"
    p = KEYS / f"{mid}.key"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(p, 0o600)
    return str(p)


# ---------------------------------------------------------------- ssh pool
class SSHPool:
    def __init__(self):
        self.clients: dict = {}
        self.locks: dict = {}
        self.glock = threading.Lock()

    def _lock(self, mid):
        with self.glock:
            return self.locks.setdefault(mid, threading.Lock())

    def drop(self, mid):
        with self._lock(mid):
            c = self.clients.pop(mid, None)
            if c:
                try:
                    c.close()
                except Exception:
                    pass

    def _connect(self, m):
        c = paramiko.SSHClient()
        # máy vast.ai đổi liên tục nên tự chấp nhận host key
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = dict(
            hostname=m["host"].strip(),
            port=int(m.get("port") or 22),
            username=(m.get("user") or "root").strip(),
            timeout=10,
            banner_timeout=15,
            auth_timeout=15,
        )
        kf = key_file_for(m)
        if kf:
            if not Path(kf).exists():
                raise RuntimeError(f"Không thấy file key: {kf}")
            kw["key_filename"] = kf
            kw["look_for_keys"] = False
        if m.get("passphrase"):
            kw["passphrase"] = m["passphrase"]
        c.connect(**kw)
        c.get_transport().set_keepalive(30)
        return c

    def _get(self, m):
        mid = m["id"]
        with self._lock(mid):
            c = self.clients.get(mid)
            t = c.get_transport() if c else None
            if t is None or not t.is_active():
                if c:
                    try:
                        c.close()
                    except Exception:
                        pass
                c = self._connect(m)
                self.clients[mid] = c
            return c

    def run(self, m, cmd: str, stdin_data: Optional[str] = None, timeout: int = 30):
        for attempt in (0, 1):
            try:
                c = self._get(m)
                stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
                if stdin_data is not None:
                    stdin.write(stdin_data)
                    stdin.flush()
                    stdin.channel.shutdown_write()
                out = stdout.read().decode("utf-8", "replace")
                err = stderr.read().decode("utf-8", "replace")
                code = stdout.channel.recv_exit_status()
                return out, err, code
            except paramiko.AuthenticationException:
                self.drop(m["id"])
                raise
            except (paramiko.SSHException, OSError, EOFError):
                self.drop(m["id"])
                if attempt == 1:
                    raise


pool = SSHPool()


def friendly_error(e: Exception) -> str:
    if isinstance(e, paramiko.AuthenticationException):
        return "Sai key / không xác thực được (kiểm tra user, key, passphrase)"
    if isinstance(e, paramiko.PasswordRequiredException):
        return "Key có passphrase — hãy nhập passphrase"
    s = str(e) or e.__class__.__name__
    if "timed out" in s.lower():
        return "Hết thời gian kết nối (máy tắt, sai IP/port hoặc mạng chặn)"
    if "Connection refused" in s or "Errno 61" in s or "Errno 111" in s:
        return "Kết nối bị từ chối (sai port hoặc máy chưa chạy)"
    return s


# ---------------------------------------------------------------- remote script
STATUS_SCRIPT = r"""
export LC_ALL=C
echo "@@HOST"; hostname
echo "@@UPTIME"; cat /proc/uptime; cat /proc/loadavg; nproc 2>/dev/null
echo "@@MEM"; free -m 2>/dev/null | awk '/^Mem:/{print $2" "$3" "$7}'
echo "@@DISK"; df -P -m / /workspace 2>/dev/null | tail -n +2 | sort -u
echo "@@GPU"
nvidia-smi --query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit --format=csv,noheader,nounits 2>/dev/null
echo "@@GPUAPPS"
nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader,nounits 2>/dev/null
echo "@@PS"
ps -eo pid=,ppid=,user=,etimes=,pcpu=,pmem=,rss=,args= 2>/dev/null
echo "@@PROCINFO"
for p in $(ps -eo pid=,args= | grep -E -- "$PAT" | grep -v grep | awk '{print $1}'); do
  printf '%s|%s|%s|%s\n' "$p" "$(readlink /proc/$p/cwd 2>/dev/null)" "$(readlink /proc/$p/fd/1 2>/dev/null)" "$(readlink /proc/$p/fd/2 2>/dev/null)"
done
echo "@@TMUX"
tmux ls -F '#{session_name}|#{session_windows}|#{session_created}|#{session_attached}' 2>/dev/null
echo "@@PANES"
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}|#{pane_pid}|#{pane_current_command}' 2>/dev/null
echo "@@SCREEN"
screen -ls 2>/dev/null | grep -E '^\s+[0-9]+\.' | awk '{print $1}'
echo "@@END"
"""


def split_sections(out: str) -> dict:
    sec, cur = {}, None
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            sec[cur] = []
        elif cur:
            sec[cur].append(line)
    return sec


def num(x):
    try:
        return float(x)
    except Exception:
        return None


def parse_status(out: str, m: dict) -> dict:
    s = split_sections(out)
    res = {"ok": True}
    res["hostname"] = (s.get("HOST") or [""])[0]

    up = s.get("UPTIME", [])
    res["uptime_s"] = num(up[0].split()[0]) if up else None
    res["load"] = [num(x) for x in up[1].split()[:3]] if len(up) > 1 else []
    res["ncpu"] = int(up[2]) if len(up) > 2 and up[2].strip().isdigit() else None

    mem = (s.get("MEM") or [""])[0].split()
    res["mem"] = {"total": num(mem[0]), "used": num(mem[1])} if len(mem) >= 2 else None

    disks = []
    for line in s.get("DISK", []):
        p = line.split()
        if len(p) >= 6:
            disks.append({"mount": p[5], "total": num(p[1]), "used": num(p[2])})
    res["disks"] = disks

    gpus, uuid2idx = [], {}
    for line in s.get("GPU", []):
        p = [x.strip() for x in line.split(",")]
        if len(p) < 9:
            continue
        if len(p) > 9:  # tên GPU có dấu phẩy
            p = p[:2] + [",".join(p[2:len(p) - 6])] + p[-6:]
        idx, guid, name, util, mu, mt, temp, pw, pl = p
        uuid2idx[guid] = idx
        gpus.append({
            "index": idx, "name": name, "util": num(util), "mem_used": num(mu),
            "mem_total": num(mt), "temp": num(temp), "power": num(pw), "power_limit": num(pl),
        })
    res["gpus"] = gpus

    gpu_by_pid = {}
    for line in s.get("GPUAPPS", []):
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 3 and p[0].isdigit():
            e = gpu_by_pid.setdefault(int(p[0]), {"mem": 0, "gpus": []})
            e["mem"] += num(p[2]) or 0
            e["gpus"].append(uuid2idx.get(p[1], "?"))

    # toàn bộ process
    allp = {}
    for line in s.get("PS", []):
        p = line.split(None, 7)
        if len(p) < 8 or not p[0].isdigit():
            continue
        allp[int(p[0])] = {
            "pid": int(p[0]), "ppid": int(p[1]), "user": p[2], "etime": num(p[3]),
            "cpu": num(p[4]), "mem": num(p[5]), "rss_mb": round((num(p[6]) or 0) / 1024, 1),
            "cmd": p[7],
        }

    info = {}
    for line in s.get("PROCINFO", []):
        p = line.split("|")
        if len(p) >= 4 and p[0].isdigit():
            info[int(p[0])] = {"cwd": p[1], "stdout": p[2], "stderr": p[3]}

    panes = {}
    for line in s.get("PANES", []):
        p = line.split("|")
        if len(p) >= 2 and p[1].isdigit():
            panes[int(p[1])] = p[0]

    try:
        inc = re.compile(m.get("pattern") or DEFAULT_PATTERN)
    except re.error:
        inc = re.compile(DEFAULT_PATTERN)
    exc_src = m.get("exclude") if m.get("exclude") is not None else DEFAULT_EXCLUDE
    try:
        exc = re.compile(exc_src) if exc_src else None
    except re.error:
        exc = None

    def find_pane(pid):
        seen = 0
        while pid and pid in allp and seen < 64:
            if pid in panes:
                return panes[pid]
            pid = allp[pid]["ppid"]
            seen += 1
        return None

    procs = []
    for pid, p in allp.items():
        cmd = p["cmd"]
        tracked = bool(inc.search(cmd)) and not (exc and exc.search(cmd))
        if not tracked and pid not in gpu_by_pid:
            continue
        if cmd.startswith("bash -s") or "grep -E" in cmd:
            continue
        q = dict(p)
        q.update(info.get(pid, {"cwd": "", "stdout": "", "stderr": ""}))
        q["tmux"] = find_pane(pid)
        g = gpu_by_pid.get(pid)
        q["gpu_mem"] = g["mem"] if g else None
        q["gpu_ids"] = g["gpus"] if g else []
        q["log"] = next(
            (f for f in (q["stdout"], q["stderr"]) if f and f.startswith("/") and not f.startswith("/dev/")),
            None,
        )
        procs.append(q)
    procs.sort(key=lambda x: x["pid"])
    res["procs"] = procs

    tm = []
    for line in s.get("TMUX", []):
        p = line.split("|")
        if len(p) >= 4:
            tm.append({"name": p[0], "windows": p[1], "created": num(p[2]), "attached": p[3] != "0"})
    res["tmux"] = tm
    res["panes"] = [{"target": t, "pid": pid} for pid, t in panes.items()]
    res["screens"] = [x.strip() for x in s.get("SCREEN", []) if x.strip()]

    busy_gpu = any((g["util"] or 0) >= 10 for g in gpus)
    res["state"] = "running" if (procs or busy_gpu) else "idle"
    return res


# ---------------------------------------------------------------- api
app = FastAPI(title="Vast Monitor")


class MachineIn(BaseModel):
    name: str = ""
    host: str
    port: int = 22
    user: str = "root"
    key_path: str = ""
    key_text: str = ""          # dán private key trực tiếp (lưu vào data/keys, chmod 600)
    passphrase: Optional[str] = None
    pattern: str = ""
    exclude: Optional[str] = None
    note: str = ""


class KillIn(BaseModel):
    pid: int
    sig: str = "TERM"


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/defaults")
def defaults():
    return {"pattern": DEFAULT_PATTERN, "exclude": DEFAULT_EXCLUDE}


@app.get("/api/machines")
def list_machines():
    return [public(m) for m in load_machines()]


def _apply(m: dict, body: MachineIn):
    m.update({
        "name": body.name.strip() or f"{body.host}:{body.port}",
        "host": body.host.strip(),
        "port": int(body.port),
        "user": body.user.strip() or "root",
        "key_path": body.key_path.strip(),
        "pattern": body.pattern.strip(),
        "exclude": body.exclude.strip() if body.exclude is not None else m.get("exclude"),
        "note": body.note,
    })
    if body.passphrase is not None:
        m["passphrase"] = body.passphrase
    if body.key_text.strip():
        m["key_file"] = write_pasted_key(m["id"], body.key_text)


@app.post("/api/machines")
def create_machine(body: MachineIn):
    with _store_lock:
        ms = load_machines()
        m = {"id": uuid.uuid4().hex[:10], "created": time.time()}
        _apply(m, body)
        ms.append(m)
        save_machines(ms)
    return public(m)


@app.put("/api/machines/{mid}")
def update_machine(mid: str, body: MachineIn):
    with _store_lock:
        ms = load_machines()
        for m in ms:
            if m["id"] == mid:
                _apply(m, body)
                save_machines(ms)
                pool.drop(mid)
                return public(m)
    raise HTTPException(404, "Không tìm thấy máy")


@app.delete("/api/machines/{mid}")
def delete_machine(mid: str):
    with _store_lock:
        ms = load_machines()
        keep = [m for m in ms if m["id"] != mid]
        for m in ms:
            if m["id"] == mid and m.get("key_file"):
                try:
                    os.remove(m["key_file"])
                except OSError:
                    pass
        save_machines(keep)
    pool.drop(mid)
    return {"ok": True}


@app.get("/api/machines/{mid}/status")
def status(mid: str):
    m = get_machine(mid)
    t0 = time.time()
    pat = m.get("pattern") or DEFAULT_PATTERN
    script = f"PAT={shlex.quote(pat)}\n" + STATUS_SCRIPT
    try:
        out, err, _ = pool.run(m, "bash -s", stdin_data=script, timeout=40)
        res = parse_status(out, m)
    except Exception as e:
        return {"ok": False, "error": friendly_error(e), "fetched_at": time.time()}
    res["fetched_at"] = time.time()
    res["elapsed_ms"] = int((time.time() - t0) * 1000)
    return res


def clean_tty(text: str) -> str:
    """Bỏ mã màu ANSI và giữ phần sau \\r cuối (thanh tiến trình tqdm)."""
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            parts = [x for x in line.split("\r") if x.strip()]
            line = parts[-1] if parts else ""
        lines.append(line)
    return "\n".join(lines)


def remote_path(path: str) -> str:
    path = path.strip()
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


@app.get("/api/machines/{mid}/tail")
def tail(mid: str, path: str, lines: int = 200):
    m = get_machine(mid)
    lines = max(10, min(int(lines), 5000))
    cmd = f"tail -n {lines} -- {remote_path(path)} 2>&1; echo; ls -l --time-style=+%s -- {remote_path(path)} 2>/dev/null"
    try:
        out, _, _ = pool.run(m, cmd, timeout=30)
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}
    body, _, meta = out.rstrip("\n").rpartition("\n")
    info = meta.split()
    mtime = num(info[5]) if len(info) >= 7 else None
    size = num(info[4]) if len(info) >= 7 else None
    if mtime is None:  # không parse được dòng ls -> giữ toàn bộ output
        body = out
    return {"ok": True, "text": clean_tty(body), "mtime": mtime, "size": size}


@app.get("/api/machines/{mid}/tmux")
def tmux_capture(mid: str, target: str, lines: int = 200):
    if not re.fullmatch(r"[\w.\-:@ ]+", target):
        raise HTTPException(400, "target không hợp lệ")
    m = get_machine(mid)
    lines = max(10, min(int(lines), 5000))
    cmd = f"tmux capture-pane -p -J -t {shlex.quote(target)} -S -{lines} 2>&1"
    try:
        out, _, _ = pool.run(m, cmd, timeout=20)
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}
    return {"ok": True, "text": clean_tty(out.rstrip())}


@app.post("/api/machines/{mid}/kill")
def kill(mid: str, body: KillIn):
    if body.sig not in ("TERM", "INT", "KILL"):
        raise HTTPException(400, "signal không hợp lệ")
    m = get_machine(mid)
    try:
        out, err, code = pool.run(m, f"kill -s {body.sig} {int(body.pid)} 2>&1", timeout=15)
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}
    return {"ok": code == 0, "error": (out + err).strip() or None}


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
