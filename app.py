"""口语跟读工具后端：静态页面 + edge-tts 逐句合成接口。

接口：
  GET  /api/health          健康检查
  GET  /api/voices          可选口音列表
  /api/media/*             视频 / 音频导入（见 media.py）
  /ops、/api/ops/*         运维监控（见 ops.py）
  POST /api/tts             {"text": "...", "voice": "en-NZ-MollyNeural"}
                            返回 {"audio": base64 mp3, "words": [{"t": 起始ms, "d": 时长ms, "w": 单词}]}
"""

import asyncio
import base64
import hashlib
import json
import os
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

import edge_tts
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import CONFIG
import ops
from media import router as media_router

BASE = Path(__file__).parent
STATIC = BASE / "static"

VOICES = [
    {"id": "en-NZ-MollyNeural", "label": "新西兰 · Molly（女）"},
    {"id": "en-NZ-MitchellNeural", "label": "新西兰 · Mitchell（男）"},
    {"id": "en-US-AvaNeural", "label": "美国 · Ava（女）"},
    {"id": "en-US-AndrewNeural", "label": "美国 · Andrew（男）"},
    {"id": "en-US-EmmaNeural", "label": "美国 · Emma（女）"},
    {"id": "en-US-BrianNeural", "label": "美国 · Brian（男）"},
    {"id": "en-GB-SoniaNeural", "label": "英国 · Sonia（女）"},
    {"id": "en-GB-RyanNeural", "label": "英国 · Ryan（男）"},
    {"id": "en-AU-NatashaNeural", "label": "澳大利亚 · Natasha（女）"},
    {"id": "en-AU-WilliamMultilingualNeural", "label": "澳大利亚 · William（男）"},
]
VOICE_IDS = {v["id"] for v in VOICES}

MAX_CHARS = 800          # 单次合成上限（前端按句切分，一般远小于此）
CACHE_SIZE = 2000        # 内存缓存条数（单词发音也走缓存）
RATE_LIMIT = 90          # 每个 IP 每分钟最多请求数

app = FastAPI(title="speak-tool")
app.include_router(media_router)
app.include_router(ops.router)


@app.on_event("startup")
async def _ops_start():
    ops.start()


@app.middleware("http")
async def _count_requests(request: Request, call_next):
    t0 = time.time()
    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
        return resp
    finally:
        ops.record_request(request.url.path, status, (time.time() - t0) * 1000)

_cache: "OrderedDict[str, dict]" = OrderedDict()
_hits: "defaultdict[str, deque]" = defaultdict(deque)


class TTSReq(BaseModel):
    text: str
    voice: str = "en-NZ-MollyNeural"


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _rate_ok(ip: str) -> bool:
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return False
    q.append(now)
    return True


async def _synth(text: str, voice: str) -> dict:
    comm = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    audio = bytearray()
    words = []
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            # edge-tts 的 offset/duration 单位是 100 纳秒，换算为毫秒
            words.append({
                "t": round(chunk["offset"] / 10000),
                "d": round(chunk["duration"] / 10000),
                "w": chunk["text"],
            })
    if not audio:
        raise RuntimeError("empty audio")
    return {"audio": base64.b64encode(bytes(audio)).decode(), "words": words}


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/voices")
async def voices():
    return {"voices": VOICES}


@app.post("/api/tts")
async def tts(req: TTSReq, request: Request):
    text = " ".join(req.text.split())
    if not text:
        raise HTTPException(400, "文本为空")
    if len(text) > MAX_CHARS:
        raise HTTPException(413, f"单句过长（上限 {MAX_CHARS} 字符）")
    if req.voice not in VOICE_IDS:
        raise HTTPException(400, "不支持的口音")
    if not _rate_ok(_client_ip(request)):
        raise HTTPException(429, "请求太频繁，请稍后再试")

    key = hashlib.sha1(f"{req.voice}|{text}".encode()).hexdigest()
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]

    last_err = None
    for attempt in range(2):  # 微软接口偶发断连，重试一次
        try:
            result = await asyncio.wait_for(_synth(text, req.voice), timeout=30)
            _cache[key] = result
            if len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
            return result
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[tts] attempt {attempt + 1} failed: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(0.5)
    return JSONResponse({"detail": f"语音合成失败：{type(last_err).__name__}"}, status_code=502)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/ops")
async def ops_page():
    return FileResponse(STATIC / "ops.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
async def sw():
    # Service Worker 必须从根路径提供，作用域才能覆盖整个站点
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/config.js")
async def config_js():
    # 浏览器端需要的公开配置（不含任何密钥）
    pub = {"cloudEndpoint": CONFIG["cloud_endpoint"], "cloudPublishableKey": CONFIG["cloud_publishable_key"],
           "oauthRelayUrl": CONFIG["oauth_relay_url"]}
    body = "window.APP_CONFIG = " + json.dumps(pub, ensure_ascii=False) + ";\n"
    return Response(body, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    data = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    if CONFIG["share_code"]:  # 托管网关要求带访问码，否则“添加到主屏幕”后打不开
        data["start_url"] = "/?sharecode=" + CONFIG["share_code"]
    return JSONResponse(data, media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
