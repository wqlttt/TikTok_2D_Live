#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数字人桥冒烟测试（跨平台）。

不需要 DeepSeek API Key —— 用 `kind=speak` 走「直接朗读」分支，
可以完整验证 HTTP -> 队列 -> worker -> TTS -> 回复队列 -> /audio 这一段。

用法：
    python tests/smoke_bridge.py
    python tests/smoke_bridge.py --port 8123 --verbose

退出码：0 全部通过，1 有失败项。
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import wave

try:
    import requests
except ImportError:
    print("需要 requests：python -m pip install -r requirements.txt")
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(ROOT, "bridge.py")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print("[%s] %s%s" % (mark, name, ("  -> " + detail) if detail and not ok else ""))
    return ok


def wait_port(port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def raw_request(port, request_line, host_header=None):
    """发原始 HTTP 请求，绕过 requests 的 URL 归一化（用于路径穿越测试）。"""
    host = host_header or "127.0.0.1:%d" % port
    req = "%s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % (request_line, host)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(req.encode("ascii"))
        chunks = []
        while True:
            try:
                buf = s.recv(4096)
            except OSError:
                break
            if not buf:
                break
            chunks.append(buf)
    return b"".join(chunks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    port = args.port
    base = "http://127.0.0.1:%d" % port

    env = dict(os.environ)
    env.update({
        "BRIDGE_HOST": "127.0.0.1",
        "BRIDGE_PORT": str(port),
        "BRIDGE_MIN_INTERVAL": "0",      # 测试里不要限流
        "DEEPSEEK_API_KEY": "sk-smoke-test-not-a-real-key",
        "TTS_BACKEND": env.get("SMOKE_TTS_BACKEND", "auto"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8:replace",
    })

    print("启动 bridge.py（端口 %d）…" % port)
    proc = subprocess.Popen(
        [sys.executable, BRIDGE],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")

    try:
        if not wait_port(port):
            out = ""
            try:
                out = proc.stdout.read(4000) if proc.stdout else ""
            except Exception:
                pass
            check("服务启动", False, "端口未监听\n" + out)
            return 1
        check("服务启动", True)

        # ---------- 1. 首页 ----------
        r = requests.get(base + "/", timeout=10)
        check("GET / 返回 Live2D 页面",
              r.status_code == 200 and "Live2D" in r.text,
              "status=%d len=%d" % (r.status_code, len(r.text)))

        # ---------- 2. 状态接口 ----------
        r = requests.get(base + "/api/state", timeout=10)
        st = r.json()
        check("GET /api/state 结构正确",
              r.status_code == 200 and "status" in st and "replies" in st,
              json.dumps(st, ensure_ascii=False)[:200])

        # ---------- 3. 投递一条「直接朗读」（不经过 DeepSeek） ----------
        text = "宝子们晚上好呀，这是跨平台测试😆"
        r = requests.post(base + "/api/danmaku",
                          json={"text": text, "kind": "speak"}, timeout=10)
        body = r.json()
        check("POST /api/danmaku 入队", r.status_code == 200 and body.get("queued"),
              json.dumps(body, ensure_ascii=False))

        # ---------- 4. 等 TTS 完成 ----------
        deadline = time.time() + 60
        state = {}
        while time.time() < deadline:
            state = requests.get(base + "/api/state", timeout=10).json()
            if state.get("status") in ("done", "error"):
                break
            time.sleep(0.5)
        check("TTS 链路完成（status=done）",
              state.get("status") == "done",
              "status=%s error=%s" % (state.get("status"), state.get("error")))
        check("回复文本非空", bool(state.get("reply")), repr(state.get("reply"))[:120])
        check("生成了本地 wav", bool(state.get("local_path"))
              and os.path.isfile(state.get("local_path") or ""),
              str(state.get("local_path")))
        if args.verbose:
            print("    timings:", state.get("timings"))

        # ---------- 5. wav 格式 ----------
        seq = (state.get("replies") or [{}])[-1].get("seq")
        if seq:
            r = requests.get(base + "/audio?seq=%d" % seq, timeout=10)
            ok = r.status_code == 200 and r.content[:4] == b"RIFF"
            detail = "status=%d len=%d head=%r" % (r.status_code, len(r.content), r.content[:4])
            if ok:
                path = state.get("local_path")
                try:
                    with wave.open(path, "rb") as w:
                        detail += " %dHz %dch %ds" % (
                            w.getframerate(), w.getnchannels(),
                            round(w.getnframes() / w.getframerate(), 2))
                except Exception as e:
                    detail += " (wave open failed: %s)" % e
            check("GET /audio 返回合法 WAV", ok, detail)
        else:
            check("GET /audio 返回合法 WAV", False, "回复队列里没有 seq")

        # ---------- 6. 错误输入 ----------
        r = requests.post(base + "/api/danmaku", data=b"{not json",
                          headers={"Content-Type": "application/json"}, timeout=10)
        check("非法 JSON -> 400", r.status_code == 400, "status=%d" % r.status_code)

        r = requests.post(base + "/api/danmaku", json={"foo": "bar"}, timeout=10)
        check("缺少 content/text -> 400", r.status_code == 400, "status=%d" % r.status_code)

        r = requests.post(base + "/api/danmaku", json=["a", "b"], timeout=10)
        check("JSON 数组 -> 400", r.status_code == 400, "status=%d" % r.status_code)

        r = requests.post(base + "/api/danmaku", data=b"x" * 9000,
                          headers={"Content-Type": "application/json"}, timeout=10)
        check("超大请求体 -> 413", r.status_code == 413, "status=%d" % r.status_code)

        # ---------- 7. 访问控制 ----------
        r = requests.get(base + "/api/state", timeout=10,
                         headers={"Host": "evil.example.com"})
        check("伪造 Host -> 403", r.status_code == 403, "status=%d" % r.status_code)

        r = requests.post(base + "/api/danmaku", json={"text": "x"}, timeout=10,
                          headers={"Origin": "http://evil.example.com"})
        check("跨站 Origin -> 403", r.status_code == 403, "status=%d" % r.status_code)

        # ---------- 8. 路径穿越（原始 socket，绕过客户端归一化） ----------
        resp = raw_request(port, "GET /live2d/../../bridge.py")
        head = resp.split(b"\r\n", 1)[0]
        check("路径穿越被拒绝", b"403" in head or b"404" in head, head.decode("latin1"))
        check("路径穿越未泄漏源码", b"def do_GET" not in resp,
              "响应中包含 bridge.py 内容！")

        resp = raw_request(port, "GET /live2d/..%2f..%2fbridge.py")
        head = resp.split(b"\r\n", 1)[0]
        check("编码型路径穿越被拒绝", b"403" in head or b"404" in head, head.decode("latin1"))

        # ---------- 9. 未知路径 ----------
        r = requests.get(base + "/no-such-endpoint", timeout=10)
        check("未知路径 -> 404", r.status_code == 404, "status=%d" % r.status_code)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "-" * 56)
    print("通过 %d / %d" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("失败项：")
        for name, _ok, detail in failed:
            print("  - %s: %s" % (name, detail))
    print("-" * 56)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
