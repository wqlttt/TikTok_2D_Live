#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""礼物演出导演（Gift Director）。

职责很窄：
  礼物事件 -> 匹配规则 -> 生成 ActionPlan -> 排队/冷却/优先级/打断

它不负责：
  - Live2D 渲染
  - TTS / LLM
  - 抖音协议

这样做的好处是：以后换渲染器（Live2D / Spine / 序列帧 / 视频），
只要消费同一个 ActionPlan JSON 即可。

ActionPlan 结构（前端消费）：
{
  "action_id": "large_gift-3",
  "rule_id": "large_gift",
  "priority": 80,
  "interruptible": false,
  "duration": 5.0,
  "created_at": 1710000000.0,
  "interrupt": false,
  "gift": {"nick": "...", "name": "...", "count": 1, "value": 100, "combo": 1},
  "timeline": [
    {"at": 0.0, "type": "effect", "name": "fireworks", "duration": 3.2},
    {"at": 0.2, "type": "toast", "text": "🎉 张三 送出 嘉年华 x1！", "duration": 3.2}
  ]
}

大礼物也可以把 effect 换成预渲染视频：
  {"at": 0.0, "type": "video", "src": "/static/videos/large_gift.webm",
   "duration": 5.0, "audio": false, "fit": "contain"}

时间轴设计：
  - at: 相对于动作开始的秒数，可并行
  - duration: 该步骤持续时间（特效/浮层/视频使用）
  - type: motion / effect / video / toast / speak；未知类型由前端派发事件
  - 前端按 at 调度，因此“复杂活动”可以是一条时间轴，而不是一串 if/else

规则配置见 config/gift_actions.json。
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "config", "gift_actions.json")

_DEFAULT_DEFAULTS = {
    "duration": 2.4,
    "priority": 10,
    "cooldown": 0,
    "user_cooldown": 0,
    "interruptible": True,
}


class _SafeDict(dict):
    """format_map 用：缺字段时保留 {name}，避免配置写错直接崩。"""

    def __missing__(self, key):  # noqa: D105
        return "{" + key + "}"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _normalize_timeline(raw_steps: Any) -> List[Dict[str, Any]]:
    """校验/规整时间轴，保留未知 type 供未来扩展。"""
    if not isinstance(raw_steps, list):
        return []
    steps: List[Dict[str, Any]] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        step = copy.deepcopy(raw)
        step["type"] = str(step.get("type") or "").strip().lower()
        if not step["type"]:
            continue
        step["at"] = max(0.0, _as_float(step.get("at"), 0.0))
        if "duration" in step:
            step["duration"] = max(0.0, _as_float(step.get("duration"), 0.0))
        steps.append(step)
    return steps


def _timeline_duration(steps: List[Dict[str, Any]]) -> float:
    end = 0.0
    for step in steps:
        end = max(end, _as_float(step.get("at"), 0.0) +
                  _as_float(step.get("duration"), 0.0))
    return end


