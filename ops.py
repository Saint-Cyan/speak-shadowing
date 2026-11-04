"""运维监控：服务器规格与资源占用、进程是否重启、导入任务、请求统计、事件日志。

接口：
  GET /ops                 监控页面（static/ops.html）
  GET /api/ops/status      当前快照
  GET /api/ops/history     最近一小时的资源采样（每 5 秒一次）

采样和事件会追加写到 OPS_DIR 下的文件里：进程重启后，还能看到上一个进程最后的状态和事件，
用来判断是不是内存超限被杀（OOM）、被平台重启，还是请求被分到了另一个实例。
不记录任何用户身份或内容（视频标题、令牌、用户 ID 都不记录）。
"""

import asyncio
import json
import os
import platform
import resource
import shutil
import socket
import sys
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, Request

router = APIRouter()

OPS_DIR = Path(os.environ.get("OPS_DIR", "/tmp/speak-ops"))
SAMPLE_EVERY = 5
KEEP_SAMPLES = 720            # 内存里保留 1 小时
KEEP_EVENTS = 300
FILE_MAX_LINES = 3000

STARTED = time.time()
INSTANCE = uuid.uuid4().hex[:8]   # 每个进程一个随机编号，换进程就会变
PID = os.getpid()
HOST = socket.gethostname()

_samples: deque = deque(maxlen=KEEP_SAMPLES)
_events: deque = deque(maxlen=KEEP_EVENTS)
_req = defaultdict(lambda: {"n": 0, "err4": 0, "err5": 0, "ms": 0.0})
_errors: deque = deque(maxlen=50)
_prev = {"last_sample": None, "last_events": [], "found": False}
_cpu_last = None
_peak = {"rss": 0, "cg": 0}


# ---------------- 读取系统信息（读不到就返回 None） ----------------
def _read(path: str):
    try:
        return Path(path).read_text().strip()
    except Exception:  # noqa: BLE001
        return None


def _int(v):
    try:
        return int(v)
    except Exception:  # noqa: BLE001
        return None


