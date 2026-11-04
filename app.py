"""口语跟读工具后端：静态页面 + 逐句语音合成接口（微软 edge-tts / 腾讯云，见 tts.py）。

接口：
  GET  /api/health          健康检查
  GET  /api/voices          可选口音列表
  /api/media/*             视频 / 音频导入（见 media.py）
  /ops、/api/ops/*         运维监控（见 ops.py）
  POST /api/tts             {"text": "...", "voice": "en-NZ-MollyNeural" 或 "403006"}（按音色判断引擎）
                            返回 {"audio": base64 mp3, "words": [{"t": 起始ms, "d": 时长ms, "w": 单词}]}
"""

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import CONFIG
import ops
import tencent
import tts as tts_engine
from media import pron_router
from media import router as media_router

BASE = Path(__file__).parent
STATIC = BASE / "static"


MAX_CHARS = 2000         # 单次合成上限（超过 500 字符会拆成多段合成再拼接）
RATE_LIMIT = 90          # 每个 IP 每分钟最多请求数

app = FastAPI(title="speak-tool")
app.include_router(media_router)
app.include_router(pron_router)
app.include_router(ops.router)


@app.on_event("startup")
async def _ops_start():
    ops.start()
    asyncio.get_event_loop().create_task(tts_engine.edge_watch())  # 部署后立即检测 edge-tts 是否可用（不阻塞启动）


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

_hits: "defaultdict[str, deque]" = defaultdict(deque)


class TTSReq(BaseModel):
    text: str
    voice: str = tts_engine.DEFAULT_A


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


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/voices")
async def voices():
    """两个引擎的音色列表、可用性（edge-tts 在启动时检测）和站点默认引擎。"""
    return tts_engine.status()


@app.post("/api/tts")
async def tts(req: TTSReq, request: Request):
    text = " ".join(req.text.split())
    if not text:
        raise HTTPException(400, "文本为空")
    if len(text) > MAX_CHARS:
        raise HTTPException(413, f"单句过长（上限 {MAX_CHARS} 字符）")
    if not tts_engine.engine_of(req.voice):
        raise HTTPException(400, "不支持的音色，请在播放设置里重新选择")
    if not _rate_ok(_client_ip(request)):
        raise HTTPException(429, "请求太频繁，请稍后再试")
    try:
        return await asyncio.wait_for(tts_engine.synth(text, req.voice), timeout=60)
    except tts_engine.TTSError as e:
        return JSONResponse({"detail": str(e)}, status_code=502)
    except asyncio.TimeoutError:
        return JSONResponse({"detail": "语音合成超时，请重试"}, status_code=504)


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
           "oauthRelayUrl": CONFIG["oauth_relay_url"], "speechEngine": "tencent" if tencent.enabled() else ""}
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
