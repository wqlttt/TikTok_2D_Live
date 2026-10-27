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

import json
import os
import re
import subprocess
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
TTS_BACKEND = env("TTS_BACKEND", "local").lower()   # local(本地 say) | vidu(云端对口型)
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


def say_tts(text, voice):
    """本地 TTS：调用 macOS `say`，直接输出 WAV。"""
    os.makedirs("audio", exist_ok=True)
    out = os.path.join("audio", f"reply_{int(time.time() * 1000)}.wav")
    subprocess.run(
        ["say", "-v", voice, "-o", out,
         "--file-format=WAVE", "--data-format=LEI16@22050", text],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def pick_say_voice(content):
    """本地模式：按弹幕关键词选择 macOS `say` 音色，返回 (voice, 标签)。"""
    default = env("SAY_VOICE", "Tingting")
    raw = env("SAY_VOICE_MAP", "")
    mapping = {}
    if raw:
        try:
            mapping = json.loads(raw)
        except Exception:
            mapping = {}
    for label in sorted(mapping, key=len, reverse=True):
        if label and label in content:
            return mapping[label], label
    return default, "默认"


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
    say_voice = say_voice or env("SAY_VOICE", "Tingting")
    t0 = time.time()
    wav_path = say_tts(strip_emoji(reply), say_voice)
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

        if TTS_BACKEND == "local":
            say_voice, voice_label = pick_say_voice(content)
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
            local_path = download(video_url, "output")
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


_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, "avatar.html"), encoding="utf-8") as _f:
    AVATAR_HTML = _f.read()
LIVE2D_DIR = os.path.join(_here, "live2d")


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
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/avatar"):
            return self._send(200, AVATAR_HTML, "text/html; charset=utf-8")
        if self.path == "/api/state":
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
        if self.path == "/api/danmaku":
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, {"error": "bad json"})
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


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"数字人桥已启动：http://{HOST}:{PORT}")
    print(f"  - 虚拟人页面： http://{HOST}:{PORT}/")
    print(f"  - 收弹幕：     POST http://{HOST}:{PORT}/api/danmaku")
    print(f"  - 查状态：     GET  http://{HOST}:{PORT}/api/state")
    print(f"  - 本地音频：   GET  http://{HOST}:{PORT}/audio")
    print(f"  渲染后端 TTS_BACKEND = {TTS_BACKEND}（local=本地 say / vidu=云端对口型）")
    print("提示：开播前请确认 .env 已填 DeepSeek Key。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
