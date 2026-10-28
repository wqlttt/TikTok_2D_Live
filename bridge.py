#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小可运行的本地「数字人桥」服务（仅用标准库，conda `usually` 环境零新依赖）。

功能：
  POST /api/danmaku   收弹幕 -> 入队 -> 后台走链路(DeepSeek -> TTS -> Vidu 对口型)
  GET  /api/state     查最新一条处理结果（含各阶段耗时）
  GET  /video         播放最新渲染视频文件
  GET  /              预览页（自动轮询并播放最新视频）

启动：
  conda activate usually
  cp .env.example .env    # 填 DEEPSEEK_API_KEY / VIDU_API_KEY / VIDU_TTS_VOICE_ID / VIDU_AVATAR_VIDEO_URL
  python bridge.py

依赖：verify_pipeline.py 同目录；requests + python-dotenv（usually 环境已装）。
"""

import hmac
import json
import os
import re
import socket
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# 跨平台 TTS：piper / macOS say / Windows sapi，按 TTS_BACKEND 或平台自动选择
from tts_backend import (
    BASE_DIR,
    configure_console,
    pick_local_voice,
    resolve_backend,
    synthesize as tts_synthesize,
    warmup as tts_warmup,
)

# 复用验证脚本里的链路函数
from verify_pipeline import (
    download,
    deepseek_reply,
    env,
    pick_voice,
    vidu_lipsync_submit,
    vidu_lipsync_wait,
    vidu_tts,
)

HOST = env("BRIDGE_HOST", "127.0.0.1")
PORT = int(env("BRIDGE_PORT", "8000") or 8000)
MIN_INTERVAL = float(env("BRIDGE_MIN_INTERVAL", "3") or 3)   # 两条回复最小间隔(秒)
MAX_QUEUE = int(env("BRIDGE_MAX_QUEUE", "20") or 20)
MAX_BODY = int(env("BRIDGE_MAX_BODY", "8192") or 8192)       # 请求体上限(字节)

# BRIDGE_TOKEN 是「只读视图令牌」：只保护 GET（页面 / 状态 / 音频 / 视频）。
# 写入口 /api/danmaku 只允许回环访问，因此令牌永远不会成为可写凭据——
# 即使它随 ?token= 出现在 URL 里、被分享出去，最坏后果也只是「别人看到你的
# 弹幕」，而不是「别人替你烧 API 额度」。
BRIDGE_TOKEN = env("BRIDGE_TOKEN", "")
TOKEN_B = BRIDGE_TOKEN.encode("utf-8") if BRIDGE_TOKEN else b""
MIN_TOKEN_LEN = 16
ALLOW_UNAUTHENTICATED = env("BRIDGE_ALLOW_UNAUTHENTICATED", "").lower() in (
    "1", "true", "yes", "on")

# ---- 绑定与访问控制 ----
# 注意：Host 白名单只防 DNS rebinding（攻击者域名解析到 127.0.0.1 后借浏览器
# 打本地服务），**它不构成鉴权**——Host 头是客户端自填的，攻击者直接写
# `Host: 127.0.0.1:8000` 就能过。真正的屏障是「写只允许回环」。
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
BOUND_TO_LOOPBACK = HOST in LOOPBACK_HOSTS or HOST == ""
ALLOWED_HOSTS = set(LOOPBACK_HOSTS)
if HOST and HOST not in ("0.0.0.0", "::"):
    ALLOWED_HOSTS.add(HOST)
# 只有显式暴露到局域网时才接受本机网卡 IP，避免「已防护」的错觉
LOCAL_IPS = set()
if not BOUND_TO_LOOPBACK:
    try:
        LOCAL_IPS = set(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:  # 离线或网络异常时不阻断启动
        LOCAL_IPS = set()

LOCK = threading.Lock()
QUEUE = deque()
LATEST = {
    "status": "idle",       # idle | queued | processing | done | error
    "user": "", "content": "", "reply": "", "voice": "",
    "audio_url": "", "task_id": "", "video_url": "", "local_path": "",
    "timings": {}, "error": "", "updated_at": time.strftime("%H:%M:%S"),
}
_last_run = 0.0
_last_content = ""
LATEST_AUDIO = ""          # 本地 TTS 产出的最新 wav 路径
try:
    # piper(本地) / say(macOS) / sapi(Windows) / vidu(云端对口型) / auto(按平台探测)
    TTS_BACKEND = resolve_backend()
except (ValueError, RuntimeError) as _e:
    raise SystemExit(f"[配置错误] TTS_BACKEND 解析失败：{_e}")
HISTORY = deque(maxlen=int(env("HISTORY_TURNS", "8") or 8))   # 记忆最近 N 轮对话，让回复能接上下文
USER_MEMORY = {}           # 昵称 -> deque([(弹幕, 回复), ...]) 按人记忆，提升用户亲和性
REPLIES = deque(maxlen=30) # 已完成的回复队列（供前端排队播放，避免下一条覆盖上一条）
REPLY_SEQ = 0
REPLY_AUDIO = {}           # seq -> 音频文件路径
STARTED_AT = time.time()   # 桥启动时间(epoch)
LAST_DANMAKU_TIME = None   # 上次收到弹幕的时间戳(epoch)，用于前端"是否在线"提示
MODELS = ["Hiyori", "Haru", "Mao", "Natori", "Ren", "Rice", "Mark", "Wanko"]
CURRENT_MODEL = "Hiyori"   # 当前角色，弹幕可切换
GIFT_LOG = deque(maxlen=10) # 最近礼物事件（前端特效）


def _gift_board_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "gift_board.json")


def _load_gift_board():
    try:
        with open(_gift_board_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_gift_board():
    try:
        with open(_gift_board_path(), "w", encoding="utf-8") as f:
            json.dump(GIFT_BOARD, f, ensure_ascii=False)
    except Exception:
        pass


GIFT_BOARD = _load_gift_board()   # 送礼榜单：从文件恢复，跨重启累计

MAX_AUDIO_KEEP = int(env("MAX_AUDIO_KEEP", "50") or 50)   # 最多保留最近 N 条音频文件


def _cleanup_audio():
    """删除超出保留范围的旧音频文件，防止磁盘无限累积。"""
    if REPLY_SEQ <= MAX_AUDIO_KEEP:
        return
    cutoff = REPLY_SEQ - MAX_AUDIO_KEEP
    for seq in list(REPLY_AUDIO):
        if seq <= cutoff:
            p = REPLY_AUDIO.pop(seq, None)
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"      # 主要 emoji 区
    "\U00002600-\U000027BF"       # 杂项符号 + 装饰符号
    "\U0001F1E6-\U0001F1FF"       # 旗帜
    "\U00002B00-\U00002BFF"       # 部分符号
    "\uFE0F\u200D"                # 变体选择符 + 零宽连接符
    "]+"
)


def strip_emoji(text):
    """去掉 emoji（气泡里仍显示，但 TTS 念 emoji 很奇怪，这里只影响朗读文本）。"""
    return _EMOJI_RE.sub("", text).strip()


BLOCK_WORDS = [w.strip() for w in env("BLOCK_WORDS", "").split(",") if w.strip()]


def filter_reply(text):
    """本地敏感词兜底过滤（DeepSeek 自审之外的保险）。"""
    for w in BLOCK_WORDS:
        if w:
            text = text.replace(w, "*" * len(w))
    return text


def user_context(nick):
    """按昵称取该观众最近几轮对话，让数字人记得「上次和这个人聊过什么」。"""
    mem = USER_MEMORY.get(nick)
    if not mem:
        return ""
    parts = []
    for c, r in mem:
        parts.append(f"他之前说「{c}」，你之前回「{r}」")
    return "；".join(parts)


def try_switch_model(content):
    """检测换角色命令：命中返回新模型名，否则 None。"""
    global CURRENT_MODEL
    text = (content or "").strip()
    if not text:
        return None
    # 明确"换/切"命令 → 轮换下一个
    if text in ("换角色", "切换角色", "换人", "换皮肤", "换形象", "换模型",
                "换个角色", "换个形象", "下一个", "下一位"):
        i = MODELS.index(CURRENT_MODEL)
        CURRENT_MODEL = MODELS[(i + 1) % len(MODELS)]
        return CURRENT_MODEL
    # 含"换/切"关键词 + 角色名 → 切到指定
    if ("换" in text or "切" in text):
        for model in MODELS:
            if model.lower() in text.lower():
                if model != CURRENT_MODEL:
                    CURRENT_MODEL = model
                    return model
    return None


def finish_reply(reply, voice_label, user, content, timings, say_voice=None):
    """合成语音 + 更新状态 + 入回复队列（chat/gift/speak 共用）。"""
    global LATEST, LATEST_AUDIO, REPLY_SEQ
    reply = filter_reply(reply)   # 本地敏感词兜底
    text = strip_emoji(reply)
    t0 = time.time()
    # 后端选择统一由 tts_backend 负责（piper / say / sapi），这里不再分支，
    # 否则新增后端时容易漏改某条分支而落到 macOS 专属的 say 上。
    #
    # vidu 模式例外：进场播报、礼物感谢、换角色提示这类短句直接走 finish_reply，
    # 不经过 process_one 的 vidu 分支；而 tts_backend 里没有实现 vidu（云端
    # 对口型在 process_one 里单独处理）。沿用改造前的行为：vidu 模式下短播报
    # 用平台原生 TTS 兜底，否则这些播报会全部报错。
    backend = resolve_backend("auto") if TTS_BACKEND == "vidu" else TTS_BACKEND
    wav_path = tts_synthesize(text, say_voice or None, backend=backend)
    timings["tts"] = round(time.time() - t0, 2)
    with LOCK:
        LATEST_AUDIO = wav_path
        LATEST.update(status="done", reply=reply, voice=voice_label,
                      audio_url="", task_id="", video_url="",
                      local_path=wav_path, timings=timings,
                      error="", updated_at=time.strftime("%H:%M:%S"))
    REPLY_SEQ += 1
    REPLIES.append({"seq": REPLY_SEQ, "user": user, "content": content,
                    "reply": reply, "voice": voice_label})
    REPLY_AUDIO[REPLY_SEQ] = wav_path
    _cleanup_audio()


def process_one(item):
    """在后台线程执行完整链路。item 含 kind: chat(弹幕)/gift(礼物)/speak(直接念)。"""
    global LATEST, LATEST_AUDIO, REPLY_SEQ
    kind = item.get("kind", "chat")
    user = item.get("user", "")
    content = item.get("content") or item.get("text") or ""
    with LOCK:
        LATEST.update(status="processing", user=user, content=content,
                      reply="", voice="", video_url="", local_path="", error="",
                      updated_at=time.strftime("%H:%M:%S"))
    timings = {}

    # 换角色命令（仅 chat）
    if kind == "chat":
        new_model = try_switch_model(content)
        if new_model:
            finish_reply(f"好嘞，换到「{new_model}」啦～", "默认", user, content, timings)
            return

    try:
        # 直接念（进场/关注/点赞/守护/醒目留言等）
        if kind == "speak":
            finish_reply(item.get("text", ""), "默认", user, content, timings)
            return

        # 礼物：累计榜单 + 记录最近礼物
        if kind == "gift":
            GIFT_BOARD[user] = GIFT_BOARD.get(user, 0) + item.get("gift_value", 0)
            GIFT_LOG.append({"nick": user, "item": item.get("gift_item"),
                             "num": item.get("gift_num"), "value": item.get("gift_value")})
            _save_gift_board()

        # DeepSeek 回复（chat 和 gift 都走这里）
        t0 = time.time()
        reply = deepseek_reply(content, nick=user, history=list(HISTORY),
                               user_context=user_context(user))
        timings["deepseek"] = round(time.time() - t0, 2)
        HISTORY.append({"role": "user", "content": f"弹幕：{content}"})
        HISTORY.append({"role": "assistant", "content": reply})
        if user:
            USER_MEMORY.setdefault(user, deque(maxlen=3)).append((content, reply))

        if TTS_BACKEND != "vidu":
            say_voice, voice_label = pick_local_voice(content)
            finish_reply(reply, voice_label, user, content, timings, say_voice=say_voice)
        else:
            voice_id, voice_label = pick_voice(content)
            t0 = time.time()
            audio_url = vidu_tts(reply, voice_id)
            timings["tts"] = round(time.time() - t0, 2)

            avatar_video = env("VIDU_AVATAR_VIDEO_URL")
            if not avatar_video:
                raise RuntimeError("缺少 VIDU_AVATAR_VIDEO_URL")

            t0 = time.time()
            task_id = vidu_lipsync_submit(avatar_video, audio_url)
            timings["submit"] = round(time.time() - t0, 2)

            t0 = time.time()
            video_url = vidu_lipsync_wait(task_id)
            timings["render_wait"] = round(time.time() - t0, 2)

            t0 = time.time()
            local_path = download(video_url, os.path.join(BASE_DIR, "output"))
            timings["download"] = round(time.time() - t0, 2)

            with LOCK:
                LATEST.update(status="done", reply=reply, voice=voice_label,
                              audio_url=audio_url, task_id=task_id, video_url=video_url,
                              local_path=local_path, timings=timings,
                              error="", updated_at=time.strftime("%H:%M:%S"))
            REPLY_SEQ += 1
            REPLIES.append({"seq": REPLY_SEQ, "user": user, "content": content,
                            "reply": reply, "voice": voice_label})
    except Exception as e:  # noqa: BLE001
        with LOCK:
            LATEST.update(status="error", error=str(e), timings=timings,
                          updated_at=time.strftime("%H:%M:%S"))
        traceback.print_exc()


def merge_adjacent_gifts(item):
    """把队列里紧邻的同昵称礼物合并成一条，避免连击重复感谢。"""
    nick = item.get("user", "")
    total_num = item.get("gift_num", 1)
    total_value = item.get("gift_value", 0)
    parts = [(item.get("gift_item", "礼物"), item.get("gift_num", 1))]
    with LOCK:
        while QUEUE and QUEUE[0].get("kind") == "gift" and QUEUE[0].get("user") == nick:
            g = QUEUE.popleft()
            total_num += g.get("gift_num", 1)
            total_value += g.get("gift_value", 0)
            parts.append((g.get("gift_item", "礼物"), g.get("gift_num", 1)))
    if len(parts) > 1:
        item["gift_num"] = total_num
        item["gift_value"] = total_value
        summary = "、".join(f"{n}个「{i}」" for i, n in parts)
        item["content"] = f"观众「{nick}」连送了 {summary}"
    return item


def worker():
    global _last_run, _last_content
    while True:
        with LOCK:
            item = QUEUE.popleft() if QUEUE else None
        if item is None:
            time.sleep(0.5)
            continue
        if item.get("kind") == "gift":
            item = merge_adjacent_gifts(item)
        process_one(item)
        _last_run = time.time()
        _last_content = item.get("content") or item.get("text") or ""


def enqueue(item):
    global _last_run, _last_content, LAST_DANMAKU_TIME
    now = time.time()
    if now - _last_run < MIN_INTERVAL:
        with LOCK:
            LATEST.update(status="done", reply="(限流) 回得太快啦，稍等～")
        return False
    key = item.get("content") or item.get("text") or ""
    if key and key == _last_content:
        return False
    with LOCK:
        if len(QUEUE) >= MAX_QUEUE:
            return False
        if item.get("priority") == "vip":
            QUEUE.appendleft(item)   # 打赏过的观众弹幕插队
        else:
            QUEUE.append(item)
        LAST_DANMAKU_TIME = now
        LATEST.update(status="queued", user=item.get("user", ""), content=key,
                      updated_at=time.strftime("%H:%M:%S"))
    return True


with open(os.path.join(BASE_DIR, "avatar.html"), encoding="utf-8") as _f:
    AVATAR_HTML = _f.read()
LIVE2D_DIR = os.path.join(BASE_DIR, "live2d")


PREVIEW_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>数字人预览</title>
<style>
  body{background:#111;color:#eee;font-family:-apple-system,sans-serif;margin:0}
  video{width:100%;max-height:72vh;background:#000}
  #box{max-width:960px;margin:0 auto;padding:16px}
  #reply{font-size:22px;margin:12px 0;color:#9ef}
  .muted{color:#888;font-size:13px}
  #state{font-size:13px;color:#8a8}
</style></head>
<body><div id="box">
  <video id="v" autoplay muted controls loop></video>
  <div id="reply">等待弹幕…</div>
  <div id="state" class="muted">status: idle</div>
  <div class="muted">POST /api/danmaku 收弹幕 · GET /api/state 查状态</div>
</div>
<script>
  let last = "";
  async function poll(){
    try{
      const r = await fetch("/api/state");
      const s = await r.json();
      document.getElementById("state").textContent =
        `status: ${s.status}  ${s.error ? "· 错误: "+s.error : ""}  ` +
        (s.timings && s.timings.deepseek ? JSON.stringify(s.timings) : "");
      if(s.status==="processing"){ document.getElementById("reply").textContent =
        `弹幕: ${s.content}  → 生成中…`; }
      else if(s.status==="done" && s.reply){
        document.getElementById("reply").textContent =
          `回复: ${s.reply}` + (s.voice ? `  [音色: ${s.voice}]` : "");
      }
      if(s.local_path && s.local_path !== last){
        last = s.local_path;
        const v = document.getElementById("v");
        v.src = "/video?t=" + Date.now();
        v.muted = false;
        v.play().catch(()=>{});
      }
    }catch(e){}
  }
  setInterval(poll, 2000); poll();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 页面 URL 可能带 ?token=，必须阻止它随 Referer 泄漏到任何外部资源。
        # 用响应头而不是 <meta name="referrer">：meta 只对其后发起的请求生效，
        # 有人把远程资源挪到 meta 之前就会静默失效；响应头覆盖所有响应且改不掉。
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 访问控制 ----------

    def _host_ok(self):
        """校验 Host 头（阻断 DNS rebinding）。"""
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return True                    # HTTP/1.0 客户端不带 Host，放行避免误伤
        if raw.startswith("["):            # [::1] 或 [::1]:8000 —— 不能按冒号切
            end = raw.find("]")
            host = raw[1:end] if end > 0 else raw
        else:
            host = raw.rsplit(":", 1)[0]
        return host.strip().lower() in ALLOWED_HOSTS or host.strip() in LOCAL_IPS

    def _is_loopback_client(self):
        """请求是否来自本机（本机转发器与浏览器无需携带令牌）。"""
        ip = (self.client_address[0] if self.client_address else "") or ""
        return (ip.startswith("127.") or ip in ("::1", "0:0:0:0:0:0:0:1")
                or ip == "::ffff:127.0.0.1")

    def _token_ok(self, allow_query=False):
        """只读视图令牌校验。

        回环客户端免校验（本机转发器与 OBS 浏览器源照常工作）；非回环客户端
        必须带对令牌。只有页面入口（GET /）允许用 ?token= 传令牌——那是用户
        手输的入口，浏览器无法在地址栏带自定义头。
        """
        if not BRIDGE_TOKEN:
            return True          # 仅在回环绑定或显式 ALLOW_UNAUTHENTICATED 下出现
        if self._is_loopback_client():
            return True
        got = self.headers.get("X-Bridge-Token") or ""
        if not got and allow_query:
            try:
                got = (parse_qs(urlsplit(self.path).query).get("token") or [""])[0]
            except ValueError:
                got = ""
        if not got:
            return False
        return hmac.compare_digest(got.encode("utf-8"), TOKEN_B)

    def _origin_ok(self):
        """校验 Origin（阻断跨站 POST）。非浏览器客户端不带 Origin，放行。"""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        try:
            parts = urlsplit(origin)
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        if host not in ALLOWED_HOSTS and host not in LOCAL_IPS:
            return False
        # 端口也要比：否则任意本地端口的页面都能对本服务发起跨站请求
        try:
            port = parts.port
        except ValueError:
            return False
        if port is None:
            return PORT in (80, 443)
        return port == PORT

    def _deny(self):
        return self._send(403, {"error": "forbidden: host/origin not allowed"})

    def do_GET(self):
        if not self._host_ok():
            return self._deny()
        # 先剥掉 query 再匹配路径：否则带 ?token= 的入口会被后面所有分支漏掉
        path = self.path.split("?", 1)[0]
        if path in ("/", "/avatar"):
            if not self._token_ok(allow_query=True):
                return self._send(401, {"error": "bad token"})
            return self._send(200, AVATAR_HTML, "text/html; charset=utf-8")
        if path == "/api/state":
            if not self._token_ok():
                return self._send(401, {"error": "bad token"})
            with LOCK:
                d = dict(LATEST)
                d["replies"] = list(REPLIES)
                d["last_danmaku_at"] = LAST_DANMAKU_TIME
                d["started_at"] = STARTED_AT
                d["model"] = CURRENT_MODEL
                d["gift_board"] = sorted(GIFT_BOARD.items(), key=lambda x: -x[1])[:10]
                d["gift_log"] = list(GIFT_LOG)
                return self._send(200, d)
        if self.path.startswith("/audio"):
            if not self._token_ok():
                return self._send(401, {"error": "bad token"})
            p = None
            if "seq=" in self.path:
                try:
                    seq = int(self.path.split("seq=")[1].split("&")[0])
                    p = REPLY_AUDIO.get(seq)
                except Exception:
                    p = None
            if not p:
                with LOCK:
                    p = LATEST_AUDIO
            if p and os.path.exists(p):
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "audio/wav")
            return self._send(404, {"error": "no audio yet"})
        if self.path.startswith("/video"):
            if not self._token_ok():
                return self._send(401, {"error": "bad token"})
            with LOCK:
                p = LATEST.get("local_path", "")
            if p and os.path.exists(p):
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "video/mp4")
            return self._send(404, {"error": "no video yet"})
        if self.path.startswith("/live2d/"):
            rel = self.path[len("/live2d/"):].split("?")[0]
            full = os.path.realpath(os.path.join(LIVE2D_DIR, rel))
            if not full.startswith(os.path.realpath(LIVE2D_DIR) + os.sep):
                return self._send(403, {"error": "forbidden"})
            if os.path.isfile(full):
                if full.endswith(".png"):
                    ct = "image/png"
                elif full.endswith(".js"):
                    ct = "application/javascript"
                elif full.endswith(".json"):
                    ct = "application/json"
                else:
                    ct = "application/octet-stream"
                with open(full, "rb") as f:
                    return self._send(200, f.read(), ct)
            return self._send(404, {"error": "not found"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._deny()
        if self.path == "/api/danmaku":
            # 写入口只允许本机：转发器 cyber-xiaoya-danmaku 永远连
            # http://127.0.0.1:8000，远程写没有真实需求。
            # 返回 403 而不是 401 —— 避免转发器把「远程写被禁」误判成令牌过期
            # 去反复重试；同时明确区分「没权限做这件事」与「令牌不对」。
            if not self._is_loopback_client():
                return self._send(403, {"error": "remote write disabled"})
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n <= 0:
                    return self._send(400, {"error": "empty body"})
                if n > MAX_BODY:
                    return self._send(413, {"error": "payload too large"})
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, {"error": "bad json"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "json object required"})
            kind = (body.get("kind") or "").strip()
            priority = (body.get("priority") or "normal").strip()
            if kind == "gift":
                nick = (body.get("nick") or body.get("say_nick") or "").strip()
                num = int(body.get("gift_num") or 1)
                itm = (body.get("gift_item") or "礼物").strip()
                item = {
                    "kind": "gift", "user": nick, "priority": priority,
                    "gift_item": itm, "gift_num": num,
                    "gift_value": int(body.get("gift_value") or 0),
                    "content": f"观众「{nick or '神秘人'}」送了 {num} 个「{itm}」",
                }
            elif (body.get("content") or "").strip():
                item = {
                    "kind": "chat", "priority": priority,
                    "user": (body.get("user") or body.get("nick") or "").strip(),
                    "content": (body.get("content") or "").strip(),
                }
            elif (body.get("text") or "").strip():
                item = {"kind": "speak", "user": "", "priority": priority,
                        "text": (body.get("text") or "").strip()}
            else:
                return self._send(400, {"error": "content required"})
            ok = enqueue(item)
            return self._send(200, {"queued": ok, "depth": len(QUEUE),
                                    "status": "queued" if ok else "skipped"})
        return self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def check_auth_config():
    """启动前的鉴权配置检查（失败关闭）。

    默认配置（127.0.0.1）不受影响。一旦绑到非回环地址，必须提供足够长的
    只读令牌，否则拒绝启动——避免用户在不知情的情况下把弹幕流与音频暴露给
    整个局域网。

    即使配了令牌，也要注意：令牌只保护读，写入口已经硬性限定回环。
    """
    if BOUND_TO_LOOPBACK:
        return
    if ALLOW_UNAUTHENTICATED:
        print("[安全警告] BRIDGE_HOST=%s 已暴露到局域网，且未设置只读令牌。" % HOST)
        print("           同网段任何人都能查看弹幕与音频（写入口仍只允许本机）。")
        return
    if not BRIDGE_TOKEN:
        raise SystemExit(
            "[配置错误] BRIDGE_HOST=%s 会把服务暴露到局域网，但 BRIDGE_TOKEN 为空。\n"
            "  请二选一：\n"
            "    1) 设置 BRIDGE_TOKEN=<至少 16 位的随机串>，用 ?token= 打开页面\n"
            "    2) 设 BRIDGE_ALLOW_UNAUTHENTICATED=1 明确接受只读暴露\n"
            "  注：写入口 /api/danmaku 无论如何都只允许本机访问，\n"
            "  所以远程无法替你消耗 API 额度。" % HOST)
    if len(BRIDGE_TOKEN) < MIN_TOKEN_LEN:
        raise SystemExit(
            "[配置错误] BRIDGE_TOKEN 太短（%d 位），暴露到局域网时至少需要 %d 位。"
            % (len(BRIDGE_TOKEN), MIN_TOKEN_LEN))
    if not BRIDGE_TOKEN.isascii():
        # http.server 用 latin-1 解码请求头，而 parse_qs 按 UTF-8 解码 query。
        # 非 ASCII 令牌在这两条路径上会得到不同的字节序列，导致「URL 里能用、
        # 请求头里 401」这种诡异现象。强制 ASCII 后两种编码等价，两条路径天然
        # 一致——不要去分路径 encode，那会留下只在一条路径正确的分支。
        raise SystemExit(
            "[配置错误] BRIDGE_TOKEN 只能包含 ASCII 字符（建议用随机串）。\n"
            "  生成：python -c \"import secrets;print(secrets.token_urlsafe(24))\"")


if __name__ == "__main__":
    configure_console()   # Windows 下输出重定向时不因 GBK 编码崩溃
    check_auth_config()
    tts_warmup(TTS_BACKEND)  # Piper 预热（阻塞几秒，避免与 worker 线程竞争加载模型）
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"数字人桥已启动：http://{HOST}:{PORT}")
    print(f"  - 虚拟人页面： http://{HOST}:{PORT}/")
    print(f"  - 收弹幕：     POST http://{HOST}:{PORT}/api/danmaku")
    print(f"  - 查状态：     GET  http://{HOST}:{PORT}/api/state")
    print(f"  - 本地音频：   GET  http://{HOST}:{PORT}/audio")
    print(f"  TTS 后端 = {TTS_BACKEND}"
          f"（piper=本地模型 / say=macOS / sapi=Windows / vidu=云端对口型）")
    print(f"  工作目录 = {BASE_DIR}")
    print("提示：开播前请确认 .env 已填 DeepSeek Key。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
