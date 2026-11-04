"""语音合成：两个引擎可选，都返回音频和每个单词的时间轴（用于逐词高亮、单词“句中读音”）。

- 微软 edge-tts：免费、口音多（新西兰 / 英 / 澳 / 美），但不是微软正式开放的接口，可能随时不可用。
  服务启动时（也就是每次部署后）会合成一句短语检测是否可用，之后每 30 分钟复查；不可用时前端禁用这个选项。
- 腾讯云 TextToVoice：正式商用接口，国内稳定，按字符计费（有免费额度）。

腾讯云逐词时间的来源：
  - 精品 / 大模型音色：合成接口直接返回（EnableSubtitle）。
  - 超自然大模型音色：合成接口不返回，再把合成好的音频交给腾讯云「一句话识别」（带词级时间戳）对齐出来；
    识别失败时按单词长度在整段时长里估算。
两种来源都会按原文重新对齐，保证返回的单词就是原文里的写法（前端按原文查找单词）。

合成结果写入内存缓存和磁盘缓存（TTS_CACHE_DIR），同一音色同一句话只合成一次，节省免费额度。
"""

import asyncio
import base64
import difflib
import hashlib
import json
import os
import re
import time
import uuid
from collections import OrderedDict
from pathlib import Path

import edge_tts

import tencent
from config import CONFIG

