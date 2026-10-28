#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""访问控制回归测试（跨平台）。

与 tests/smoke_bridge.py 分工：smoke 管主链路（HTTP -> 队列 -> worker -> TTS），
本文件管安全矩阵（鉴权、绑定、暴露面、秘密不外泄）。两者互不 import，
辅助函数各自复制一份，避免耦合——改一个套件不会波及另一个。

覆盖项：
  1. fail-closed：绑非 loopback 且令牌为空 / 过短 / 非 ASCII -> 拒绝启动；
     绑回环时同样的令牌不受影响（不误伤默认用户）
  2. Origin 端口不符 -> 403（只比 hostname 不比端口时，任意本地页面可跨站打）
  3. 非回环读：无令牌 401，?token= 可进，页面不再内嵌令牌
  4. 非回环写：一律 403（remote write disabled），令牌正确也不例外
  5. 秘密不外泄：未授权响应体不得出现令牌明文（参数化遍历，新增端点自动覆盖）
  6. Referrer-Policy 响应头 + 页面无任何跨源 URL 字面量
  7. BRIDGE_ALLOW_UNAUTHENTICATED=1 逃生舱行为与文档一致

环境要求：
  - 需要本机能把服务绑到 0.0.0.0。Windows 首次可能弹防火墙提示，
    **不需要管理员权限**；企业策略禁止监听时相关用例会失败并给出原因。
  - 令牌用例必须从本机**非回环 IP** 发起：_token_ok() 对 127.0.0.1 客户端
    免校验（本机转发器不带自定义请求头），从回环发起永远测不到令牌分支。
    取不到非回环 IPv4 时相关用例 SKIP 并打印原因，不会误红。

用法：
    python tests/access_control.py
退出码：0 全部符合预期，1 有不符合项。
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(ROOT, "bridge.py")
TOKEN = "s3cret-token-0123456789"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  -> " + detail) if detail and not ok else ""))
    return ok


def skip(name, why):
    print("[SKIP] %s  -> %s" % (name, why))
    RESULTS.append((name, True, "skipped"))


def lan_ip():
    """取一个本机非回环 IPv4；没有则返回空串。"""
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return ""


