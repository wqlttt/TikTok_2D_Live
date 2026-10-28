#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查看 live2d/ 下每个模型的 Motion 组、数量和表情。

用法：
  python inspect_live2d.py
  python inspect_live2d.py Hiyori

只看 model3.json，不需要启动 bridge，也不需要 Python 第三方库。
换模型后先跑这个脚本，再把 config/gift_actions.json 里的
{"type":"motion","group":"TapBody","index":0} 改成实际存在的组/下标。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List


def find_models(root: str) -> List[str]:
    models: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".model3.json"):
                models.append(os.path.join(dirpath, name))
    return sorted(models)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def print_model(path: str) -> None:
    data = load_json(path)
    name = os.path.basename(path).replace(".model3.json", "")
    refs = data.get("FileReferences") or {}
    motions = refs.get("Motions") or {}
    expressions = refs.get("Expressions") or []

    print(f"\n{name}")
    print(f"  file: {path}")
    if not motions:
        print("  Motions: (none)")
        return

    total = 0
    for group, items in motions.items():
        items = items if isinstance(items, list) else []
        total += len(items)
        print(f"  Motions.{group}: {len(items)}")
        for index, item in enumerate(items):
            file_name = item.get("File") if isinstance(item, dict) else "?"
            print(f"    [{index}] {file_name}")

    print(f"  total motions: {total}")
    if expressions:
        print(f"  Expressions: {len(expressions)}")
        for index, item in enumerate(expressions):
            expr_name = item.get("Name") if isinstance(item, dict) else "?"
            print(f"    [{index}] {expr_name}")
    else:
        print("  Expressions: (none)")


def main() -> int:
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live2d")
    keyword = sys.argv[1] if len(sys.argv) > 1 else ""

    if not os.path.isdir(root):
        print(f"没有找到 live2d/ 目录：{root}")
        print("先运行 bash setup.sh，或把模型目录放到 live2d/ 下。")
        return 1

    models = find_models(root)
    if keyword:
        models = [path for path in models if keyword.lower() in path.lower()]
    if not models:
        print(f"没有找到 .model3.json（关键词：{keyword or '无'}）")
        return 1

    print(f"Live2D 模型目录：{root}")
    print(f"找到 {len(models)} 个模型")
    for path in models:
        try:
            print_model(path)
        except Exception as exc:  # noqa: BLE001
            print(f"\n{path}\n  读取失败：{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
