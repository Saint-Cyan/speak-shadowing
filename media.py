"""视频 / 音频导入：解析 B 站链接、下载音频或接收本地文件，用 ffmpeg 转成单声道并按静音切成 ≤55 秒的小段。

整段音频交给腾讯云录音文件识别（说话人分离 + 词级时间戳）；切段只用于保存每句的原声。翻译整理在浏览器里调用模型完成。所有接口都要求带登录令牌，后端会向云服务核实身份，避免被陌生人当下载器用。

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
import secrets
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

import tencent
from config import CONFIG
from ops import event

router = APIRouter(prefix="/api/media")

CLOUD_ENDPOINT = CONFIG["cloud_endpoint"]
PUBLISHABLE_KEY = CONFIG["cloud_publishable_key"]  # 公开配置，不是密钥
WORK = Path(os.environ.get("MEDIA_DIR", "/tmp/speak-media"))
MAX_SECONDS = 2 * 3600        # 单次导入上限 2 小时
MAX_UPLOAD = 300 * 1024 * 1024
CHUNK_MAX = 180.0             # 分段只用于保存原声（整段交给腾讯云识别），在 90–180 秒之间找停顿切开
CHUNK_MIN = 90.0
ASR_INLINE_MAX = 4_800_000    # 不超过约 4.8MB 的音频直接随请求提交，更大的让腾讯云按链接下载
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
    _token_shape(token, key)
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


_token_seen: deque = deque(maxlen=20)


def _token_shape(token: str, key: str):
    """只记录登录令牌的“形态”（长度、段数、摘要前 8 位、声明的签发/过期时间），不记录内容，用来排查令牌为何每次不同。"""
    shape = {"t": round(time.time(), 1), "len": len(token), "parts": token.count(".") + 1, "digest": key[:8]}
    try:
        part = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        shape.update(iat=payload.get("iat"), exp=payload.get("exp"), has_sub=bool(payload.get("sub")))
    except Exception:  # noqa: BLE001
        shape["jwt"] = False
    if not _token_seen or _token_seen[-1]["digest"] != shape["digest"]:
        _token_seen.append(shape)


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
            "ffmpeg": ff, "active_jobs": active, "token_shapes": list(_token_seen)}


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


def _plan_chunks(duration: float, silences: list, cmax: float = CHUNK_MAX, cmin: float = CHUNK_MIN) -> list:
    """在 cmin–cmax 秒之间找最长的停顿切开；找不到就在 cmax 秒硬切。返回 [(start, end)] 秒。"""
    cuts, cur = [], 0.0
    while duration - cur > cmax:
        best = None
        for s0, s1 in silences:
            mid = (s0 + s1) / 2
            if cur + cmin <= mid <= cur + cmax and (best is None or (s1 - s0) > best[1]):
                best = (mid, s1 - s0)
        nxt = best[0] if best else cur + cmax
        cuts.append((cur, nxt))
        cur = nxt
    if duration - cur > 0.5 or not cuts:
        cuts.append((cur, duration))
    return cuts


_PUNCT = set(",.?!;:")


def _split_sentences(detail: list) -> list:
    """把腾讯云的识别结果（按说话人 / 停顿成段，带词级时间戳）按句末标点再切成适合跟读的句子。"""
    out = []
    for it in detail or []:
        base = int(it.get("StartMs") or 0)
        sp = int(it.get("SpeakerId") or 0)
        words = it.get("Words") or []
        if not words:
            text = (it.get("FinalSentence") or "").strip()
            if text:
                out.append({"s": base, "e": int(it.get("EndMs") or base), "sp": sp, "t": text})
            continue
        cur, n_words = [], 0

        def flush():
            nonlocal cur, n_words
            toks = [w for w in cur if (w.get("Word") or "").strip()]
            if not toks:
                cur, n_words = [], 0
                return
            text = ""
            for w in toks:
                t = w["Word"].strip()
                text += t if (t in _PUNCT or not text) else " " + t
            real = [w for w in toks if w["Word"].strip() not in _PUNCT]
            if real:
                out.append({"s": base + int(real[0].get("OffsetStartMs") or 0), "e": base + int(real[-1].get("OffsetEndMs") or 0),
                            "sp": sp, "t": text.strip()})
            elif out:
                out[-1]["t"] += text.strip()
            cur, n_words = [], 0

        for w in words:
            t = (w.get("Word") or "").strip()
            cur.append(w)
            if t and t not in _PUNCT:
                n_words += 1
            if (t in (".", "?", "!") and n_words >= 3) or (t in (",", ";") and n_words >= 30):
                flush()
        flush()
    return [x for x in out if re.search(r"[A-Za-z]", x["t"])]


async def _tencent_asr(jid: str, full: Path, duration: float) -> list:
    j = _jobs[jid]
    size = full.stat().st_size
    j.update(state="processing", message="腾讯云语音识别：提交中…")
    if size <= ASR_INLINE_MAX:
        raw = full.read_bytes()
        task = await tencent.asr_create(data_b64=base64.b64encode(raw).decode(), data_len=len(raw))
        how = "直接提交"
    else:
        url = f"{CLOUD_ENDPOINT}/api/media/jobs/{jid}/full.mp3?k={j['key']}"
        if CONFIG.get("share_code"):
            url += f"&sharecode={CONFIG['share_code']}"
        task = await tencent.asr_create(url=url)
        how = "链接下载"
    event("asr_submitted", f"任务 {jid[:6]} 已提交腾讯云识别（{round(duration)} 秒，{size >> 10}KB，{how}）")
    t0 = time.time()
    while True:
        await asyncio.sleep(3)
        try:
            d = await tencent.asr_status(task)
        except tencent.TencentError as e:
            if time.time() - t0 > 3 * 3600:
                raise
            print(f"[media] asr status error: {e}", flush=True)
            continue
        st = d.get("StatusStr")
        waited = round(time.time() - t0)
        if st == "success":
            sents = _split_sentences(d.get("ResultDetail"))
            j["asr_ms"] = round((time.time() - t0) * 1000)
            event("asr_done", f"任务 {jid[:6]} 腾讯云识别完成：{len(sents)} 句，{len({x['sp'] for x in sents})} 位说话人，用时 {waited} 秒")
            return sents
        if st == "failed":
            raise RuntimeError(d.get("ErrorMsg") or "识别失败")
        j["message"] = f"腾讯云语音识别中…（已等待 {waited} 秒；1 小时音频一般 1–3 分钟）"
        if time.time() - t0 > 3 * 3600:
            raise RuntimeError("识别超过 3 小时仍未完成")


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
        if not tencent.enabled():
            raise RuntimeError("服务器没有配置腾讯云密钥，无法识别")
        try:
            j["sentences"] = await _tencent_asr(jid, full, duration)
        except Exception as e:  # noqa: BLE001
            event("asr_failed", f"任务 {jid[:6]} 腾讯云识别失败：{str(e)[-200:]}", "error")
            raise RuntimeError(f"腾讯云语音识别失败：{str(e)[-200:]}")
        engine = j["engine"] = "tencent"
        plan = _plan_chunks(duration, silences)
        j.update(message=f"正在切分为 {len(plan)} 段…")
        chunks = []
        for i, (a, b) in enumerate(plan):
            await _run(ff, "-hide_banner", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(full), "-c", "copy", str(d / f"{i}.mp3"))
            chunks.append({"idx": i, "start_ms": round(a * 1000), "end_ms": round(b * 1000)})
        full.unlink(missing_ok=True)
        j.update(state="ready", message="", duration_ms=round(duration * 1000), chunks=chunks)
        event("job_ready", f"任务 {jid[:6]} 音频就绪：{round(duration)} 秒，{len(chunks)} 段（{engine}），用时 {round(time.time() - j['created'])} 秒")
    except Exception as e:  # noqa: BLE001
        event("job_error", f"任务 {jid[:6]} 音频处理失败：{str(e)[-300:]}", "error")
        j.update(state="error", message=str(e)[-200:] if "腾讯云" in str(e) else f"音频处理失败：{str(e)[-200:]}")


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
    _jobs[jid] = {"id": jid, "uid": uid, "key": secrets.token_urlsafe(24), "created": time.time(), "state": "downloading",
                  "message": "排队中…", "duration_ms": None, "chunks": [], **meta}
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
    return {"id": jid, "key": _jobs[jid]["key"]}


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
    return {"id": jid, "key": _jobs[jid]["key"]}


def _get_job(jid: str, request: Request) -> dict:
    """按任务凭证访问：创建任务时返回的随机 key，前端放在 X-Job-Key 头里。不再比对用户身份。"""
    j = _jobs.get(jid)
    if not j:  # 这个进程里根本没有该任务：通常说明进程重启过，或请求落到了另一个实例
        event("job_not_found", f"查询任务 {jid[:6]} 时本进程中不存在（已有 {len(_jobs)} 个任务）", "error")
        raise HTTPException(404, "导入任务不存在或已过期（服务重启后需要重新准备音频）")
    key = request.headers.get("x-job-key", "")
    if not key or not secrets.compare_digest(key, j["key"]):
        event("job_key_mismatch", f"查询任务 {jid[:6]} 时任务凭证{'缺失' if not key else '不匹配'}", "error")
        raise HTTPException(403, "无权访问这个导入任务（任务凭证不符），请重新开始导入")
    return j


@router.get("/jobs/{jid}")
async def job_status(jid: str, request: Request):
    if not _ip_rate_ok(_client_ip(request)):
        raise HTTPException(429, "请求太频繁，请稍后再试")
    j = _get_job(jid, request)
    out = {k: j[k] for k in ("id", "state", "message", "duration_ms", "chunks", "source", "title")}
    out.update(engine=j.get("engine"), asr_error=j.get("asr_error"), asr_ms=j.get("asr_ms"))
    return out


@router.get("/jobs/{jid}/transcript")
async def job_transcript(jid: str, request: Request):
    j = _get_job(jid, request)
    if j.get("engine") != "tencent":
        raise HTTPException(404, "这个任务没有整段识别结果")
    return {"sentences": j.get("sentences") or []}


@router.get("/jobs/{jid}/full.mp3")
async def job_full(jid: str, k: str = ""):
    """给腾讯云按链接下载整段音频用：凭任务凭证访问（放在查询参数里，腾讯云无法带请求头）。"""
    j = _jobs.get(jid)
    if not j or not k or not secrets.compare_digest(k, j["key"]):
        raise HTTPException(404, "not found")
    p = _job_dir(jid) / "full.mp3"
    if not p.exists():
        raise HTTPException(404, "not found")
    event("asr_fetch", f"任务 {jid[:6]} 的整段音频被下载（腾讯云识别）")
    return FileResponse(p, media_type="audio/mpeg")


@router.get("/jobs/{jid}/chunks/{idx}")
async def job_chunk(jid: str, idx: int, request: Request):
    if not _ip_rate_ok(_client_ip(request)):
        raise HTTPException(429, "请求太频繁，请稍后再试")
    j = _get_job(jid, request)
    if j["state"] != "ready" or not (0 <= idx < len(j["chunks"])):
        raise HTTPException(404, "这一段音频还没准备好")
    p = _job_dir(jid) / f"{idx}.mp3"
    if not p.exists():
        raise HTTPException(404, "这一段音频已被清理，请重新导入")
    return FileResponse(p, media_type="audio/mpeg")


# ---------------- 发音评测：腾讯云智聆口语评测 ----------------
pron_router = APIRouter(prefix="/api/pron")


class PronReq(BaseModel):
    ref_text: str
    wav_b64: str


@pron_router.post("/assess")
async def pron_assess(req: PronReq, request: Request):
    await require_user(request)
    if not tencent.enabled():
        raise HTTPException(503, "NotConfigured: 服务器没有配置腾讯云密钥")
    ref = " ".join(req.ref_text.split())
    n = len(re.findall(r"[A-Za-z0-9']+", ref))
    if not n:
        raise HTTPException(400, "句子里没有英文单词")
    if n > 120:
        raise HTTPException(400, "句子太长（口语评测最多 120 个单词）")
    if len(req.wav_b64) > 4_000_000:
        raise HTTPException(413, "录音太长（口语评测最多约 90 秒）")
    t0 = time.time()
    try:
        r = await tencent.soe_eval(ref, req.wav_b64, uuid.uuid4().hex, eval_mode=1 if n <= 30 else 2)
    except tencent.TencentError as e:
        event("soe_error", f"口语评测失败：{e.code} {e.message[:200]}", "error")
        raise HTTPException(502, f"{e.code}: {e.message}")
    tags = {0: "ok", 1: "inserted", 2: "missing", 3: "mispronounced", 4: "unknown"}
    words = []
    for w in r.get("Words") or []:
        phones = [{"p": ph.get("Phone"), "ref": ph.get("ReferencePhone"), "letter": ph.get("ReferenceLetter"),
                   "score": round(ph.get("PronAccuracy") or 0), "stress": ph.get("Stress"), "detected_stress": ph.get("DetectedStress")}
                  for ph in (w.get("PhoneInfos") or [])]
        words.append({"w": w.get("ReferenceWord") or w.get("Word"), "heard": w.get("Word"), "score": round(w.get("PronAccuracy") or 0),
                      "tag": tags.get(w.get("MatchTag"), "unknown"), "phones": phones})
    pct = lambda x: None if x is None or x < 0 else round(x * 100)  # noqa: E731
    return {"overall": round(r.get("SuggestedScore") or 0), "accuracy": round(r.get("PronAccuracy") or 0),
            "fluency": pct(r.get("PronFluency")), "completeness": pct(r.get("PronCompletion")),
            "words": words, "ms": round((time.time() - t0) * 1000)}
