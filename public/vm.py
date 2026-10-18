"""vm.py — helper nhúng vào code thực nghiệm để đẩy tiến trình + metrics lên Vast Monitor.

Dùng:
    from vm import vm_start, vm_log, vm_finish
    vm_start("phobert_lr2e-5", config=vars(args))   # bật agent (nếu đang ở trên vast) + ghi run
    for epoch in ...:
        vm_log("phobert_lr2e-5", epoch=epoch, loss=loss, val_f1=f1)
    vm_finish("phobert_lr2e-5", status="done")

Không có VM_URL (chạy local) thì mọi hàm là no-op, không ảnh hưởng thực nghiệm.
Chỉ dùng thư viện chuẩn, mọi lỗi đều được nuốt để không làm hỏng run.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

def _load_container_env():
    """Trên vast.ai, biến môi trường của tài khoản (VM_URL, VM_TOKEN...) nằm ở tiến trình gốc của container
    nhưng thường KHÔNG có trong phiên SSH/tmux. Đọc bổ sung từ /proc/1/environ nếu thiếu."""
    try:
        with open("/proc/1/environ", "rb") as f:
            for item in f.read().split(b"\0"):
                if b"=" not in item:
                    continue
                k, v = item.decode("utf-8", "replace").split("=", 1)
                if (k.startswith(("VM_", "NTFY_", "VAST_")) or k in ("CONTAINER_ID", "GPU_COUNT", "PUBLIC_IPADDR")) and not os.environ.get(k):
                    os.environ[k] = v
    except Exception:
        pass


_load_container_env()


_DIR = os.path.join(os.path.expanduser("~"), ".vast_monitor")
_RUNS = os.path.expanduser(os.environ.get("VM_RUNS_DIR", os.path.join(_DIR, "runs")))


def _enabled():
    return bool(os.environ.get("VM_URL") and os.environ.get("VM_TOKEN"))


def _write(run, rec):
    if not _enabled():
        return
    try:
        os.makedirs(_RUNS, exist_ok=True)
        rec["_t"] = time.time()
        safe = "".join(c if c.isalnum() or c in "-_.=" else "_" for c in run)[:120]
        with open(os.path.join(_RUNS, safe + ".jsonl"), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print("[vast-monitor] log error:", e, file=sys.stderr)


def vm_start(run=None, config=None):
    """Tải và chạy agent nền (chỉ một bản), rồi đánh dấu bắt đầu run."""
    if not _enabled():
        if os.environ.get("CONTAINER_ID") or os.environ.get("VAST_CONTAINERLABEL"):
            print("[vast-monitor] Đang chạy trên vast.ai nhưng không thấy VM_URL/VM_TOKEN — dashboard sẽ KHÔNG nhận dữ liệu. "
                  "Kiểm tra Environment Variables trong Account Settings của vast.ai.", file=sys.stderr)
        return
    try:
        os.makedirs(_DIR, exist_ok=True)
        path = os.path.join(_DIR, "vast_agent.py")
        url = os.environ["VM_URL"].rstrip("/") + "/agent.py"
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as e:
            if not os.path.exists(path):
                raise
            print("[vast-monitor] dùng agent đã tải trước:", e, file=sys.stderr)
        subprocess.Popen([sys.executable, path], stdout=open(os.path.join(_DIR, "agent.log"), "a"),
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        print("[vast-monitor] không bật được agent:", e, file=sys.stderr)
    if run:
        _write(run, {"_type": "start", "config": config, "pid": os.getpid(), "argv": sys.argv})


def vm_log(run, **metrics):
    """Ghi một điểm metrics (số) — vd vm_log(run, epoch=3, loss=0.21, val_f1=0.81)."""
    _write(run, dict(metrics))


def vm_finish(run, status="done", error=None):
    """status: "done" | "failed" | "stopped". Gọi trong finally để dashboard biết run kết thúc."""
    _write(run, {"_type": "end", "status": status, "error": (str(error)[:1000] if error else None)})
