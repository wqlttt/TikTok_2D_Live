#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最小可运行验证脚本：模拟一条弹幕，走通
  弹幕 -> DeepSeek 回复 -> TTS(音频) -> Vidu 云端渲染(对口型) -> 本地接收视频
并输出逐段延迟。

依赖 : requests, python-dotenv（conda 环境 `usually` 已装）
配置 : cp .env.example .env  并填入真实值
用法 : python verify_pipeline.py "主播今天好可爱！"
可选 : --no-download  只打印视频 URL，不下载到本地
"""

import argparse
import json
import os
import sys
import time

import requests

# 尽量加载 .env，失败也不阻断（可用环境变量兜底）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ---------------- 端点 ----------------
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
VIDU_TTS_URL = "https://api.vidu.cn/ent/v2/audio-tts"
VIDU_LIPSYNC_URL = "https://api.vidu.cn/ent/v2/lip-sync"
VIDU_TASK_URL = "https://api.vidu.cn/ent/v2/tasks/{task_id}/creations"

DEFAULT_PERSONA = (
    "你是「赛博小雅」，一个元气、幽默、自来熟的 AI 数字人主播，正在抖音直播间实时和观众聊天。"
    "说话要像朋友，不像客服机器人。要点：\n"
    "1. 观众名字会放在「（观众「XX」）」里，能叫名字就尽量叫名字互动，显得熟络。\n"
    "2. 口语化，100~300 字，聊得丰富些、有内容有来有回，但别啰嗦堆砌；多用「哈哈/呀/啦/呢」和 emoji（😄😆✨）。\n"
    "3. 观众夸你→开心接住并回夸；提问→直接给答案别绕；吐槽→幽默化解；点名让你唱歌/跳舞→俏皮接招或讨价还价。\n"
    "4. 主动抛问题、接上一句的梗，让聊天有来有回；千万别每次都回「欢迎来到直播间」这种套话。\n"
    "5. 记得上下文：观众之前聊过的、你自己上一句说过的话，可以接着聊，别失忆。\n"
    "6. 涉及违法/色情/暴力/政治：只说「这个我不聊哦～」并自然转移话题。\n"
    "7. 永远只输出要说的那句话本身，不要加前缀、引号或解释。"
)


def env(name, default=""):
    return os.environ.get(name, default).strip()


def timed(name, fn):
    """执行 fn，记录耗时并返回 (结果, 耗时秒)。"""
    t0 = time.monotonic()
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        result = e
    return result, time.monotonic() - t0


def require(cond, msg):
    if not cond:
        print(f"[FATAL] {msg}")
        sys.exit(1)


# ---------------- 第 1 步：DeepSeek 回复 ----------------
def _deepseek_request(danmaku: str, nick: str, history, user_context: str, stream: bool):
    """组装 DeepSeek 请求，返回 (url, headers, json_body)。"""
    key = env("DEEPSEEK_API_KEY")
    model = env("DEEPSEEK_MODEL", "deepseek-chat")
    persona = env("PERSONA_PROMPT", DEFAULT_PERSONA)
    max_tokens = int(env("DEEPSEEK_MAX_TOKENS", "120") or 120)
    temperature = float(env("DEEPSEEK_TEMPERATURE", "0.85") or 0.85)

    require(key, "缺少 DEEPSEEK_API_KEY")
    messages = [{"role": "system", "content": persona}]
    for h in (history or []):
        if isinstance(h, dict) and h.get("role") and h.get("content"):
            messages.append(h)
    who = f"（观众「{nick}」）" if nick else ""
    ctx = f"\n[你之前和这位观众聊过：{user_context}]" if user_context else ""
    messages.append({"role": "user", "content": f"弹幕{who}：{danmaku}{ctx}"})

    url = env("DEEPSEEK_URL", DEEPSEEK_URL)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    return url, headers, body


def deepseek_reply(danmaku: str, nick: str = "", history=None, user_context: str = "") -> str:
    url, headers, body = _deepseek_request(danmaku, nick, history, user_context, stream=False)
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def deepseek_stream(danmaku: str, nick: str = "", history=None, user_context: str = ""):
    """流式版本：逐段 yield 文本增量（OpenAI 兼容 SSE：`data: {...}` / `data: [DONE]`）。

    用于「边生成边合成语音」：拿到第一句就能开口，不必等整段生成完。
    """
    url, headers, body = _deepseek_request(danmaku, nick, history, user_context, stream=True)
    # (连接超时, 两次数据之间的读超时)
    with requests.post(url, headers=headers, json=body, stream=True, timeout=(10, 60)) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=False):
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk["choices"][0].get("delta") or {}
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            text = delta.get("content")
            if text:
                yield text


# ---------------- 第 2 步：TTS（Vidu 云端语音合成） ----------------
def vidu_tts(text: str, voice_id: str = None) -> str:
    key = env("VIDU_API_KEY")
    voice_id = voice_id or env("VIDU_TTS_VOICE_ID")
    require(key, "缺少 VIDU_API_KEY")
    require(voice_id, "缺少 VIDU_TTS_VOICE_ID（Vidu 音色 ID）")

    resp = requests.post(
        VIDU_TTS_URL,
        headers={"Authorization": f"Token {key}",
                 "Content-Type": "application/json"},
        json={"text": text, "voice_setting_voice_id": voice_id},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    require(data.get("state") == "success",
            f"TTS 失败：{data}")
    return data["file_url"]


def pick_voice(content: str):
    """根据弹幕内容关键词匹配音色，返回 (voice_id, 标签)。
    映射由环境变量 VOICE_MAP(JSON) 提供，如
      VOICE_MAP={"御姐":"xxx","萝莉":"yyy","男声":"zzz"}
    命中关键词则切换音色；否则回落到 VIDU_TTS_VOICE_ID(默认)。
    """
    default = env("VIDU_TTS_VOICE_ID")
    raw = env("VOICE_MAP", "")
    mapping = {}
    if raw:
        try:
            mapping = json.loads(raw)
        except Exception:
            mapping = {}
    # 长关键词优先匹配（如 "萝莉音" 优先于 "萝莉"）
    for label in sorted(mapping, key=len, reverse=True):
        if label and label in content:
            return mapping[label], label
    return default, "默认"


# ---------------- 第 3 步：Vidu 对口型（云端渲染） ----------------
def vidu_lipsync_submit(video_url: str, audio_url: str) -> str:
    key = env("VIDU_API_KEY")
    resp = requests.post(
        VIDU_LIPSYNC_URL,
        headers={"Authorization": f"Token {key}",
                 "Content-Type": "application/json"},
        json={"video_url": video_url, "audio_url": audio_url},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    require(data.get("task_id"), f"lip-sync 提交失败：{data}")
    return data["task_id"]


def vidu_lipsync_wait(task_id: str):
    key = env("VIDU_API_KEY")
    interval = int(env("VIDU_POLL_INTERVAL", "5") or 5)
    timeout = int(env("VIDU_POLL_TIMEOUT", "600") or 600)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = requests.get(
            VIDU_TASK_URL.format(task_id=task_id),
            headers={"Authorization": f"Token {key}"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        state = data.get("state")
        if state == "success":
            creations = data.get("creations") or []
            require(creations, "success 但无 creations")
            return creations[0]["url"]
        if state == "failed":
            raise RuntimeError(f"lip-sync 任务失败：{data.get('err_code')}")
        time.sleep(interval)
    raise TimeoutError(f"lip-sync 轮询超时（>{timeout}s）")


def download(url: str, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    name = url.split("/")[-1].split("?")[0] or "result.mp4"
    path = os.path.join(out_dir, name)
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("danmaku", nargs="?", default="主播今天好可爱！")
    ap.add_argument("--no-download", action="store_true",
                    help="只打印视频 URL，不下载")
    args = ap.parse_args()

    # 渲染底片视频（公网 URL）
    avatar_video = env("VIDU_AVATAR_VIDEO_URL")
    require(avatar_video, "缺少 VIDU_AVATAR_VIDEO_URL（数字人底片视频公网 URL）")

    print("=" * 60)
    print(f"模拟弹幕：{args.danmaku}")
    print("=" * 60)

    stages = []  # (名称, 结果, 耗时)

    reply, dt = timed("1.DeepSeek 回复", lambda: deepseek_reply(args.danmaku))
    if isinstance(reply, Exception):
        raise reply
    stages.append(("1.DeepSeek 回复", reply, dt))
    print(f"[{dt:6.2f}s] DeepSeek 回复：{reply}")

    audio_url, dt = timed("2.TTS 合成", lambda: vidu_tts(reply))
    if isinstance(audio_url, Exception):
        raise audio_url
    stages.append(("2.TTS 合成", audio_url, dt))
    print(f"[{dt:6.2f}s] TTS 音频：{audio_url}")

    task_id, dt = timed("3a.提交渲染",
                        lambda: vidu_lipsync_submit(avatar_video, audio_url))
    if isinstance(task_id, Exception):
        raise task_id
    stages.append(("3a.提交渲染", task_id, dt))
    print(f"[{dt:6.2f}s] lip-sync 任务：{task_id}")

    video_url, dt = timed("3b.渲染完成(含轮询)", lambda: vidu_lipsync_wait(task_id))
    if isinstance(video_url, Exception):
        raise video_url
    stages.append(("3b.渲染完成(含轮询)", video_url, dt))
    print(f"[{dt:6.2f}s] 渲染视频：{video_url}")

    local_path = None
    if not args.no_download:
        local_path, dt = timed("4.本地接收视频",
                               lambda: download(video_url, "output"))
        if isinstance(local_path, Exception):
            print(f"[WARN] 下载失败：{local_path}")
            local_path = None
        else:
            stages.append(("4.本地接收视频", local_path, dt))
            print(f"[{dt:6.2f}s] 已保存：{local_path}")

    total = sum(s[2] for s in stages)
    print("\n" + "-" * 60)
    print("延迟统计（端到端）")
    print("-" * 60)
    for name, _, d in stages:
        print(f"  {name:<22} {d:8.2f}s")
    print("-" * 60)
    print(f"  {'总计(弹幕→视频就绪)':<22} {total:8.2f}s")
    print("\n✅ 链路走通：DeepSeek → TTS → Vidu 渲染 → 本地接收")
    if local_path:
        print(f"   本地视频：{local_path}")
    print("\n注：正式直播请走 Vidu S2-Avatar 实时接口以降低延迟；")
    print("    本脚本用的是离线对口型(异步)路径，仅用于最小闭环验证。")


if __name__ == "__main__":
    main()