EDGE_VOICES = [
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
EDGE_IDS = {v["id"] for v in EDGE_VOICES}
EDGE_DEFAULT = ("en-NZ-MollyNeural", "en-NZ-MitchellNeural")

VOICES = [
    {"id": "403006", "label": "美式 · YunMia（女 · 超自然）", "timing": "asr", "rate": 24000, "tier": "超自然大模型"},
    {"id": "403007", "label": "美式 · YunLeo（男 · 超自然）", "timing": "asr", "rate": 24000, "tier": "超自然大模型"},
    {"id": "501009", "label": "WeWinny（女 · 大模型）", "timing": "native", "rate": 24000, "tier": "大模型"},
    {"id": "501008", "label": "WeJames（男 · 大模型）", "timing": "native", "rate": 24000, "tier": "大模型"},
    {"id": "101050", "label": "WeJack（男 · 精品）", "timing": "native", "rate": 16000, "tier": "精品"},
]
BY_ID = {v["id"]: v for v in VOICES}
DEFAULT_A, DEFAULT_B = "403006", "403007"
TC_DEFAULT = (DEFAULT_A, DEFAULT_B)
# 站点默认引擎：config.json 的 tts_engine（edge / tencent），默认 edge
DEFAULT_ENGINE = (CONFIG.get("tts_engine") or "edge").strip().lower()
if DEFAULT_ENGINE not in ("edge", "tencent"):
    DEFAULT_ENGINE = "edge"
EDGE = {"ok": None, "checked_at": None, "error": None, "latency_ms": None, "last_ok_at": None}
EDGE_RECHECK = 30 * 60

PIECE_MAX = 480                       # 腾讯云单次英文最多 500 个字符，留余量
CACHE_SIZE = 2000
CACHE_DIR = Path(os.environ.get("TTS_CACHE_DIR", "/tmp/speak-tts"))
_mem: "OrderedDict[str, dict]" = OrderedDict()
_sem = asyncio.Semaphore(6)           # 超自然音色默认 10 并发，留余量
STATS = {"synth_calls": 0, "edge_calls": 0, "chars": {}, "cache_mem": 0, "cache_disk": 0, "align_asr": 0, "align_estimate": 0,
         "errors": 0, "last_error": None, "last_error_at": None}

TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*")


class TTSError(Exception):
    pass


# ---------------- 对齐：把参考时间（识别 / 字幕）映射到原文单词 ----------------
def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def align(text: str, ref: list, duration_ms: int) -> list:
    """ref: [(word, start_ms, end_ms)]。返回 [{"t", "d", "w"}]，w 是原文中的单词写法。"""
    tokens = [m.group() for m in TOKEN_RE.finditer(text)]
    if not tokens:
        return []
    o_chars, o_tok = [], []
    for i, t in enumerate(tokens):
        for c in _alnum(t):
            o_chars.append(c)
            o_tok.append(i)
    r_chars, r_time = [], []
    for w, s, e in ref:
        letters = _alnum(w)
        if not letters:
            continue
        span = max(1, e - s)
        for k, c in enumerate(letters):
            r_chars.append(c)
            r_time.append((s + span * k / len(letters), s + span * (k + 1) / len(letters)))
    start = [None] * len(tokens)
    end = [None] * len(tokens)
    if r_chars:
        sm = difflib.SequenceMatcher(None, "".join(o_chars), "".join(r_chars), autojunk=False)
        for a, b, n in sm.get_matching_blocks():
            for k in range(n):
                i, (ts, te) = o_tok[a + k], r_time[b + k]
                start[i] = ts if start[i] is None else min(start[i], ts)
                end[i] = te if end[i] is None else max(end[i], te)
    # 没对上的单词：在前后已知时间之间按字母数插值；一个都没对上就整段估算
    if all(x is None for x in start):
        return estimate(text, duration_ms)
    i = 0
    while i < len(tokens):
        if start[i] is not None:
            i += 1
            continue
        j = i
        while j < len(tokens) and start[j] is None:
            j += 1
        lo = end[i - 1] if i > 0 else max(0, (start[j] if j < len(tokens) else duration_ms) - 300 * (j - i))
        hi = start[j] if j < len(tokens) else min(duration_ms, lo + 400 * (j - i))
        hi = max(hi, lo + 60 * (j - i))
        weights = [len(_alnum(tokens[k])) + 1 for k in range(i, j)]
        tot, acc = sum(weights), lo
        for k, wgt in zip(range(i, j), weights):
            seg = (hi - lo) * wgt / tot
            start[k], end[k] = acc, acc + seg
            acc += seg
        i = j
    out, prev = [], 0
    for t, s, e in zip(tokens, start, end):
        s = max(prev, s)
        e = max(s + 40, e)
        out.append({"t": round(s), "d": round(e - s), "w": t})
        prev = e
    return out


def estimate(text: str, duration_ms: int) -> list:
    """没有任何参考时间时，按单词长度（标点处多留停顿）把整段时长分给每个单词。"""
    tokens = list(TOKEN_RE.finditer(text))
    if not tokens:
        return []
    lead, tail = 150, 250
    span = max(200, duration_ms - lead - tail)
    weights = []
    for k, m in enumerate(tokens):
        nxt = text[m.end(): tokens[k + 1].start() if k + 1 < len(tokens) else len(text)]
        weights.append((len(m.group()) + 2, 4 if re.search(r"[.?!;:]", nxt) else 2 if "," in nxt else 0))
    tot = sum(a + b for a, b in weights)
    out, acc = [], lead
    for m, (a, b) in zip(tokens, weights):
        d = span * a / tot
        out.append({"t": round(acc), "d": round(d), "w": m.group()})
        acc += d + span * b / tot
    return out


# ---------------- 合成 ----------------
def _split(text: str) -> list:
    if len(text) <= PIECE_MAX:
        return [text]
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.?!;])\s+", text):
        while len(sent) > PIECE_MAX:  # 单句过长：在逗号或空格处断开
            cut = max(sent.rfind(", ", 0, PIECE_MAX), sent.rfind(" ", 0, PIECE_MAX))
            cut = cut if cut > 0 else PIECE_MAX
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(sent[:cut + 1].strip())
            sent = sent[cut + 1:].strip()
        if cur and len(cur) + 1 + len(sent) > PIECE_MAX:
            parts.append(cur)
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


def _friendly(e: "tencent.TencentError", voice: dict) -> str:
    if e.code in ("UnsupportedOperation.PkgExhausted", "UnsupportedOperation.NoFreeAccount"):
        return (f"腾讯云语音合成「{voice['tier']}」音色的免费额度已用完。可以在播放设置里换成其他音色，"
                "或到腾讯云语音合成控制台开通后付费 / 购买资源包")
    if e.code in ("UnsupportedOperation.ServerNotOpen", "InvalidParameterValue.AppIdNotRegistered"):
        return "腾讯云账号还没开通语音合成服务"
    if e.code in ("UnsupportedOperation.AccountArrears",):
        return "腾讯云账号欠费，语音合成已停用"
    if e.code in ("LimitExceeded.AccessLimit", "InternalError.ExceedMaxLimit"):
        return "语音合成请求太频繁，请稍后再试"
    return f"语音合成失败：{e.code} {e.message[:120]}"