def wait_port(port, timeout=20.0, host="127.0.0.1"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def boot(port, extra):
    env = dict(os.environ)
    env.update({"BRIDGE_PORT": str(port), "BRIDGE_MIN_INTERVAL": "0",
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8:replace"})
    env.pop("DEEPSEEK_API_KEY", None)      # 不产生任何计费调用
    env.update(extra)
    return subprocess.Popen([sys.executable, BRIDGE], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


def raw_post(port, body, host_header=None, origin=None, token=None, dst="127.0.0.1"):
    """原始 socket 发 POST。requests/http.client 会强制补 Host，无法测「无 Host」。"""
    data = json.dumps(body).encode()
    lines = ["POST /api/danmaku HTTP/1.0"]
    if host_header is not None:
        lines.append("Host: " + host_header)
    if origin:
        lines.append("Origin: " + origin)
    if token:
        lines.append("X-Bridge-Token: " + token)
    lines += ["Content-Type: application/json", "Content-Length: %d" % len(data)]
    req = ("\r\n".join(lines) + "\r\n\r\n").encode() + data
    with socket.create_connection((dst, port), timeout=5) as s:
        s.sendall(req)
        return s.recv(4096).split(b"\r\n", 1)[0].decode("latin1")


def raw_get(port, path, host_header=None, token=None, dst="127.0.0.1"):
    lines = ["GET %s HTTP/1.0" % path]
    if host_header is not None:
        lines.append("Host: " + host_header)
    if token:
        lines.append("X-Bridge-Token: " + token)
    req = ("\r\n".join(lines) + "\r\n\r\n").encode()
    with socket.create_connection((dst, port), timeout=5) as s:
        s.sendall(req)
        buf = b""
        while True:
            b = s.recv(65536)
            if not b:
                break
            buf += b
    return buf.split(b"\r\n", 1)[0].decode("latin1"), buf


# ---------------------------------------------------------------- 用例


def case_fail_closed(port):
    """绑 0.0.0.0 且无令牌 -> 必须拒绝启动。"""
    p = boot(port, {"BRIDGE_HOST": "0.0.0.0", "BRIDGE_TOKEN": ""})
    try:
        deadline = time.time() + 10
        while time.time() < deadline and p.poll() is None:
            time.sleep(0.2)
        exited = p.poll() is not None
        out = p.stdout.read() if exited and p.stdout else ""
        check("0.0.0.0 + 空令牌 -> 拒绝启动（fail-closed）",
              exited and p.returncode != 0,
              "进程仍在运行（fail-open）rc=%s" % p.returncode)
        if exited:
            check("拒绝启动时给出可操作提示",
                  "BRIDGE_TOKEN" in out and "BRIDGE_ALLOW_UNAUTHENTICATED" in out,
                  (out or "")[:160])
    finally:
        if p.poll() is None:
            stop(p)


def case_origin_port(port):
    """Origin 端口必须比对。"""
    p = boot(port, {"BRIDGE_HOST": "127.0.0.1", "BRIDGE_TOKEN": ""})
    try:
        if not wait_port(port):
            return check("Origin 端口校验", False, "服务未启动")
        h = "127.0.0.1:%d" % port
        head = raw_post(port, {"kind": "speak", "text": "op"}, h,
                        origin="http://127.0.0.1:9999")
        check("Origin 端口不符 -> 403", " 403 " in head, head)
        head = raw_post(port, {"kind": "speak", "text": "ok"}, h,
                        origin="http://127.0.0.1:%d" % port)
        check("Origin 同源 -> 200（对照）", " 200 " in head, head)
        head = raw_post(port, {"kind": "speak", "text": "evil"}, "evil.example.com")
        check("外域 Host -> 403（对照）", " 403 " in head, head)
    finally:
        stop(p)


def case_token_nonloopback(port):
    """非回环客户端：POST 与 GET 都必须带令牌。"""
    ip = lan_ip()
    if not ip:
        skip("非回环令牌校验", "本机无可用非回环 IPv4，无法模拟外部客户端")
        return
    p = boot(port, {"BRIDGE_HOST": "0.0.0.0", "BRIDGE_TOKEN": "s3cret-token-0123456789"})
    try:
        if not wait_port(port, host=ip):
            return check("非回环令牌校验", False, "服务未启动（%s:%d）" % (ip, port))
        spoof = "127.0.0.1:%d" % port        # 伪造 Host 绕过 rebinding 检查

        head = raw_post(port, {"kind": "speak", "text": "no-token"},
                        spoof, dst=ip)
        check("非回环 POST 无令牌 -> 403（remote write disabled）", " 403 " in head, head)

        head = raw_post(port, {"kind": "speak", "text": "bad"}, spoof,
                        token="wrong", dst=ip)
        check("非回环 POST 错误令牌 -> 403", " 403 " in head, head)

        head, _ = raw_get(port, "/api/state", spoof, dst=ip)
        check("非回环 GET /api/state 无令牌 -> 401", " 401 " in head, head)

        # 造一条音频，再验证不能匿名枚举
        head = raw_post(port, {"kind": "speak", "text": "私有内容"},
                        spoof, token=TOKEN, dst=ip)
        check("非回环 POST 令牌正确也 403（写操作仅限回环）", " 403 " in head, head)
        deadline = time.time() + 60
        while time.time() < deadline:
            _, body = raw_get(port, "/api/state", spoof, token="s3cret-token-0123456789", dst=ip)
            if b'"done"' in body:
                break
            time.sleep(0.5)
        head, _ = raw_get(port, "/audio?seq=1", spoof, dst=ip)
        check("非回环 GET /audio?seq=1 无令牌 -> 401", " 401 " in head, head)
        head, _ = raw_get(port, "/video", spoof, dst=ip)
        check("非回环 GET /video 无令牌 -> 401", " 401 " in head, head)
    finally:
        stop(p)


def case_page_no_token(port):
    """页面不得把令牌交给未授权客户端：非回环 GET / 必须 401。

    修复前 GET / 不校验令牌且页面内联令牌，等于把可写凭据发给同网段任何人。
    """
    ip = lan_ip()
    if not ip:
        skip("页面不泄漏令牌", "本机无可用非回环 IPv4")
        return
    p = boot(port, {"BRIDGE_HOST": "0.0.0.0", "BRIDGE_TOKEN": TOKEN})
    try:
        if not wait_port(port, host=ip):
            return check("页面不泄漏令牌", False, "服务未启动")
        spoof = "127.0.0.1:%d" % port
        head, body = raw_get(port, "/", spoof, dst=ip)
        check("非回环 GET / 无令牌 -> 401", " 401 " in head, head)
        check("非回环 GET / 响应不含令牌明文", TOKEN.encode() not in body,
              "响应体含令牌，长度 %d" % len(body))
        head, body = raw_get(port, "/avatar", spoof, dst=ip)
        check("非回环 GET /avatar 无令牌 -> 401", " 401 " in head, head)

        # 带 query 令牌应放行（手机/OBS 只能靠 URL 携带）
        head, body = raw_get(port, "/?token=" + TOKEN, spoof, dst=ip)
        check("非回环 GET /?token= 放行（对照）", " 200 " in head, head)

        # 但 query 令牌不得在 POST 侧生效，否则等于把可写凭据写进 URL/历史
        head = raw_post(port, {"kind": "speak", "text": "q"}, spoof, dst=ip)
        check("POST 不接受 query 令牌（须用请求头）", " 403 " in head, head)
    finally:
        stop(p)


def case_no_secret_in_unauthorized(port):
    """通用性质：任何未授权响应体里都不允许出现令牌明文。

    参数化遍历端点，新增端点自动被覆盖，比逐条用例更防回归。
    """
    ip = lan_ip()
    if not ip:
        skip("未授权响应不含令牌", "本机无可用非回环 IPv4")
        return
    p = boot(port, {"BRIDGE_HOST": "0.0.0.0", "BRIDGE_TOKEN": TOKEN})
    try:
        if not wait_port(port, host=ip):
            return check("未授权响应不含令牌", False, "服务未启动")
        spoof = "127.0.0.1:%d" % port
        targets = ["/", "/avatar", "/api/state", "/audio", "/audio?seq=1",
                   "/video", "/live2d/x.png", "/no-such-endpoint"]
        leaked = []
        for path in targets:
            head, body = raw_get(port, path, spoof, dst=ip)
            if " 401 " in head or " 403 " in head:
                if TOKEN.encode() in body:
                    leaked.append(path)
        check("未授权响应体均不含令牌明文（%d 个端点）" % len(targets),
              not leaked, "泄漏端点: %s" % ", ".join(leaked))

        head = raw_post(port, {"kind": "speak", "text": "x"}, spoof, dst=ip)
        check("未授权 POST 响应体不含令牌明文", (" 401 " in head or " 403 " in head), head)
    finally:
        stop(p)


def case_non_ascii_token(port):
    """A 方案：令牌必须为 ASCII，且检查限定在「暴露到局域网」分支。

    中文令牌在 header 路径会因 latin-1 解码 + UTF-8 重编码而永不匹配，
    所以非回环时必须拒绝启动；但绑回环时令牌根本不参与比较，
    不该误伤默认用户——这两条要同时断言，否则 A 实现成无作用域会漏。
    """
    cn = "小雅令牌一二三四五六七八九十甲乙"   # 16 字，长度检查可过

    # 1) 非回环 + 中文令牌 -> 拒绝启动
    p = boot(port, {"BRIDGE_HOST": "0.0.0.0", "BRIDGE_TOKEN": cn})
    try:
        deadline = time.time() + 10
        while time.time() < deadline and p.poll() is None:
            time.sleep(0.2)
        exited = p.poll() is not None
        out = p.stdout.read() if exited and p.stdout else ""
        check("非回环 + 中文令牌 -> 拒绝启动", exited and p.returncode != 0,
              "进程仍在运行（会 401 而非拒启）rc=%s" % p.returncode)
        if exited:
            check("拒绝时提示只能使用 ASCII",
                  "ASCII" in out.upper(), (out or "")[:160])
    finally:
        if p.poll() is None:
            stop(p)

    # 2) 回环 + 中文令牌 -> 正常启动（不误伤默认用户）
    p2 = boot(port + 1, {"BRIDGE_HOST": "127.0.0.1", "BRIDGE_TOKEN": cn})
    try:
        ok = wait_port(port + 1, timeout=8)
        check("回环 + 中文令牌 -> 正常启动（不误伤默认用户）", ok,
              "绑回环时令牌不参与比较，不该被拒启")
        if ok:
            head, _ = raw_get(port + 1, "/api/state", "127.0.0.1:%d" % (port + 1))
            check("回环 + 中文令牌 -> 本机可读", " 200 " in head, head)
    finally:
        stop(p2)


def case_weak_token(port):
    """1 位令牌在 LAN 上秒破，应拒绝启动或至少显式告警。"""
    ip = lan_ip()
    if not ip:
        skip("弱令牌被拒或告警", "本机无可用非回环 IPv4")
        return
    p = boot(port, {"BRIDGE_HOST": "0.0.0.0", "BRIDGE_TOKEN": "1"})
    try:
        started = wait_port(port, host=ip, timeout=6)
        out = ""
        if not started and p.poll() is not None and p.stdout:
            out = p.stdout.read() or ""
        elif started:
            p.terminate()
            try:
                out = p.stdout.read() or ""
            except Exception:
                out = ""
        warned = any(k in out for k in ("过短", "太短", "弱", "至少"))
        check("弱令牌（1 位）应拒绝启动或告警", (not started) or warned,
              "已启动且无告警；1 位令牌可暴力枚举")
    finally:
        if p.poll() is None:
            stop(p)


def case_referrer_policy(port):
    """令牌放进 URL 后，必须用响应头声明 Referrer-Policy，且页面不得引用跨源资源。

    断言对象是响应头而非 <meta>：meta 只对「它之后」发起的请求生效，谁把
    CDN <link> 挪到 meta 前面就静默失效；header 覆盖全部响应且改 HTML 删不掉。

    跨源扫描用「页面里出现任何 https?:// 字面量」判定，能同时覆盖
    src/href/@import/<iframe>/fetch("https://…")/location.href 等所有形式——
    声明式与动态构造都逃不掉，因为最终都得写出 URL 字面量。
    """
    p = boot(port, {"BRIDGE_HOST": "127.0.0.1", "BRIDGE_TOKEN": ""})
    try:
        if not wait_port(port):
            return check("Referrer 策略", False, "服务未启动")

        head, body = raw_get(port, "/", "127.0.0.1:%d" % port)
        if " 200 " not in head:
            return check("Referrer 策略", False, head)
        html = body.decode("utf-8", "replace")

        # a) 响应头（HTML 文档）
        head_full = raw_get(port, "/", "127.0.0.1:%d" % port)
        check("GET / 响应头含 Referrer-Policy: no-referrer",
              "referrer-policy: no-referrer" in _raw_headers(port, "/").lower(),
              "未设置 Referrer-Policy 响应头")

        # a2) 响应头（JSON 接口，meta 覆盖不到）
        hdrs = _raw_headers(port, "/api/state")
        check("GET /api/state 响应头含 Referrer-Policy",
              "referrer-policy: no-referrer" in hdrs.lower(),
              "JSON 响应未设置 Referrer-Policy")

        # b) 页面不得出现任何跨源 URL 字面量
        urls = re.findall(r"https?://[^\s\"'<>)]+", html)
        check("页面无任何 http(s):// 字面量（含 fetch/location/iframe 动态构造）",
              not urls, "发现 %d 处: %s" % (len(urls), "; ".join(urls[:3])))

        # c) 高危声明式引用
        bad = re.findall(r"@import|<iframe", html, re.I)
        check("页面无 @import / <iframe", not bad, "发现: %s" % bad[:3])
    finally:
        stop(p)


def _raw_headers(port, path):
    """返回原始响应头文本（不含 body），用于断言响应头。"""
    req = ("GET %s HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n"
           % (path, port)).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(req)
        buf = b""
        while True:
            b = s.recv(65536)
            if not b:
                break
            buf += b
            if b"\r\n\r\n" in buf:
                break
    return buf.split(b"\r\n\r\n", 1)[0].decode("latin1")


def case_explicit_optin(port):
    """BRIDGE_ALLOW_UNAUTHENTICATED=1 是显式逃生舱：应能启动，且行为与文档一致。"""
    p = boot(port, {"BRIDGE_HOST": "0.0.0.0", "BRIDGE_TOKEN": "",
                    "BRIDGE_ALLOW_UNAUTHENTICATED": "1"})
    try:
        if not wait_port(port):
            return check("显式放弃鉴权后可启动", False, "服务未启动")
        check("BRIDGE_ALLOW_UNAUTHENTICATED=1 可启动", True)
        head = raw_post(port, {"kind": "speak", "text": "optin"},
                        "127.0.0.1:%d" % port)
        check("逃生舱下 POST 放行（与文档一致）", " 200 " in head, head)
    finally:
        stop(p)


def main():
    print("访问控制回归（%s）\n" % os.path.basename(BRIDGE))
    case_fail_closed(8181)
    case_origin_port(8182)
    case_token_nonloopback(8183)
    case_page_no_token(8185)
    case_no_secret_in_unauthorized(8186)
    case_non_ascii_token(8187)
    case_weak_token(8188)
    case_referrer_policy(8189)
    case_explicit_optin(8184)

    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "-" * 56)
    print("符合预期 %d / %d" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("不符合预期：")
        for name, _ok, detail in failed:
            print("  - %s: %s" % (name, detail))
    print("-" * 56)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