def _kv(path: str) -> dict:
    out = {}
    txt = _read(path) or ""
    for line in txt.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def cgroup_memory() -> dict:
    """容器的内存上限与用量（cgroup v2 优先，其次 v1）。"""
    v2 = _read("/sys/fs/cgroup/memory.max")
    if v2 is not None:
        ev = _kv("/sys/fs/cgroup/memory.events")
        return {"version": 2, "limit": None if v2 == "max" else _int(v2),
                "current": _int(_read("/sys/fs/cgroup/memory.current")),
                "peak": _int(_read("/sys/fs/cgroup/memory.peak")),
                "oom": _int(ev.get("oom")), "oom_kill": _int(ev.get("oom_kill"))}
    lim = _int(_read("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
    if lim is not None:
        oc = _kv("/sys/fs/cgroup/memory/memory.oom_control")
        return {"version": 1, "limit": None if lim > (1 << 60) else lim,
                "current": _int(_read("/sys/fs/cgroup/memory/memory.usage_in_bytes")),
                "peak": _int(_read("/sys/fs/cgroup/memory/memory.max_usage_in_bytes")),
                "oom": None, "oom_kill": _int(oc.get("oom_kill"))}
    return {"version": None, "limit": None, "current": None, "peak": None, "oom": None, "oom_kill": None}


def cgroup_cpu() -> dict:
    v2 = _read("/sys/fs/cgroup/cpu.max")
    if v2:
        q, p = (v2.split() + ["100000"])[:2]
        return {"quota_cores": None if q == "max" else round(int(q) / int(p), 2)}
    q = _int(_read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
    p = _int(_read("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
    return {"quota_cores": round(q / p, 2) if q and p and q > 0 else None}


def proc_memory() -> dict:
    st = _kv("/proc/self/status")
    rss = _int(st.get("VmRSS"))
    hwm = _int(st.get("VmHWM"))
    if rss is None:  # macOS 本地调试：只有峰值
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        hwm = ru if sys.platform == "darwin" else ru * 1024
        return {"rss": None, "peak": hwm}
    return {"rss": rss * 1024, "peak": hwm * 1024 if hwm else None}


def children_memory() -> dict:
    """本进程的子进程（ffmpeg）占用的内存。"""
    total, n = 0, 0
    try:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            st = _kv(f"/proc/{d}/status")
            if st.get("PPid") == str(PID):
                n += 1
                total += (_int(st.get("VmRSS")) or 0) * 1024
    except Exception:  # noqa: BLE001
        return {"count": None, "rss": None}
    return {"count": n, "rss": total}


def system_memory() -> dict:
    mi = _kv("/proc/meminfo")
    t, a = _int(mi.get("MemTotal")), _int(mi.get("MemAvailable"))
    return {"total": t * 1024 if t else None, "available": a * 1024 if a else None}


def disk(path: Path) -> dict:
    try:
        path.mkdir(parents=True, exist_ok=True)
        u = shutil.disk_usage(path)
        return {"path": str(path), "total": u.total, "free": u.free}
    except Exception:  # noqa: BLE001
        return {"path": str(path), "total": None, "free": None}


def dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except Exception:  # noqa: BLE001
        pass
    return total


def cpu_percent() -> float:
    """本进程（含已结束的子进程）自上次采样以来的 CPU 占用，100% = 占满 1 核。"""
    global _cpu_last
    s = resource.getrusage(resource.RUSAGE_SELF)
    c = resource.getrusage(resource.RUSAGE_CHILDREN)
    used = s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime
    now = time.time()
    pct = None
    if _cpu_last:
        dt = now - _cpu_last[0]
        if dt > 0:
            pct = round((used - _cpu_last[1]) / dt * 100, 1)
    _cpu_last = (now, used)
    return pct


def boot_time():
    for line in (_read("/proc/stat") or "").splitlines():
        if line.startswith("btime "):
            return _int(line.split()[1])
    return None


# ---------------- 事件、请求、持久化 ----------------
def _append(name: str, obj: dict):
    try:
        OPS_DIR.mkdir(parents=True, exist_ok=True)
        p = OPS_DIR / name
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        if p.stat().st_size > FILE_MAX_LINES * 400:  # 粗略控制大小：保留后一半
            lines = p.read_text(encoding="utf-8").splitlines()[-FILE_MAX_LINES // 2:]
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def event(kind: str, msg: str, level: str = "info", **extra):
    e = {"t": round(time.time(), 1), "inst": INSTANCE, "pid": PID, "kind": kind, "level": level, "msg": str(msg)[:500]}
    e.update({k: v for k, v in extra.items() if v is not None})
    _events.append(e)
    _append("events.jsonl", e)
    print(f"[ops] {level} {kind}: {msg}", flush=True)


def record_request(path: str, status: int, ms: float):
    key = path
    for prefix in ("/api/media/jobs/", "/static/"):
        if path.startswith(prefix):
            key = prefix + "*"
    r = _req[key]
    r["n"] += 1
    r["ms"] += ms
    if 400 <= status < 500:
        r["err4"] += 1
    if status >= 500:
        r["err5"] += 1
    if status >= 500 or (status == 404 and key.startswith("/api/")):
        _errors.append({"t": round(time.time(), 1), "path": key, "status": status})


def _load_previous():
    """读取上一个进程留下的最后一次采样和最后一批事件。"""
    try:
        lines = (OPS_DIR / "samples.jsonl").read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            s = json.loads(line)
            if s.get("inst") != INSTANCE:
                _prev.update(last_sample=s, found=True)
                break
    except Exception:  # noqa: BLE001
        pass
    try:
        lines = (OPS_DIR / "events.jsonl").read_text(encoding="utf-8").splitlines()
        evs = [json.loads(x) for x in lines[-200:]]
        _prev["last_events"] = [e for e in evs if e.get("inst") != INSTANCE][-50:]
        if _prev["last_events"]:
            _prev["found"] = True
    except Exception:  # noqa: BLE001
        pass


def _tts_stats():
    try:
        import tts
        st = dict(tts.STATS)
        st["cache_dir"] = str(tts.CACHE_DIR)
        st["edge"] = dict(tts.EDGE)
        st["default_engine"] = tts.DEFAULT_ENGINE
        st["cache_files"] = len(list(tts.CACHE_DIR.glob("*.json"))) if tts.CACHE_DIR.exists() else 0
        return st
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def tencent_ok():
    try:
        import tencent
        return tencent.enabled()
    except Exception:  # noqa: BLE001
        return False


def _jobs_summary():
    try:
        import media
        jobs = list(media._jobs.values())
        now = time.time()
        active = [j for j in jobs if j["state"] in ("downloading", "processing")]
        return {
            "total": len(jobs), "active": len(active),
            "by_state": {s: sum(1 for j in jobs if j["state"] == s) for s in ("downloading", "processing", "ready", "error")},
            "items": [{"id": j["id"][:6], "source": j.get("source"), "state": j["state"], "message": j.get("message") or "",
                       "age_s": round(now - j["created"]), "chunks": len(j.get("chunks") or []),
                       "duration_ms": j.get("duration_ms"), "engine": j.get("engine")} for j in sorted(jobs, key=lambda x: -x["created"])[:20]],
            "work_dir": str(media.WORK), "work_bytes": dir_size(media.WORK),
            "auth": {k: media._auth_diag.get(k) for k in ("mode", "last_error", "last_error_at", "last_ok_at")},
            "token_shapes": list(media._token_seen),
            "tencent": tencent_ok(),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _sample() -> dict:
    cg = cgroup_memory()
    pm = proc_memory()
    ch = children_memory()
    sm = system_memory()
    try:
        load1 = round(os.getloadavg()[0], 2)
    except OSError:
        load1 = None
    try:
        import media
        active = sum(1 for j in media._jobs.values() if j["state"] in ("downloading", "processing"))
    except Exception:  # noqa: BLE001
        active = None
    s = {"t": round(time.time(), 1), "inst": INSTANCE, "pid": PID, "rss": pm["rss"], "child_rss": ch["rss"],
         "cg_used": cg["current"], "cg_limit": cg["limit"], "oom_kill": cg["oom_kill"],
         "sys_avail": sm["available"], "cpu": cpu_percent(), "load1": load1, "jobs": active}
    if pm["rss"]:
        _peak["rss"] = max(_peak["rss"], pm["rss"] + (ch["rss"] or 0))
    if cg["current"]:
        _peak["cg"] = max(_peak["cg"], cg["current"])
    return s


async def _sampler():
    while True:
        try:
            s = _sample()
            _samples.append(s)
            _append("samples.jsonl", s)
            lim, used = s.get("cg_limit"), s.get("cg_used")
            if lim and used and used > lim * 0.9:
                event("memory_high", f"容器内存已用 {used >> 20}MB / 上限 {lim >> 20}MB", "warn")
        except Exception as e:  # noqa: BLE001
            print(f"[ops] sample failed: {e}", flush=True)
        await asyncio.sleep(SAMPLE_EVERY)


_task = None


def start():
    """在应用启动时调用：读取上一个进程的记录、写启动事件、开始定时采样。"""
    global _task
    _load_previous()
    cg = cgroup_memory()
    prev = _prev["last_sample"]
    note = ""
    if prev:
        gap = round(time.time() - prev["t"])
        note = f"；上一个进程（{prev.get('inst')}）最后一次采样在 {gap} 秒前"
        if prev.get("cg_limit") and prev.get("cg_used"):
            note += f"，当时容器内存 {prev['cg_used'] >> 20}MB / {prev['cg_limit'] >> 20}MB"
    event("startup", f"进程启动 pid={PID} host={HOST}{note}", oom_kill=cg["oom_kill"])
    cpu_percent()
    _task = asyncio.get_event_loop().create_task(_sampler())


# ---------------- 接口 ----------------
@router.get("/api/ops/status")
async def status(request: Request):
    cg = cgroup_memory()
    now = time.time()
    try:
        import media
        ff = media.ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        ff = f"不可用：{type(e).__name__}"
    bt = boot_time()
    return {
        "now": round(now, 1),
        "instance": {"id": INSTANCE, "pid": PID, "host": HOST, "started": round(STARTED, 1),
                     "uptime_s": round(now - STARTED), "boot_time": bt,
                     "python": platform.python_version(), "platform": platform.platform(terse=True)},
        "cpu": {"count": os.cpu_count(), **cgroup_cpu(),
                "load": [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
                "process_pct": _samples[-1]["cpu"] if _samples else None},
        "memory": {"cgroup": cg, "process": proc_memory(), "children": children_memory(), "system": system_memory(),
                   "peak_since_start": {"process_and_children": _peak["rss"] or None, "cgroup": _peak["cg"] or None}},
        "disk": {"tmp": disk(Path("/tmp")), "ops": disk(OPS_DIR)},
        "ffmpeg": ff,
        "jobs": _jobs_summary(),
        "tts": _tts_stats(),
        "requests": {k: {"n": v["n"], "err4": v["err4"], "err5": v["err5"], "avg_ms": round(v["ms"] / v["n"]) if v["n"] else 0}
                     for k, v in sorted(_req.items(), key=lambda kv: -kv[1]["n"])},
        "recent_errors": list(_errors)[-20:],
        "events": list(_events)[-80:],
        "previous": _prev if _prev["found"] else None,
    }


@router.get("/api/ops/history")
async def history():
    return {"every_s": SAMPLE_EVERY, "samples": list(_samples)}