async def _piece(text: str, voice: dict) -> tuple:
    """合成一段（≤500 字符），返回 (mp3 字节, 词时间 [{t,d,w}], 时长 ms)。"""
    params = {"Text": text, "SessionId": uuid.uuid4().hex, "VoiceType": int(voice["id"]), "PrimaryLanguage": 2,
              "Codec": "mp3", "SampleRate": voice["rate"], "EnableSubtitle": voice["timing"] == "native", "ModelType": 1}
    r = None
    for attempt in range(2):  # 网络抖动或限流时重试一次；额度、参数、鉴权类错误不重试
        try:
            async with _sem:
                r = await tencent.call("tts", "TextToVoice", "2019-08-23", params, timeout=30)
            break
        except tencent.TencentError as e:
            if attempt == 1 or e.code.startswith(("UnsupportedOperation", "InvalidParameter", "AuthFailure", "NotConfigured")):
                raise TTSError(_friendly(e, voice))
            await asyncio.sleep(0.6)
        except Exception as e:  # noqa: BLE001
            if attempt == 1:
                raise TTSError(f"语音合成失败：{type(e).__name__}")
            await asyncio.sleep(0.6)
    STATS["synth_calls"] += 1
    STATS["chars"][voice["tier"]] = STATS["chars"].get(voice["tier"], 0) + len(text)
    audio = base64.b64decode(r["Audio"])
    if voice["timing"] == "native":
        subs = r.get("Subtitles") or []
        ref = [(s.get("Text") or "", int(s.get("BeginTime") or 0), int(s.get("EndTime") or 0)) for s in subs]
        dur = max([e for _, _, e in ref] or [0]) + 200
        return audio, align(text, ref, dur), _mp3_ms(audio) or dur
    # 超自然音色：用一句话识别对齐
    dur = _mp3_ms(audio)
    try:
        rr = await tencent.call("asr", "SentenceRecognition", "2019-06-14", {
            "EngSerViceType": "16k_en", "SourceType": 1, "VoiceFormat": "mp3", "Data": base64.b64encode(audio).decode(),
            "DataLen": len(audio), "WordInfo": 1, "FilterPunc": 0}, timeout=20)
        ref = [(w.get("Word") or "", int(w.get("StartTime") or 0), int(w.get("EndTime") or 0)) for w in rr.get("WordList") or []]
        dur = dur or int(rr.get("AudioDuration") or 0)
        STATS["align_asr"] += 1
        return audio, align(text, ref, dur), dur
    except tencent.TencentError as e:
        STATS["align_estimate"] += 1
        print(f"[tts] align by ASR failed, estimating: {e.code} {e.message[:100]}", flush=True)
        return audio, estimate(text, dur or len(text) * 60), dur


def _mp3_ms(b: bytes) -> int:
    """粗略读取 MP3（CBR/VBR 都按帧累计）的时长。"""
    i, n, total = 0, len(b), 0.0
    if b[:3] == b"ID3" and n > 10:
        i = 10 + ((b[6] & 0x7f) << 21 | (b[7] & 0x7f) << 14 | (b[8] & 0x7f) << 7 | (b[9] & 0x7f))
    rates = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
    br1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
    br2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
    while i + 4 <= n:
        if b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
            i += 1
            continue
        ver = (b[i + 1] >> 3) & 3
        bri = (b[i + 2] >> 4) & 15
        sri = (b[i + 2] >> 2) & 3
        pad = (b[i + 2] >> 1) & 1
        if ver == 1 or bri in (0, 15) or sri == 3:
            i += 1
            continue
        sr = rates[ver][sri]
        br = (br1 if ver == 3 else br2)[bri] * 1000
        spf = 1152 if ver == 3 else 576
        size = (144 if ver == 3 else 72) * br // sr + pad
        if size <= 0:
            break
        total += spf / sr
        i += size
    return round(total * 1000)


def engine_of(voice_id: str):
    return "edge" if voice_id in EDGE_IDS else "tencent" if voice_id in BY_ID else None