def _render(value: Any, ctx: Dict[str, Any]) -> Any:
    """递归替换 {nick}/{gift}/{count}/{value}/{combo} 等占位符。"""
    if isinstance(value, str):
        try:
            return value.format_map(_SafeDict(ctx))
        except (ValueError, KeyError):
            return value
    if isinstance(value, list):
        return [_render(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _render(v, ctx) for k, v in value.items()}
    return value


class GiftDirector:
    """礼物规则导演。线程安全，可被 HTTP handler 和后台 tick 同时调用。"""

    def __init__(self, config_path: Optional[str] = None,
                 config: Optional[Dict[str, Any]] = None,
                 clock=time.time, max_queue: int = 8):
        self._lock = threading.RLock()
        self._clock = clock
        self._config_path = config_path or _DEFAULT_CONFIG
        self._max_queue = max(1, _as_int(max_queue, 8))
        self._defaults: Dict[str, Any] = dict(_DEFAULT_DEFAULTS)
        self._rules: List[Dict[str, Any]] = []
        self._current: Optional[Dict[str, Any]] = None
        self._current_until = 0.0
        self._queue: List[Dict[str, Any]] = []
        self._next_at: Dict[str, float] = {}
        self._user_next_at: Dict[str, float] = {}
        self._seq = 0
        self.last_error = ""

        if config is not None:
            self.load_config(config)
        else:
            self.reload()

    # ---------------------------------------------------------------- config

    def reload(self) -> bool:
        """重新读取配置；失败时保留旧规则。"""
        try:
            with open(self._config_path, encoding="utf-8") as f:
                data = json.load(f)
            self.load_config(data)
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def load_config(self, data: Dict[str, Any]) -> None:
        """加载配置对象。会清空当前演出和队列，避免旧动作引用旧规则。"""
        if not isinstance(data, dict):
            raise ValueError("gift config must be a JSON object")

        defaults = dict(_DEFAULT_DEFAULTS)
        if isinstance(data.get("defaults"), dict):
            defaults.update(data["defaults"])

        rules: List[Dict[str, Any]] = []
        for index, raw in enumerate(data.get("rules") or []):
            if not isinstance(raw, dict):
                continue
            rule = copy.deepcopy(raw)
            rule["id"] = str(rule.get("id") or f"rule_{index}")
            rule["priority"] = _as_int(rule.get("priority"),
                                       _as_int(defaults.get("priority"), 10))
            rule["cooldown"] = max(0.0, _as_float(rule.get("cooldown"),
                                                  _as_float(defaults.get("cooldown"), 0.0)))
            rule["user_cooldown"] = max(
                0.0,
                _as_float(rule.get("user_cooldown"),
                          _as_float(defaults.get("user_cooldown"), 0.0)),
            )
            rule["interruptible"] = bool(rule.get("interruptible",
                                                  defaults.get("interruptible", True)))
            rule["match"] = rule.get("match") if isinstance(rule.get("match"), dict) else {}
            timeline = _normalize_timeline(rule.get("timeline") or rule.get("steps") or [])
            rule["timeline"] = timeline
            rule["duration"] = max(
                0.5,
                _as_float(rule.get("duration"), 0.0),
                _timeline_duration(timeline),
                _as_float(defaults.get("duration"), 2.4),
            )
            rule["_order"] = index
            rule["_specificity"] = self._specificity(rule["match"])
            rules.append(rule)

        rules.sort(key=lambda r: (-r["priority"], -r["_specificity"], r["_order"]))

        with self._lock:
            self._defaults = defaults
            self._rules = rules
            self._current = None
            self._current_until = 0.0
            self._queue = []
            self._next_at = {}
            self._user_next_at = {}
            self.last_error = ""

    @staticmethod
    def _specificity(match: Dict[str, Any]) -> int:
        return sum(1 for v in match.values() if v not in (None, "", [], {}, ()))

    # ------------------------------------------------------------------ event

    @staticmethod
    def normalize_event(event: Dict[str, Any]) -> Dict[str, Any]:
        nick = str(event.get("nick") or event.get("user") or "神秘人").strip()
        gift = str(event.get("gift") or event.get("gift_item") or "礼物").strip()
        count = max(1, _as_int(event.get("count", event.get("gift_num", 1)), 1))
        value = max(0, _as_int(event.get("value", event.get("gift_value", 0)), 0))
        combo = max(count, _as_int(event.get("combo"), count))
        return {
            "nick": nick or "神秘人",
            "gift": gift or "礼物",
            "count": count,
            "value": value,
            "combo": combo,
            "gift_id": str(event.get("gift_id") or ""),
            "event_id": str(event.get("event_id") or ""),
            "ts": _as_float(event.get("ts"), time.time()),
        }

    # ------------------------------------------------------------------ match

    @staticmethod
    def _matches(rule: Dict[str, Any], event: Dict[str, Any]) -> bool:
        match = rule.get("match") or {}

        names = match.get("names")
        if isinstance(names, str):
            names = [names]
        if names:
            lowered = str(event["gift"]).lower()
            if not any(str(name).lower() in lowered for name in names):
                return False

        excludes = match.get("exclude_names")
        if isinstance(excludes, str):
            excludes = [excludes]
        if excludes:
            lowered = str(event["gift"]).lower()
            if any(str(name).lower() in lowered for name in excludes):
                return False

        if "min_value" in match and event["value"] < _as_int(match["min_value"]):
            return False
        if "max_value" in match and event["value"] > _as_int(match["max_value"]):
            return False
        if "min_count" in match and event["count"] < _as_int(match["min_count"]):
            return False
        if "max_count" in match and event["count"] > _as_int(match["max_count"]):
            return False
        if "min_combo" in match and event["combo"] < _as_int(match["min_combo"]):
            return False
        if "max_combo" in match and event["combo"] > _as_int(match["max_combo"]):
            return False
        return True

    def _select(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for rule in self._rules:
            if self._matches(rule, event):
                return rule
        return None

    # ------------------------------------------------------------- scheduling

    def _expire(self, now: float) -> None:
        if self._current is not None and now >= self._current_until:
            self._current = None
            self._current_until = 0.0

    def _cooldown_ok(self, rule: Dict[str, Any], event: Dict[str, Any], now: float) -> bool:
        rule_id = rule["id"]
        if now < self._next_at.get(rule_id, 0.0):
            return False
        user_key = f"{rule_id}|{event['nick']}"
        return now >= self._user_next_at.get(user_key, 0.0)

    def _mark_cooldown(self, rule: Dict[str, Any], event: Dict[str, Any], now: float) -> None:
        if rule["cooldown"] > 0:
            self._next_at[rule["id"]] = now + rule["cooldown"]
        if rule["user_cooldown"] > 0:
            self._user_next_at[f"{rule['id']}|{event['nick']}"] = now + rule["user_cooldown"]

    def _build_plan(self, rule: Dict[str, Any], event: Dict[str, Any],
                    now: float) -> Dict[str, Any]:
        self._seq += 1
        ctx = {
            "nick": event["nick"],
            "user": event["nick"],
            "gift": event["gift"],
            "item": event["gift"],
            "count": event["count"],
            "num": event["count"],
            "value": event["value"],
            "combo": event["combo"],
        }
        return {
            "action_id": f"{rule['id']}-{self._seq}",
            "rule_id": rule["id"],
            "label": str(rule.get("label") or rule["id"]),
            "priority": int(rule["priority"]),
            "interruptible": bool(rule["interruptible"]),
            "duration": round(float(rule["duration"]), 3),
            "created_at": round(now, 3),
            "interrupt": False,
            "gift": {
                "nick": event["nick"],
                "name": event["gift"],
                "count": event["count"],
                "value": event["value"],
                "combo": event["combo"],
            },
            "timeline": _render(rule["timeline"], ctx),
        }

    def _enqueue(self, plan: Dict[str, Any]) -> bool:
        plan["_queued_at"] = self._clock()
        if len(self._queue) < self._max_queue:
            self._queue.append(plan)
            return True

        # 队列满：只有更高优先级才挤掉最低优先级。
        self._queue.sort(key=lambda p: (-p["priority"], p.get("_queued_at", 0.0)))
        lowest = self._queue[-1]
        if plan["priority"] <= lowest["priority"]:
            return False
        self._queue.pop()
        self._queue.append(plan)
        return True

    @staticmethod
    def _public(plan: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in plan.items() if not key.startswith("_")}

    def ingest(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """收一个礼物事件。

        返回 ActionPlan 表示“现在就可以播”；
        返回 None 表示被冷却/队列/无规则拦截，前端不需要处理。
        """
        if not isinstance(event, dict):
            return None
        now = self._clock()
        normalized = self.normalize_event(event)
        with self._lock:
            self._expire(now)
            rule = self._select(normalized)
            if rule is None or not self._cooldown_ok(rule, normalized, now):
                return None

            plan = self._build_plan(rule, normalized, now)
            self._mark_cooldown(rule, normalized, now)

            if self._current is not None:
                # 高优先级且当前演出允许打断 -> 立即替换
                if plan["priority"] > self._current["priority"] and self._current.get("interruptible", True):
                    plan["interrupt"] = True
                    self._current = plan
                    self._current_until = now + plan["duration"]
                    return self._public(plan)

                self._enqueue(plan)
                return None

            self._current = plan
            self._current_until = now + plan["duration"]
            return self._public(plan)

    def tick(self) -> Optional[Dict[str, Any]]:
        """推进队列；当前演出结束后返回下一个 ActionPlan。"""
        now = self._clock()
        with self._lock:
            self._expire(now)
            if self._current is not None or not self._queue:
                return None
            self._queue.sort(key=lambda p: (-p["priority"], p.get("_queued_at", 0.0)))
            plan = self._queue.pop(0)
            self._current = plan
            self._current_until = now + plan["duration"]
            return self._public(plan)

    def snapshot(self) -> Dict[str, Any]:
        """调试用：当前演出/队列长度。"""
        with self._lock:
            return {
                "current": self._public(self._current) if self._current else None,
                "queue_depth": len(self._queue),
                "rules": len(self._rules),
                "last_error": self.last_error,
            }
