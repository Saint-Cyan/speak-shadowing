"""腾讯云 API 3.0 调用（TC3-HMAC-SHA256 签名），只用于语音识别（ASR）和智聆口语评测（SOE）。

密钥只在服务器上：读取 config.json 的 tencent_secret_id / tencent_secret_key，或环境变量
SPEAK_TENCENT_SECRET_ID / SPEAK_TENCENT_SECRET_KEY。浏览器拿不到。
"""

import hashlib
import hmac
import json
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


# ---------------- 智聆口语评测（旧版 HTTP 接口，一次性评测） ----------------
async def soe_eval(ref_text: str, wav_b64: str, session_id: str, eval_mode: int = 1, coeff: float = 3.5) -> dict:
    p = {"SeqId": 1, "IsEnd": 1, "VoiceFileType": 2, "VoiceEncodeType": 1, "UserVoiceData": wav_b64,
         "SessionId": session_id, "RefText": ref_text, "WorkMode": 1, "EvalMode": eval_mode,
         "ScoreCoeff": coeff, "ServerType": 0}
    return await call("soe", "TransmitOralProcessWithInit", "2018-07-24", p, timeout=60, region="")
