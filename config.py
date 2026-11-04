"""站点配置：优先读环境变量，其次读同目录下的 config.json（不提交到仓库），都没有则为空。

  SPEAK_CLOUD_ENDPOINT          云服务地址（WorkBuddy 托管时就是站点自己的域名）
  SPEAK_CLOUD_PUBLISHABLE_KEY   云服务公开配置（wbpk_ 开头，会出现在浏览器里，不是密钥）
  SPEAK_OAUTH_RELAY_URL         微信等第三方登录的中转地址（WorkBuddy 提供）
  SPEAK_SHARE_CODE              托管网关的访问码（可选，用于“添加到主屏幕”后的启动地址）
"""

import json
import os
from pathlib import Path

_FILE = Path(__file__).parent / "config.json"
_KEYS = {
    "cloud_endpoint": "SPEAK_CLOUD_ENDPOINT",
    "cloud_publishable_key": "SPEAK_CLOUD_PUBLISHABLE_KEY",
    "oauth_relay_url": "SPEAK_OAUTH_RELAY_URL",
    "share_code": "SPEAK_SHARE_CODE",
    "tencent_secret_id": "SPEAK_TENCENT_SECRET_ID",    # 腾讯云语音识别 + 智聆口语评测（只在服务器上使用）
    "tencent_secret_key": "SPEAK_TENCENT_SECRET_KEY",
    "tencent_region": "SPEAK_TENCENT_REGION",
}


def load() -> dict:
    data = {}
    if _FILE.exists():
        try:
            data = json.loads(_FILE.read_text(encoding="utf-8"))
        except ValueError as e:
            print(f"[config] config.json 格式不对，已忽略：{e}", flush=True)
    cfg = {k: (os.environ.get(env) or data.get(k) or "").strip() for k, env in _KEYS.items()}
    cfg["cloud_endpoint"] = cfg["cloud_endpoint"].rstrip("/")
    if not cfg["cloud_endpoint"] or not cfg["cloud_publishable_key"]:
        print("[config] 未配置云服务（cloud_endpoint / cloud_publishable_key）：登录、话题、AI 等功能不可用", flush=True)
    return cfg


CONFIG = load()
