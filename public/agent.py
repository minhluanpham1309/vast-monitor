#!/usr/bin/env python3
"""vast-monitor agent — chạy trên máy vast.ai, định kỳ đẩy trạng thái lên dashboard Vercel.

Chỉ dùng thư viện chuẩn của Python 3.7+. Chạy được nhiều lần: chỉ một bản sống nhờ file lock.

Biến môi trường:
  VM_URL        (bắt buộc) URL dashboard, vd https://vast-monitor.vercel.app
  VM_TOKEN      (bắt buộc) AGENT_TOKEN đã đặt trên Vercel
  VM_NAME       tên hiển thị (mặc định: VAST_CONTAINERLABEL hoặc hostname)
  VM_INTERVAL   chu kỳ gửi, giây (mặc định 60)
  VM_PATTERN    regex tiến trình theo dõi (mặc định python|torchrun|accelerate|deepspeed)
  VM_EXCLUDE    regex loại trừ
  VM_RUNS_DIR   thư mục file metrics *.jsonl (mặc định ~/.vast_monitor/runs)
  NTFY_TOPIC    nếu có: gửi thông báo tới https://ntfy.sh/<topic> khi tiến trình / run kết thúc
"""
import fcntl
import glob
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request

VERSION = "1.0"
HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".vast_monitor")
RUNS_DIR = os.path.expanduser(os.environ.get("VM_RUNS_DIR", os.path.join(STATE_DIR, "runs")))
URL = os.environ.get("VM_URL", "").rstrip("/")
TOKEN = os.environ.get("VM_TOKEN", "")
INTERVAL = max(5, int(os.environ.get("VM_INTERVAL", "60") or 60))
PATTERN = os.environ.get("VM_PATTERN") or r"python|torchrun|accelerate|deepspeed"
EXCLUDE = os.environ.get("VM_EXCLUDE") or (
    r"jupyter-lab|jupyter-notebook|jupyter lab|jupyter notebook|tensorboard|supervisord|"
    r"unattended-upgr|vast_agent\.py|agent\.py"
)
NTFY = os.environ.get("NTFY_TOPIC", "").strip()

INSTANCE_ID = os.environ.get("CONTAINER_ID") or re.sub(r"^C\.", "", os.environ.get("VAST_CONTAINERLABEL", "")) or None
MACHINE_ID = ("vast-" + INSTANCE_ID) if INSTANCE_ID else socket.gethostname()
NAME = os.environ.get("VM_NAME") or os.environ.get("VAST_CONTAINERLABEL") or socket.gethostname()

STATUS_SCRIPT = r"""
export LC_ALL=C
echo "@@UPTIME"; cat /proc/uptime; cat /proc/loadavg; nproc 2>/dev/null
echo "@@MEM"; free -m 2>/dev/null | awk '/^Mem:/{print $2" "$3}'
echo "@@DISK"; df -P -m / /workspace 2>/dev/null | tail -n +2 | sort -u
echo "@@GPU"
nvidia-smi --query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit --format=csv,noheader,nounits 2>/dev/null
echo "@@GPUAPPS"
nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader,nounits 2>/dev/null
echo "@@PS"
ps -eo pid=,ppid=,user=,etimes=,pcpu=,pmem=,rss=,args= 2>/dev/null
echo "@@TMUX"
tmux ls -F '#{session_name}|#{session_windows}|#{session_created}|#{session_attached}' 2>/dev/null
echo "@@PANES"
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}|#{pane_pid}|#{pane_current_command}' 2>/dev/null
echo "@@END"
"""


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def num(x):
    try:
        return float(x)
    except Exception:
        return None


