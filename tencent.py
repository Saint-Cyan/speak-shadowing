"""腾讯云 API 3.0 调用（TC3-HMAC-SHA256 签名），只用于语音识别（ASR）和智聆口语评测（SOE）。

密钥只在服务器上：读取 config.json 的 tencent_secret_id / tencent_secret_key，或环境变量
SPEAK_TENCENT_SECRET_ID / SPEAK_TENCENT_SECRET_KEY。浏览器拿不到。
"""

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone

import aiohttp

from config import CONFIG

SECRET_ID = CONFIG.get("tencent_secret_id", "")
SECRET_KEY = CONFIG.get("tencent_secret_key", "")
REGION = CONFIG.get("tencent_region", "") or "ap-shanghai"


def enabled() -> bool:
    return bool(SECRET_ID and SECRET_KEY)


class TencentError(Exception):
    def __init__(self, code: str, message: str, request_id: str = ""):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.request_id = code, message, request_id


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _headers(service: str, host: str, action: str, version: str, payload: str, region: str) -> dict:
    ts = int(time.time())
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    ct = "application/json; charset=utf-8"
    canonical = "\n".join([
        "POST", "/", "", f"content-type:{ct}\nhost:{host}\nx-tc-action:{action.lower()}\n",
        "content-type;host;x-tc-action", hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    ])
    scope = f"{date}/{service}/tc3_request"
    to_sign = "\n".join(["TC3-HMAC-SHA256", str(ts), scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    k = _sign(_sign(_sign(("TC3" + SECRET_KEY).encode("utf-8"), date), service), "tc3_request")
    sig = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    h = {
        "Authorization": f"TC3-HMAC-SHA256 Credential={SECRET_ID}/{scope}, SignedHeaders=content-type;host;x-tc-action, Signature={sig}",
        "Content-Type": ct, "Host": host, "X-TC-Action": action, "X-TC-Timestamp": str(ts), "X-TC-Version": version,
    }
    if region:
        h["X-TC-Region"] = region
    return h


async def call(service: str, action: str, version: str, params: dict, timeout: float = 30, region: str = REGION) -> dict:
    if not enabled():
        raise TencentError("NotConfigured", "服务器没有配置腾讯云密钥")
    host = f"{service}.tencentcloudapi.com"
    payload = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=True) as s:
        async with s.post(f"https://{host}/", data=payload.encode("utf-8"),
                          headers=_headers(service, host, action, version, payload, region)) as r:
            text = await r.text()
    try:
        resp = json.loads(text)["Response"]
    except Exception:  # noqa: BLE001
        raise TencentError("BadResponse", f"HTTP {r.status}: {text[:200]}")
    if "Error" in resp:
        e = resp["Error"]
        raise TencentError(e.get("Code", "Error"), e.get("Message", ""), resp.get("RequestId", ""))
    return resp


# ---------------- 语音识别：录音文件识别 ----------------
ASR_ENGINE = "16k_en"   # 英文引擎，支持说话人分离和词级时间戳


async def asr_create(*, url: str = None, data_b64: str = None, data_len: int = 0) -> str:
    p = {"EngineModelType": ASR_ENGINE, "ChannelNum": 1, "ResTextFormat": 2, "SpeakerDiarization": 1, "SpeakerNumber": 0}
    if url:
        p.update(SourceType=0, Url=url)
    else:
        p.update(SourceType=1, Data=data_b64, DataLen=data_len)
    r = await call("asr", "CreateRecTask", "2019-06-14", p, timeout=120)
    return str(r["Data"]["TaskId"])


async def asr_status(task_id: str) -> dict:
    r = await call("asr", "DescribeTaskStatus", "2019-06-14", {"TaskId": int(task_id)})
    return r["Data"]


# ---------------- 智聆口语评测（新版，WebSocket 录音识别模式：一次性发送 ≤60 秒音频） ----------------
_appid = {"v": CONFIG.get("tencent_appid", "")}


async def appid() -> str:
    """新版口语评测的地址里要带账号 AppID：优先读配置 tencent_appid，没有就向腾讯云查询一次。"""
    if not _appid["v"]:
        r = await call("cam", "GetUserAppId", "2019-01-16", {}, region="ap-guangzhou")
        _appid["v"] = str(r["AppId"])
    return _appid["v"]


SOE_ERRORS = {
    4001: "参数不合法", 4002: "鉴权失败（密钥不对）", 4003: "账号还没开通“智聆口语评测（新版）”",
    4004: "口语评测资源包已用完，请开通后付费或购买资源包", 4005: "腾讯云账户欠费，口语评测已停用",
    4006: "口语评测并发超限，请稍后再试", 4007: "录音解码失败", 4014: "录音太长", 4106: "录音太长",
    4102: "参考句子为空", 4103: "句子里有评测引擎不认识的词", 4104: "句子太长",
    4105: "录音里没有检测到人声", 4108: "录音里没有识别到有效发音", 4114: "句子内容无效", 4115: "句子语言和评测引擎不匹配",
}


async def soe_eval(ref_text: str, wav: bytes, eval_mode: int = 1, coeff: float = 3.5) -> dict:
    """返回与旧版接口相同结构的结果（SuggestedScore / PronAccuracy / PronFluency / PronCompletion / Words[].PhoneInfos）。"""
    if not enabled():
        raise TencentError("NotConfigured", "服务器没有配置腾讯云密钥")
    import base64 as _b64
    import random
    import urllib.parse
    aid = await appid()
    now = int(time.time())
    p = {"secretid": SECRET_ID, "timestamp": now, "expired": now + 3600, "nonce": random.randint(1, 9_999_999_999),
         "server_engine_type": "16k_en", "voice_id": uuid_str(), "voice_format": 1, "text_mode": 0,
         "ref_text": ref_text, "eval_mode": eval_mode, "score_coeff": coeff, "sentence_info_enabled": 0, "rec_mode": 1}
    keys = sorted(p)
    raw = f"soe.cloud.tencent.com/soe/api/{aid}?" + "&".join(f"{k}={p[k]}" for k in keys)
    sig = _b64.b64encode(hmac.new(SECRET_KEY.encode(), raw.encode("utf-8"), hashlib.sha1).digest()).decode()
    url = f"wss://soe.cloud.tencent.com/soe/api/{aid}?" + urllib.parse.urlencode([(k, p[k]) for k in keys] + [("signature", sig)])
    result = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), trust_env=True) as s:
        async with s.ws_connect(url, heartbeat=None) as ws:
            first = json.loads((await ws.receive(timeout=15)).data)
            if first.get("code"):
                raise TencentError(str(first["code"]), SOE_ERRORS.get(first["code"], first.get("message", "")), first.get("voice_id", ""))
            await ws.send_bytes(wav)
            await ws.send_str('{"type": "end"}')
            while True:
                m = await ws.receive(timeout=45)
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                j = json.loads(m.data)
                if j.get("code"):
                    raise TencentError(str(j["code"]), SOE_ERRORS.get(j["code"], j.get("message", "")), j.get("voice_id", ""))
                if j.get("result"):
                    r = j["result"]
                    result = json.loads(r) if isinstance(r, str) else r
                if j.get("final") == 1:
                    break
    if not result:
        raise TencentError("NoResult", "口语评测没有返回结果")
    for w in result.get("Words") or []:  # 新版 ReferenceWord 带序号后缀，如 thanks_0
        if isinstance(w.get("ReferenceWord"), str):
            w["ReferenceWord"] = re.sub(r"_\d+$", "", w["ReferenceWord"])
    return result


def uuid_str() -> str:
    import uuid
    return str(uuid.uuid4())
