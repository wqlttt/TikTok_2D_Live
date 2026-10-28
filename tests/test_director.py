# -*- coding: utf-8 -*-
"""GiftDirector 的最小单元测试：规则匹配、冷却、打断、排队。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from director import GiftDirector  # noqa: E402


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_config():
    return {
        "defaults": {"duration": 2.0, "priority": 10, "interruptible": True},
        "rules": [
            {
                "id": "large",
                "match": {"min_value": 100},
                "priority": 80,
                "cooldown": 0,
                "interruptible": False,
                "duration": 5.0,
                "timeline": [
                    {"at": 0, "type": "effect", "name": "fireworks", "duration": 3.0},
                    {"at": 0.2, "type": "toast", "text": "大礼物 {nick} {gift}"},
                ],
            },
            {
                "id": "medium",
                "match": {"min_value": 10, "max_value": 99},
                "priority": 40,
                "cooldown": 0,
                "interruptible": True,
                "duration": 2.0,
                "timeline": [{"at": 0, "type": "toast", "text": "中礼物 {nick}"}],
            },
            {
                "id": "small",
                "match": {"max_value": 9},
                "priority": 20,
                "cooldown": 5,
                "interruptible": True,
                "duration": 2.0,
                "timeline": [
                    {"at": 0, "type": "motion", "group": "TapBody", "index": 0},
                    {"at": 0.1, "type": "toast", "text": "谢谢 {nick} 的 {gift} x{count}"},
                ],
            },
            {
                "id": "fallback",
                "match": {},
                "priority": 1,
                "interruptible": True,
                "timeline": [{"at": 0, "type": "toast", "text": "谢谢 {nick}"}],
            },
        ],
    }


class GiftDirectorTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.director = GiftDirector(config=make_config(), clock=self.clock, max_queue=2)

    def gift(self, **overrides):
        event = {"nick": "小明", "gift": "玫瑰", "count": 1, "value": 1}
        event.update(overrides)
        return event

    def test_matches_small_and_renders_template(self):
        plan = self.director.ingest(self.gift(value=1, count=3))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["rule_id"], "small")
        self.assertEqual(plan["gift"]["nick"], "小明")
        self.assertEqual(plan["timeline"][1]["text"], "谢谢 小明 的 玫瑰 x3")

    def test_large_has_higher_priority(self):
        plan = self.director.ingest(self.gift(value=100, gift="嘉年华"))
        self.assertEqual(plan["rule_id"], "large")
        self.assertFalse(plan["interruptible"])
        self.assertEqual(plan["duration"], 5.0)

    def test_cooldown_blocks_repeat(self):
        self.assertIsNotNone(self.director.ingest(self.gift()))
        self.assertIsNone(self.director.ingest(self.gift()))
        self.clock.advance(5.1)
        self.assertIsNotNone(self.director.ingest(self.gift()))

    def test_high_priority_interrupts_low_priority(self):
        first = self.director.ingest(self.gift())
        self.assertEqual(first["rule_id"], "small")
        second = self.director.ingest(self.gift(value=100, gift="嘉年华"))
        self.assertEqual(second["rule_id"], "large")
        self.assertTrue(second["interrupt"])

    def test_low_priority_waits_for_non_interruptible(self):
        self.director.ingest(self.gift(value=100, gift="嘉年华"))
        # 大礼物不可打断，小礼物应该排队
        self.assertIsNone(self.director.ingest(self.gift(value=1)))
        self.assertIsNone(self.director.tick())
        self.clock.advance(5.1)
        queued = self.director.tick()
        self.assertIsNotNone(queued)
        self.assertEqual(queued["rule_id"], "small")

    def test_queue_full_drops_low_priority(self):
        self.director.ingest(self.gift(value=100, gift="嘉年华"))
        self.director.ingest(self.gift(value=1, nick="A"))
        self.director.ingest(self.gift(value=50, nick="B"))
        self.director.ingest(self.gift(value=50, nick="C"))
        # max_queue=2；第三个中优先级不高于队尾，被丢弃
        self.assertEqual(self.director.snapshot()["queue_depth"], 2)

    def test_snapshot(self):
        self.director.ingest(self.gift())
        snapshot = self.director.snapshot()
        self.assertEqual(snapshot["rules"], 4)
        self.assertIsNotNone(snapshot["current"])

    def test_invalid_event_is_ignored(self):
        self.assertIsNone(self.director.ingest(None))

    def test_default_config_combo_rule(self):
        director = GiftDirector()
        plan = director.ingest({"nick": "小红", "gift": "棒棒糖", "count": 1,
                                "value": 1, "combo": 10})
        self.assertIsNotNone(plan)
        self.assertEqual(plan["rule_id"], "combo_10")

    def test_default_config_name_rule(self):
        director = GiftDirector()
        plan = director.ingest({"nick": "小蓝", "gift": "小心心", "count": 1,
                                "value": 1, "combo": 1})
        self.assertIsNotNone(plan)
        self.assertEqual(plan["rule_id"], "heart_gift")

    def test_large_gift_does_not_use_live2d_motion(self):
        director = GiftDirector()
        plan = director.ingest({"nick": "大老板", "gift": "嘉年华", "count": 1,
                                "value": 1000, "combo": 1})
        self.assertIsNotNone(plan)
        types = [step.get("type") for step in plan["timeline"]]
        self.assertNotIn("motion", types)
        self.assertIn("effect", types)

    def test_cooldown_falls_through_to_lower_rule(self):
        # small 冷却中：第二个小礼物不应被丢，而是落到 fallback 排队
        self.director.ingest(self.gift(nick="A"))
        self.assertIsNone(self.director.ingest(self.gift(nick="B")))
        self.assertEqual(self.director.snapshot()["queue_depth"], 1)
        self.clock.advance(2.1)
        queued = self.director.tick()
        self.assertEqual(queued["rule_id"], "fallback")
        self.assertEqual(queued["gift"]["nick"], "B")

    def test_queue_rule_on_cooldown_is_queued_not_dropped(self):
        config = make_config()
        config["rules"][0].update({"cooldown": 20, "on_cooldown": "queue", "queue_ttl": 60})
        director = GiftDirector(config=config, clock=self.clock)
        first = director.ingest(self.gift(nick="A", value=520, gift="嘉年华"))
        self.assertEqual(first["rule_id"], "large")
        self.clock.advance(1)
        self.assertIsNone(director.ingest(self.gift(nick="B", value=520, gift="嘉年华")))
        self.clock.advance(4.1)
        second = director.tick()
        self.assertIsNotNone(second)
        self.assertEqual(second["rule_id"], "large")
        self.assertEqual(second["gift"]["nick"], "B")

    def test_queue_rule_on_cooldown_plays_when_idle(self):
        config = make_config()
        config["rules"][0].update({"cooldown": 20, "on_cooldown": "queue"})
        director = GiftDirector(config=config, clock=self.clock)
        director.ingest(self.gift(nick="A", value=520))
        self.clock.advance(6)   # A 已播完，但仍在 20s 冷却内
        plan = director.ingest(self.gift(nick="B", value=520))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["rule_id"], "large")

    def test_queue_rule_on_cooldown_does_not_interrupt(self):
        config = make_config()
        config["rules"][0].update({"cooldown": 20, "on_cooldown": "queue"})
        director = GiftDirector(config=config, clock=self.clock)
        director.ingest(self.gift(nick="A", value=520))
        self.clock.advance(6)
        director.ingest(self.gift(nick="S", value=50))          # medium 在台上
        self.assertIsNone(director.ingest(self.gift(nick="B", value=520)))
        self.assertEqual(director.snapshot()["current"]["rule_id"], "medium")

    def test_queued_plan_expires(self):
        self.director.ingest(self.gift(value=100, gift="嘉年华"))   # 5s 不可打断
        self.director.ingest(self.gift(value=50, nick="B"))           # 排队，ttl 默认 10s
        self.clock.advance(11)
        self.assertIsNone(self.director.tick())
        self.assertEqual(self.director.snapshot()["queue_depth"], 0)

    def test_event_id_dedupe(self):
        self.assertIsNotNone(self.director.ingest(self.gift(value=100, event_id="e1")))
        self.clock.advance(6)
        self.assertIsNone(self.director.ingest(self.gift(value=100, event_id="e1")))
        self.assertIsNotNone(self.director.ingest(self.gift(value=100, event_id="e2")))

    def test_default_config_busy_room_no_drop(self):
        director = GiftDirector(clock=self.clock)
        director.ingest({"nick": "A", "gift": "神秘礼物", "value": 520})
        self.clock.advance(1)
        director.ingest({"nick": "A", "gift": "神秘礼物", "value": 520})  # 同一人再送
        director.ingest({"nick": "B", "gift": "神秘礼物", "value": 520})
        played = []
        for _ in range(40):
            self.clock.advance(0.5)
            plan = director.tick()
            if plan:
                played.append((plan["rule_id"], plan["gift"]["nick"]))
        self.assertEqual(played, [("large_gift", "A"), ("large_gift", "B")])

    def test_video_step_is_preserved(self):
        config = make_config()
        config["rules"].insert(0, {
            "id": "video_gift",
            "match": {"names": ["WebM"]},
            "priority": 90,
            "duration": 5.0,
            "timeline": [{
                "at": 0,
                "type": "video",
                "src": "/static/videos/large_gift.webm",
                "duration": 5.0,
                "audio": False,
                "fit": "contain",
                "loop": False,
            }],
        })
        director = GiftDirector(config=config, clock=self.clock)
        plan = director.ingest({"nick": "视频测试", "gift": "WebM 大礼物",
                                "count": 1, "value": 1, "combo": 1})
        self.assertEqual(plan["timeline"][0]["type"], "video")
        self.assertEqual(plan["timeline"][0]["src"], "/static/videos/large_gift.webm")


if __name__ == "__main__":
    unittest.main()