def sh(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return r.stdout
    except Exception as e:
        return "error: %s" % e


def clean_tty(text):
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    out = []
    for line in text.split("\n"):
        if "\r" in line:
            parts = [x for x in line.split("\r") if x.strip()]
            line = parts[-1] if parts else ""
        out.append(line)
    return "\n".join(out)


def tail_file(path, lines):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block, data = 65536, b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
                if len(data) > 8_000_000:
                    break
        text = data.decode("utf-8", "replace")
        text = clean_tty(text)
        return "\n".join(text.split("\n")[-lines:])
    except Exception as e:
        return "Không đọc được %s: %s" % (path, e)


def tmux_capture(target, lines):
    return clean_tty(sh(["tmux", "capture-pane", "-p", "-J", "-t", target, "-S", "-%d" % lines]).rstrip())


def readlink(p):
    try:
        return os.readlink(p)
    except Exception:
        return ""


# ------------------------------------------------------------------ collect
def collect():
    out = sh(["bash", "-c", STATUS_SCRIPT], timeout=30)
    sec, cur = {}, None
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            sec[cur] = []
        elif cur:
            sec[cur].append(line)

    st = {}
    up = sec.get("UPTIME", [])
    st["uptime_s"] = num(up[0].split()[0]) if up else None
    st["load"] = [num(x) for x in up[1].split()[:3]] if len(up) > 1 else []
    st["ncpu"] = int(up[2]) if len(up) > 2 and up[2].strip().isdigit() else None
    mem = (sec.get("MEM") or [""])[0].split()
    st["mem"] = {"total": num(mem[0]), "used": num(mem[1])} if len(mem) >= 2 else None
    st["disks"] = []
    for line in sec.get("DISK", []):
        p = line.split()
        if len(p) >= 6:
            st["disks"].append({"mount": p[5], "total": num(p[1]), "used": num(p[2])})

    gpus, uuid2idx = [], {}
    for line in sec.get("GPU", []):
        p = [x.strip() for x in line.split(",")]
        if len(p) < 9:
            continue
        if len(p) > 9:
            p = p[:2] + [",".join(p[2:len(p) - 6])] + p[-6:]
        idx, guid, name, util, mu, mt, temp, pw, pl = p
        uuid2idx[guid] = idx
        gpus.append({"index": idx, "name": name, "util": num(util), "mem_used": num(mu), "mem_total": num(mt),
                     "temp": num(temp), "power": num(pw), "power_limit": num(pl)})
    st["gpus"] = gpus

    gpu_by_pid = {}
    for line in sec.get("GPUAPPS", []):
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 3 and p[0].isdigit():
            e = gpu_by_pid.setdefault(int(p[0]), {"mem": 0, "gpus": []})
            e["mem"] += num(p[2]) or 0
            e["gpus"].append(uuid2idx.get(p[1], "?"))

    allp = {}
    for line in sec.get("PS", []):
        p = line.split(None, 7)
        if len(p) < 8 or not p[0].isdigit():
            continue
        allp[int(p[0])] = {"pid": int(p[0]), "ppid": int(p[1]), "user": p[2], "etime": num(p[3]),
                           "cpu": num(p[4]), "mem": num(p[5]), "rss_mb": round((num(p[6]) or 0) / 1024, 1),
                           "cmd": p[7][:600]}
    panes = {}
    for line in sec.get("PANES", []):
        p = line.split("|")
        if len(p) >= 2 and p[1].isdigit():
            panes[int(p[1])] = p[0]

    try:
        inc = re.compile(PATTERN)
    except re.error:
        inc = re.compile(r"python")
    try:
        exc = re.compile(EXCLUDE) if EXCLUDE else None
    except re.error:
        exc = None
    me = os.getpid()

    def find_pane(pid):
        n = 0
        while pid in allp and n < 64:
            if pid in panes:
                return panes[pid]
            pid = allp[pid]["ppid"]
            n += 1
        return None

    procs = []
    for pid, p in allp.items():
        if pid == me:
            continue
        cmd = p["cmd"]
        tracked = bool(inc.search(cmd)) and not (exc and exc.search(cmd))
        if not tracked and pid not in gpu_by_pid:
            continue
        if (cmd.startswith("bash -c") and "@@UPTIME" in cmd) or cmd.endswith("<defunct>"):
            continue
        q = dict(p)
        q["cwd"] = readlink("/proc/%d/cwd" % pid)
        q["stdout"] = readlink("/proc/%d/fd/1" % pid)
        q["stderr"] = readlink("/proc/%d/fd/2" % pid)
        q["tmux"] = find_pane(pid)
        g = gpu_by_pid.get(pid)
        q["gpu_mem"] = g["mem"] if g else None
        q["gpu_ids"] = g["gpus"] if g else []
        q["log"] = next((f for f in (q["stdout"], q["stderr"])
                         if f and f.startswith("/") and not f.startswith("/dev/") and "(deleted)" not in f), None)
        procs.append(q)
    procs.sort(key=lambda x: x["pid"])
    st["procs"] = procs

    st["tmux"] = []
    for line in sec.get("TMUX", []):
        p = line.split("|")
        if len(p) >= 4:
            st["tmux"].append({"name": p[0], "windows": p[1], "created": num(p[2]), "attached": p[3] != "0"})

    # tự đính kèm đuôi log của các tiến trình gốc (tối đa 4 nguồn)
    pids = {p["pid"] for p in procs}
    roots = [p for p in procs if p["ppid"] not in pids]
    logs, seen = [], set()
    for p in roots:
        if p["log"] and p["log"] not in seen:
            seen.add(p["log"])
            logs.append({"kind": "file", "target": p["log"], "pid": p["pid"], "text": tail_file(p["log"], 40),
                         "mtime": os.path.getmtime(p["log"]) if os.path.exists(p["log"]) else None})
        elif p["tmux"] and p["tmux"] not in seen:
            seen.add(p["tmux"])
            logs.append({"kind": "tmux", "target": p["tmux"], "pid": p["pid"], "text": tmux_capture(p["tmux"], 40)})
        if len(logs) >= 4:
            break
    st["logs"] = logs
    st["runs"] = collect_runs()
    busy = any((g["util"] or 0) >= 10 for g in gpus)
    st["state"] = "running" if (roots or busy) else "idle"
    return st


def downsample(xs, n=200):
    if len(xs) <= n:
        return xs
    step = len(xs) / float(n)
    return [xs[int(i * step)] for i in range(n - 1)] + [xs[-1]]


def collect_runs():
    """Đọc file ~/.vast_monitor/runs/<run>.jsonl do vm_log() trong code thực nghiệm ghi ra."""
    runs = []
    files = sorted(glob.glob(os.path.join(RUNS_DIR, "*.jsonl")), key=os.path.getmtime, reverse=True)[:12]
    for f in files:
        recs = []
        for line in tail_file(f, 5000).splitlines():
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
        if not recs:
            continue
        meta = next((r for r in recs if r.get("_type") == "start"), {})
        end = next((r for r in reversed(recs) if r.get("_type") == "end"), None)
        points = [r for r in recs if not r.get("_type")]
        keys = []
        for r in points:
            for k, v in r.items():
                if k not in keys and not k.startswith("_") and isinstance(v, (int, float)) and not isinstance(v, bool):
                    keys.append(k)
        series = {}
        ds = downsample(points)
        for k in keys[:16]:
            series[k] = [[r.get("_t"), r.get(k)] for r in ds if isinstance(r.get(k), (int, float))]
        runs.append({
            "name": os.path.basename(f)[:-6],
            "config": meta.get("config"),
            "started": meta.get("_t"),
            "status": (end or {}).get("status", "running"),
            "ended": (end or {}).get("_t"),
            "error": (end or {}).get("error"),
            "last": points[-1] if points else None,
            "n": len(points),
            "updated": os.path.getmtime(f),
            "series": series,
        })
    return runs


# ------------------------------------------------------------------ events
class Tracker:
    def __init__(self):
        self.prev = None
        self.prev_runs = {}

    def diff(self, st):
        ev = []
        procs = st["procs"]
        pids = {p["pid"] for p in procs}
        roots = {p["pid"]: p for p in procs if p["ppid"] not in pids}
        if self.prev is not None:
            for pid, p in self.prev.items():
                if pid not in roots and pid not in pids:
                    ev.append({"type": "proc_end", "pid": pid, "cmd": p["cmd"][:300], "etime": p["etime"],
                               "at": time.time()})
        self.prev = roots
        for r in st["runs"]:
            old = self.prev_runs.get(r["name"])
            if old is not None and old == "running" and r["status"] != "running":
                ev.append({"type": "run_end", "run": r["name"], "status": r["status"], "error": r.get("error"),
                           "last": r.get("last"), "at": time.time()})
            self.prev_runs[r["name"]] = r["status"]
        return ev


def notify(ev):
    if not NTFY:
        return
    if ev["type"] == "proc_end":
        title = "%s: tiến trình %s kết thúc" % (NAME, ev["pid"])
        body = ev["cmd"]
    else:
        title = "%s: run %s %s" % (NAME, ev["run"], ev["status"])
        last = {k: v for k, v in (ev.get("last") or {}).items() if not k.startswith("_")}
        body = (ev.get("error") or "") + ("\n" if ev.get("error") else "") + json.dumps(last, ensure_ascii=False)[:500]
    try:
        ok = ev.get("status") in (None, "done")
        msg = json.dumps({"topic": NTFY, "title": title, "message": body or "-",
                          "tags": ["white_check_mark" if ok else "x"]}, ensure_ascii=False)
        req = urllib.request.Request("https://ntfy.sh/", data=msg.encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log("ntfy error:", e)


# ------------------------------------------------------------------ commands
def run_command(c):
    if c.get("type") == "kill":
        sig = {"TERM": signal.SIGTERM, "INT": signal.SIGINT, "KILL": signal.SIGKILL}.get(c.get("sig"), signal.SIGTERM)
        try:
            os.kill(int(c["pid"]), sig)
            return {"cid": c.get("cid"), "ok": True, "msg": "Đã gửi %s tới %s" % (c.get("sig"), c["pid"]), "at": time.time()}
        except Exception as e:
            return {"cid": c.get("cid"), "ok": False, "msg": str(e), "at": time.time()}
    return {"cid": c.get("cid"), "ok": False, "msg": "lệnh không hỗ trợ", "at": time.time()}


def do_watch(w):
    lines = int(w.get("lines") or 200)
    if w.get("kind") == "tmux":
        text = tmux_capture(w["target"], lines)
        return {"kind": "tmux", "target": w["target"], "text": text, "at": time.time()}
    path = os.path.expanduser(w["target"])
    mtime = os.path.getmtime(path) if os.path.exists(path) else None
    size = os.path.getsize(path) if os.path.exists(path) else None
    return {"kind": "file", "target": w["target"], "text": tail_file(path, lines), "mtime": mtime, "size": size,
            "at": time.time()}


def post(payload):
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(URL + "/api/push", data=data, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + TOKEN,
        "User-Agent": "vast-monitor-agent/" + VERSION})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    if not URL or not TOKEN:
        log("Thiếu VM_URL hoặc VM_TOKEN — agent thoát.")
        return 1
    os.makedirs(RUNS_DIR, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, "agent.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Agent đã chạy rồi — thoát.")
        return 0
    lock.write(str(os.getpid()))
    lock.flush()
    log("vast-monitor agent %s: id=%s name=%s url=%s interval=%ss" % (VERSION, MACHINE_ID, NAME, URL, INTERVAL))

    tracker = Tracker()
    pending_events, pending_results = [], []
    watch, backoff = None, INTERVAL
    while True:
        t0 = time.time()
        try:
            st = collect()
            new_ev = tracker.diff(st)
            for e in new_ev:
                notify(e)
            pending_events.extend(new_ev)
            st.update({"id": MACHINE_ID, "instance_id": INSTANCE_ID, "name": NAME, "agent": VERSION,
                       "hostname": socket.gethostname(), "interval": INTERVAL,
                       "gpu_count": os.environ.get("GPU_COUNT"), "public_ip": os.environ.get("PUBLIC_IPADDR"),
                       "events": pending_events[-50:], "cmd_results": pending_results[-20:]})
            if watch:
                st["watch_result"] = do_watch(watch)
            resp = post(st)
            pending_events, pending_results = [], []
            for c in resp.get("commands") or []:
                pending_results.append(run_command(c))
            watch = resp.get("watch")
            backoff = INTERVAL
        except Exception as e:
            log("push lỗi:", e)
            backoff = min(backoff * 2, 300)
        wait = 5 if (watch or pending_results) else backoff
        time.sleep(max(1, wait - (time.time() - t0)))


if __name__ == "__main__":
    sys.exit(main())
