# -*- coding: utf-8 -*-
"""winsound 存根（macOS 兼容）。

cyber-xiaoya-danmaku 的 tts_worker.py 顶层 `import winsound`，而 winsound 是
Windows 专有模块。在 macOS 上，把本文件复制到 cyber-xiaoya-danmaku/ 目录下，
即可让 reader.py 正常 import（本项目「转发给数字人桥」模式 output=dh 不做本地
朗读，PlaySound 仅为 no-op，不影响转发逻辑）。

用法：
  cp patches/winsound.py cyber-xiaoya-danmaku/winsound.py
"""

SND_FILENAME = 0x00020000
SND_ASYNC = 0x0001
SND_MEMORY = 0x0004
SND_PURGE = 0x0040
SND_NODEFAULT = 0x0002
SND_LOOP = 0x0008
SND_NOSTOP = 0x0010
SND_NOWAIT = 0x00002000
SND_ALIAS = 0x00010000
SND_ALIAS_ID = 0x00110000


def PlaySound(sound, flags=0):
    """macOS 下 no-op。"""
    return None


def Beep(frequency=440, duration=100):
    """macOS 下 no-op。"""
    return None


def MessageBeep(type=-1):
    return None
