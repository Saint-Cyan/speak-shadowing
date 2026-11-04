"""视频 / 音频导入：解析 B 站链接、下载音频或接收本地文件，用 ffmpeg 转成单声道并按静音切成 ≤55 秒的小段。

切段的原因：云服务里能听音频的模型（Gemini）单次只能处理约 60 秒。转写与整理在浏览器里调用模型完成，
这里只负责准备音频。所有接口都要求带登录令牌，后端会向云服务核实身份，避免被陌生人当下载器用。

接口：
  POST /api/media/bili/info        {"url": "..."}  → 视频标题与分 P 列表
  POST /api/media/jobs             {"bvid","cid","title","start_s","end_s"} → 新建 B 站导入任务
  POST /api/media/upload           multipart file（≤300MB）→ 新建本地文件导入任务
  GET  /api/media/jobs/{id}        任务状态与分段列表
  GET  /api/media/jobs/{id}/chunks/{idx}   某一段的 mp3
  GET  /api/media/diag             排障信息（登录核实方式、最近失败原因、ffmpeg 是否可用）
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import CONFIG
from ops import event

router = APIRouter(prefix="/api/media")

CLOUD_ENDPOINT = CONFIG["cloud_endpoint"]
PUBLISHABLE_KEY = CONFIG["cloud_publishable_key"]  # 公开配置，不是密钥
WORK = Path(os.environ.get("MEDIA_DIR", "/tmp/speak-media"))
MAX_SECONDS = 2 * 3600        # 单次导入上限 2 小时
MAX_UPLOAD = 300 * 1024 * 1024
CHUNK_MAX = 55.0              # 每段最长秒数（模型约 60 秒上限，留余量）
CHUNK_MIN = 30.0              # 在 30–55 秒之间找停顿切开
JOB_TTL = 12 * 3600
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

_jobs: dict = {}
IP_RATE_LIMIT = 60            # 导入接口每个 IP 每分钟最多请求数（含进度轮询）
MAX_ACTIVE_JOBS = 6           # 全站同时准备音频的任务上限
_ip_hits: "defaultdict[str, deque]" = defaultdict(deque)
_auth_diag = {"mode": "unknown", "last_error": None, "last_error_at": None, "last_ok_at": None}
try:
    import certifi  # 服务器可能缺根证书：用 certifi 自带的一份
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL = ssl.create_default_context()
_tokens: dict = {}            # 令牌哈希 → (用户 id, 过期时间)
_bili_cookies: dict = {}


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg  # 托管环境没有系统 ffmpeg 时，用 pip 包自带的静态版本
    return imageio_ffmpeg.get_ffmpeg_exe()


# ---------------- 登录校验 ----------------
async def require_user(request: Request) -> str:
    """方案 A：向云服务核实登录（自带根证书、沿用服务器代理）。
    核实请求因网络 / 证书失败时，退回方案 C：不再强制核实，靠网关的分享码 + 每 IP 限流 + 并发上限兜底，
    并记录失败原因（GET /api/media/diag 可查看）。云服务明确回复登录无效时仍然拒绝。"""
    if os.environ.get("SPEAK_DEV_NOAUTH") == "1":  # 仅本地调试用，线上不设置
        return "dev"
    ip = _client_ip(request)
    if not _ip_rate_ok(ip):
        raise HTTPException(429, "请求太频繁，请稍后再试")
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "请先登录")
    token = auth[7:].strip()
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _tokens.get(key)
    if hit and hit[1] > time.time():
        return hit[0]
    status, data, err = None, None, None
    try:
        conn = aiohttp.TCPConnector(ssl=_SSL)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), connector=conn, trust_env=True) as s:
            async with s.get(f"{CLOUD_ENDPOINT}/.cloud/auth/v1/user/me", headers={
                "Authorization": f"Bearer {token}", "x-wb-webapp-access-key": PUBLISHABLE_KEY, "Origin": CLOUD_ENDPOINT,
            }) as r:
                status = r.status
                if status == 200:
                    data = await r.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    if status in (401, 403):
        raise HTTPException(401, "登录已失效，请重新登录")
    if status == 200 and isinstance(data, dict):
        uid = str(data.get("sub") or data.get("uid") or data.get("id") or (data.get("user") or {}).get("id") or "")
        if uid:
            if _auth_diag["mode"] != "verified":
                event("auth_verified", "登录核实成功（严格模式）")
            _auth_diag.update(mode="verified", last_ok_at=time.time())
            _remember(key, uid, 300)
            return uid
        err = "登录查询返回里没有用户 ID"
    elif err is None:
        err = f"登录查询返回 HTTP {status}"
    # 方案 C：核实不了，按令牌里声明的用户 ID（未验证，仅用来区分各人的导入任务）放行
    if _auth_diag["mode"] != "fallback":
        event("auth_fallback", f"登录核实失败，退回分享码模式：{err[:300]}", "warn")
    _auth_diag.update(mode="fallback", last_error=err[:500], last_error_at=time.time())
    print(f"[media] login check failed, fallback to share-code mode: {err}", flush=True)
    uid = _jwt_sub(token) or f"ip:{ip}"
    _remember(key, uid, 60)
    return uid


def _remember(key: str, uid: str, ttl: int):
    _tokens[key] = (uid, time.time() + ttl)
    if len(_tokens) > 2000:
        _tokens.clear()


def _jwt_sub(token: str) -> str:
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return str(payload.get("sub") or payload.get("uid") or "")[:100]
    except Exception:  # noqa: BLE001
        return ""


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _ip_rate_ok(ip: str) -> bool:
    now = time.time()
    q = _ip_hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= IP_RATE_LIMIT:
        return False
    q.append(now)
    return True


@router.get("/diag")
async def diag():
    """排障用：登录核实走的是哪种方式、最近一次失败原因、ffmpeg 是否可用。不含任何令牌或用户信息。"""
    try:
        ff = ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        ff = f"不可用：{type(e).__name__}"
    active = sum(1 for j in _jobs.values() if j["state"] in ("downloading", "processing"))
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else None  # noqa: E731
    return {"auth_mode": _auth_diag["mode"], "last_error": _auth_diag["last_error"],
            "last_error_at": fmt(_auth_diag["last_error_at"]), "last_ok_at": fmt(_auth_diag["last_ok_at"]),
            "ffmpeg": ff, "active_jobs": active}


# ---------------- B 站 ----------------
async def _bili_session() -> aiohttp.ClientSession:
    s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers={"User-Agent": UA})
    if not _bili_cookies:
        try:
            async with s.get("https://www.bilibili.com/") as r:
                for k, v in r.cookies.items():
                    _bili_cookies[k] = v.value
        except Exception:  # noqa: BLE001
            pass
    s.cookie_jar.update_cookies(_bili_cookies)
    return s


async def _bili_json(s: aiohttp.ClientSession, url: str, referer: str = "https://www.bilibili.com/") -> dict:
    async with s.get(url, headers={"Referer": referer}) as r:
        text = await r.text()
    try:
        d = json.loads(text, strict=False)
    except ValueError:
        raise HTTPException(502, "B 站返回了无法解析的内容，可能被限流，请稍后再试")
    if d.get("code") != 0:
        raise HTTPException(400, f"B 站返回错误：{d.get('message') or d.get('code')}")
    return d.get("data") or {}


async def _resolve_link(s: aiohttp.ClientSession, url: str):
    url = url.strip()
    m = re.search(r"https?://\S+", url)
    if m:
        url = m.group(0)
    if re.search(r"b23\.tv|bili2233\.cn", url):  # 短链接先展开
        async with s.get(url, allow_redirects=True) as r:
            url = str(r.url)
    page = None
    pm = re.search(r"[?&]p=(\d+)", url)
    if pm:
        page = int(pm.group(1))
    bv = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if bv:
        return {"bvid": bv.group(1)}, page
    av = re.search(r"\bav(\d+)", url, re.I)
    if av:
        return {"aid": av.group(1)}, page
    raise HTTPException(400, "没有识别到 B 站视频链接（需要包含 BV 号或 av 号）")


class InfoReq(BaseModel):
    url: str


@router.post("/bili/info")
async def bili_info(req: InfoReq, request: Request):
    await require_user(request)
    async with await _bili_session() as s:
        ident, page = await _resolve_link(s, req.url)
        q = f"bvid={ident['bvid']}" if "bvid" in ident else f"aid={ident['aid']}"
        d = await _bili_json(s, f"https://api.bilibili.com/x/web-interface/view?{q}")
    pages = [{"page": p.get("page"), "cid": p.get("cid"), "part": p.get("part") or "", "duration": p.get("duration") or 0}
             for p in (d.get("pages") or [])]
    return {"bvid": d.get("bvid"), "title": d.get("title"), "owner": (d.get("owner") or {}).get("name"),
            "duration": d.get("duration"), "pages": pages, "selected": page}


# ---------------- 任务与音频处理 ----------------
def _job_dir(jid: str) -> Path:
    return WORK / jid


def _cleanup():
    now = time.time()
    for jid, j in list(_jobs.items()):
        if now - j["created"] > JOB_TTL:
            shutil.rmtree(_job_dir(jid), ignore_errors=True)
            _jobs.pop(jid, None)
            event("job_expired", f"任务 {jid[:6]} 超过 {JOB_TTL // 3600} 小时，已清理")


async def _run(*args: str) -> str:
    p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    if p.returncode != 0:
        raise RuntimeError(err.decode(errors="ignore")[-400:])
    return err.decode(errors="ignore")


def _plan_chunks(duration: float, silences: list) -> list:
    """在 30–55 秒之间找最长的停顿切开；找不到就在 55 秒硬切。返回 [(start, end)] 秒。"""
    cuts, cur = [], 0.0
    while duration - cur > CHUNK_MAX:
        best = None
        for s0, s1 in silences:
            mid = (s0 + s1) / 2
            if cur + CHUNK_MIN <= mid <= cur + CHUNK_MAX and (best is None or (s1 - s0) > best[1]):
                best = (mid, s1 - s0)
        nxt = best[0] if best else cur + CHUNK_MAX
        cuts.append((cur, nxt))
        cur = nxt
    if duration - cur > 0.5 or not cuts:
        cuts.append((cur, duration))
    return cuts


async def _prepare(jid: str, src: Path, start_s: float, end_s: Optional[float]):
    j = _jobs[jid]
    d = _job_dir(jid)
    ff = ffmpeg_exe()
    try:
        j.update(state="processing", message="正在提取音频…")
        full = d / "full.mp3"
        args = [ff, "-hide_banner", "-y"]
        if start_s:
            args += ["-ss", f"{start_s:.2f}"]
        args += ["-i", str(src)]
        span = min(MAX_SECONDS, max(1.0, end_s - (start_s or 0))) if end_s else MAX_SECONDS
        args += ["-t", f"{span:.2f}", "-vn", "-ac", "1", "-ar", "16000", "-b:a", "24k", str(full)]
        await _run(*args)
        src.unlink(missing_ok=True)
        log = await _run(ff, "-hide_banner", "-i", str(full), "-af", "silencedetect=noise=-35dB:d=0.25", "-f", "null", "-")
        dm = re.findall(r"Duration: (\d+):(\d+):([\d.]+)", log)
        tm = re.findall(r"time=(\d+):(\d+):([\d.]+)", log)
        hms = (tm or dm)[-1] if (tm or dm) else ("0", "0", "0")
        duration = int(hms[0]) * 3600 + int(hms[1]) * 60 + float(hms[2])
        if duration < 1:
            raise RuntimeError("没有提取到有效的音频")
        starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
        ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
        silences = list(zip(starts, ends))
        plan = _plan_chunks(duration, silences)
        j.update(message=f"正在切分为 {len(plan)} 段…")
        chunks = []
        for i, (a, b) in enumerate(plan):
            await _run(ff, "-hide_banner", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(full), "-c", "copy", str(d / f"{i}.mp3"))
            chunks.append({"idx": i, "start_ms": round(a * 1000), "end_ms": round(b * 1000)})
        full.unlink(missing_ok=True)
        j.update(state="ready", message="", duration_ms=round(duration * 1000), chunks=chunks)
        event("job_ready", f"任务 {jid[:6]} 音频就绪：{round(duration)} 秒，{len(chunks)} 段，用时 {round(time.time() - j['created'])} 秒")
    except Exception as e:  # noqa: BLE001
        event("job_error", f"任务 {jid[:6]} 音频处理失败：{str(e)[-300:]}", "error")
        j.update(state="error", message=f"音频处理失败：{str(e)[-200:]}")


async def _bili_download(jid: str, bvid: str, cid: int, start_s: float, end_s: Optional[float]):
    j = _jobs[jid]
    d = _job_dir(jid)
    try:
        async with await _bili_session() as s:
            ref = f"https://www.bilibili.com/video/{bvid}"
            play = await _bili_json(s, f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&fnval=16&qn=16", ref)
            audios = sorted(((play.get("dash") or {}).get("audio") or []), key=lambda a: a.get("bandwidth") or 0)
            if not audios:
                raise RuntimeError("这个视频没有可下载的音频流（可能需要会员或已下架）")
            urls = [audios[0].get("baseUrl") or audios[0].get("base_url")] + (audios[0].get("backupUrl") or audios[0].get("backup_url") or [])
            src = d / "src.m4s"
            last = None
            for u in [x for x in urls if x]:
                try:
                    j.update(message="正在从 B 站下载音频…")
                    async with s.get(u, headers={"Referer": ref}, timeout=aiohttp.ClientTimeout(total=900)) as r:
                        if r.status >= 400:
                            raise RuntimeError(f"HTTP {r.status}")
                        total = int(r.headers.get("Content-Length") or 0)
                        got = 0
                        with open(src, "wb") as f:
                            async for part in r.content.iter_chunked(1 << 16):
                                f.write(part)
                                got += len(part)
                                if total:
                                    j["message"] = f"正在从 B 站下载音频… {got * 100 // total}%"
                    last = None
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
            if last:
                raise RuntimeError(f"音频下载失败：{last}")
        await _prepare(jid, src, start_s, end_s)
    except HTTPException as e:
        event("job_error", f"任务 {jid[:6]} 失败：{e.detail}", "error")
        j.update(state="error", message=str(e.detail))
    except Exception as e:  # noqa: BLE001
        event("job_error", f"任务 {jid[:6]} 失败：{str(e)[-300:]}", "error")
        j.update(state="error", message=str(e)[-200:])


def _new_job(uid: str, meta: dict) -> str:
    _cleanup()
    mine = [x for x in _jobs.values() if x["uid"] == uid and x["state"] in ("downloading", "processing")]
    if len(mine) >= 3:
        raise HTTPException(429, "同时处理的导入太多，请等前面的完成")
    if sum(1 for x in _jobs.values() if x["state"] in ("downloading", "processing")) >= MAX_ACTIVE_JOBS:
        raise HTTPException(429, "服务器正忙，请稍后再试")
    jid = uuid.uuid4().hex[:16]
    _job_dir(jid).mkdir(parents=True, exist_ok=True)
    _jobs[jid] = {"id": jid, "uid": uid, "created": time.time(), "state": "downloading", "message": "排队中…",
                  "duration_ms": None, "chunks": [], **meta}
    event("job_created", f"任务 {jid[:6]} 已创建（{meta.get('source')}）")
    return jid


class JobReq(BaseModel):
    bvid: str
    cid: int
    title: Optional[str] = None
    start_s: float = 0
    end_s: Optional[float] = None


@router.post("/jobs")
async def create_job(req: JobReq, request: Request):
    uid = await require_user(request)
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", req.bvid):
        raise HTTPException(400, "BV 号不正确")
    start = max(0.0, req.start_s or 0)
    end = req.end_s if req.end_s and req.end_s > start else None
    if end and end - start > MAX_SECONDS:
        raise HTTPException(400, "单次导入最长 2 小时")
    jid = _new_job(uid, {"source": "bili", "title": (req.title or "")[:200]})
    asyncio.create_task(_bili_download(jid, req.bvid, req.cid, start, end))
    return {"id": jid}


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), start_s: float = Form(0), end_s: float = Form(0)):
    uid = await require_user(request)
    jid = _new_job(uid, {"source": "file", "title": (file.filename or "本地文件")[:200]})
    src = _job_dir(jid) / "upload.bin"
    size = 0
    try:
        with open(src, "wb") as f:
            while True:
                part = await file.read(1 << 20)
                if not part:
                    break
                size += len(part)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "文件超过 300MB，请先截取需要的片段，或只上传音频")
                f.write(part)
    except HTTPException:
        shutil.rmtree(_job_dir(jid), ignore_errors=True)
        _jobs.pop(jid, None)
        raise
    start = max(0.0, start_s or 0)
    end = end_s if end_s and end_s > start else None
    _jobs[jid]["message"] = "上传完成，正在处理…"
    asyncio.create_task(_prepare(jid, src, start, end))
    return {"id": jid}


def _get_job(jid: str, uid: str) -> dict:
    j = _jobs.get(jid)
    if not j or j["uid"] != uid:
        if not j:  # 这个进程里根本没有该任务：通常说明进程重启过，或请求落到了另一个实例
            event("job_not_found", f"查询任务 {jid[:6]} 时本进程中不存在（已有 {len(_jobs)} 个任务）", "error")
        raise HTTPException(404, "导入任务不存在或已过期（服务重启后需要重新准备音频）")
    return j


@router.get("/jobs/{jid}")
async def job_status(jid: str, request: Request):
    uid = await require_user(request)
    j = _get_job(jid, uid)
    return {k: j[k] for k in ("id", "state", "message", "duration_ms", "chunks", "source", "title")}


@router.get("/jobs/{jid}/chunks/{idx}")
async def job_chunk(jid: str, idx: int, request: Request):
    uid = await require_user(request)
    j = _get_job(jid, uid)
    if j["state"] != "ready" or not (0 <= idx < len(j["chunks"])):
        raise HTTPException(404, "这一段音频还没准备好")
    p = _job_dir(jid) / f"{idx}.mp3"
    if not p.exists():
        raise HTTPException(404, "这一段音频已被清理，请重新导入")
    return FileResponse(p, media_type="audio/mpeg")
