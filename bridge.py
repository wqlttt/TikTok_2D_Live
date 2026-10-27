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


def process_one(item):
    """在后台线程执行完整链路（local 或 vidu 两种渲染后端）。"""
    global LATEST, LATEST_AUDIO
    user, content = item.get("user", ""), item.get("content", "")
    with LOCK:
        LATEST.update(status="processing", user=user, content=content,
                      reply="", voice="", video_url="", local_path="", error="",
                      updated_at=time.strftime("%H:%M:%S"))
    timings = {}
    try:
        t0 = time.time()
        reply = deepseek_reply(content, nick=user, history=list(HISTORY))
        timings["deepseek"] = round(time.time() - t0, 2)
        HISTORY.append({"role": "user", "content": f"弹幕：{content}"})
        HISTORY.append({"role": "assistant", "content": reply})

        if TTS_BACKEND == "local":
            say_voice, voice_label = pick_say_voice(content)
            t0 = time.time()
            wav_path = say_tts(reply, say_voice)
            timings["tts"] = round(time.time() - t0, 2)
            with LOCK:
                LATEST_AUDIO = wav_path
                LATEST.update(status="done", reply=reply, voice=voice_label,
                              audio_url="", task_id="", video_url="",
                              local_path=wav_path, timings=timings,
                              error="", updated_at=time.strftime("%H:%M:%S"))
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
    except Exception as e:  # noqa: BLE001
        with LOCK:
            LATEST.update(status="error", error=str(e), timings=timings,
                          updated_at=time.strftime("%H:%M:%S"))
        traceback.print_exc()


def worker():
    global _last_run, _last_content
    while True:
        with LOCK:
            item = QUEUE.popleft() if QUEUE else None
        if item is None:
            time.sleep(0.5)
            continue
        process_one(item)
        _last_run = time.time()
        _last_content = item.get("content", "")


def enqueue(user, content):
    global _last_run, _last_content
    now = time.time()
    if now - _last_run < MIN_INTERVAL:
        with LOCK:
            LATEST.update(status="done", reply="(限流) 回得太快啦，稍等～")
        return False
    if content and content == _last_content:
        return False
    with LOCK:
        if len(QUEUE) >= MAX_QUEUE:
            return False
        QUEUE.append({"user": user, "content": content})
        LATEST.update(status="queued", user=user, content=content,
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
                return self._send(200, dict(LATEST))
        if self.path.startswith("/audio"):
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
            content = (body.get("content") or "").strip()
            # 兼容两种来源：手动 curl 用 user；赛博小雅 reader 用 nick
            user = (body.get("user") or body.get("nick") or "").strip()
            if not content:
                return self._send(400, {"error": "content required"})
            ok = enqueue(user, content)
            # 兼容 reader.py 期望的 queued/depth 字段
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