async def synth(text: str, voice_id: str) -> dict:
    eng = engine_of(voice_id)
    if not eng:
        raise TTSError("不支持的音色，请在播放设置里重新选择")
    voice = BY_ID.get(voice_id)
    if eng == "tencent" and not tencent.enabled():
        raise TTSError("服务器没有配置腾讯云密钥，无法用腾讯云合成语音")
    if eng == "edge" and EDGE["ok"] is False:
        raise TTSError("微软 edge-tts 当前不可用，请在播放设置里把发音来源换成腾讯云")
    key = hashlib.sha1(f"{'edge' if voice_id in EDGE_IDS else 'tc'}|{voice_id}|{text}".encode()).hexdigest()
    if key in _mem:
        _mem.move_to_end(key)
        STATS["cache_mem"] += 1
        return _mem[key]
    p = CACHE_DIR / f"{key}.json"
    if p.exists():
        try:
            res = json.loads(p.read_text())
            STATS["cache_disk"] += 1
            _remember(key, res)
            return res
        except Exception:  # noqa: BLE001
            pass
    try:
        if eng == "edge":
            res = await _edge_synth(text, voice_id)
        else:
            audio, words, offset = bytearray(), [], 0
            for a, w, d in await asyncio.gather(*[_piece(piece, voice) for piece in _split(text)]):
                audio.extend(a)
                words.extend({"t": x["t"] + offset, "d": x["d"], "w": x["w"]} for x in w)
                offset += d
            res = {"audio": base64.b64encode(bytes(audio)).decode(), "words": words}
    except TTSError as e:
        STATS["errors"] += 1
        STATS["last_error"], STATS["last_error_at"] = str(e), time.time()
        raise
    _remember(key, res)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(res))
    except Exception:  # noqa: BLE001
        pass
    return res


def _remember(key: str, res: dict):
    _mem[key] = res
    if len(_mem) > CACHE_SIZE:
        _mem.popitem(last=False)


# ---------------- 微软 edge-tts ----------------
async def _edge_once(text: str, voice: str) -> dict:
    comm = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    audio, words = bytearray(), []
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":  # offset / duration 单位是 100 纳秒
            words.append({"t": round(chunk["offset"] / 10000), "d": round(chunk["duration"] / 10000), "w": chunk["text"]})
    if not audio:
        raise RuntimeError("empty audio")
    return {"audio": base64.b64encode(bytes(audio)).decode(), "words": words}


async def _edge_synth(text: str, voice: str) -> dict:
    last = None
    for attempt in range(2):  # 微软接口偶发断连，重试一次
        try:
            res = await asyncio.wait_for(_edge_once(text, voice), timeout=30)
            STATS["edge_calls"] += 1
            if EDGE["ok"] is not True:
                EDGE.update(ok=True, error=None, checked_at=time.time())
            EDGE["last_ok_at"] = time.time()
            return res
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[tts] edge attempt {attempt + 1} failed: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(0.5)
    # 连续失败：立刻再做一次可用性检测，不可用就让前端禁用
    asyncio.get_event_loop().create_task(check_edge())
    raise TTSError(f"微软 edge-tts 合成失败：{type(last).__name__}，可以在播放设置里把发音来源换成腾讯云")


async def check_edge() -> bool:
    """合成一句短语，看 edge-tts 能不能用。"""
    t0 = time.time()
    try:
        await asyncio.wait_for(_edge_once("Hello, this is a quick check.", EDGE_DEFAULT[0]), timeout=20)
        EDGE.update(ok=True, error=None, latency_ms=round((time.time() - t0) * 1000), last_ok_at=time.time())
    except Exception as e:  # noqa: BLE001
        EDGE.update(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}", latency_ms=None)
    EDGE["checked_at"] = time.time()
    try:
        from ops import event
        event("edge_check", f"微软 edge-tts {'可用' if EDGE['ok'] else '不可用：' + (EDGE['error'] or '')}"
              + (f"（{EDGE['latency_ms']} ms）" if EDGE["ok"] else ""), "info" if EDGE["ok"] else "warn")
    except Exception:  # noqa: BLE001
        pass
    return EDGE["ok"]


async def edge_watch():
    """服务启动时检测一次，之后每 30 分钟复查。"""
    while True:
        await check_edge()
        await asyncio.sleep(EDGE_RECHECK)


def status() -> dict:
    return {
        "default_engine": DEFAULT_ENGINE,
        "engines": {
            "edge": {"available": EDGE["ok"], "checking": EDGE["ok"] is None, "error": EDGE["error"],
                     "checked_at": EDGE["checked_at"], "label": "微软 edge-tts（免费，新西兰 / 英 / 澳 / 美口音）",
                     "voices": EDGE_VOICES, "default": list(EDGE_DEFAULT)},
            "tencent": {"available": tencent.enabled(), "checking": False,
                        "error": None if tencent.enabled() else "服务器没有配置腾讯云密钥",
                        "label": "腾讯云（国内稳定，按字符计费）",
                        "voices": [{"id": v["id"], "label": v["label"], "word_timing": v["timing"]} for v in VOICES],
                        "default": list(TC_DEFAULT)},
        },
    }
