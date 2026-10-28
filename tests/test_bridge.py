# -*- coding: utf-8 -*-
"""bridge 的单元测试：队列调度（优先级/过期/去重/合并）、流式切句、按句合成。

不联网、不调 TTS：deepseek_stream 与 local_tts 都被替换成假实现。
用法：python tests/test_bridge.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402


def gift(nick="A", item="玫瑰", num=1, value=1, **kw):
    d = {"kind": "gift", "user": nick, "gift_item": item, "gift_num": num,
         "gift_value": value, "content": f"观众「{nick}」送了 {num} 个「{item}」"}
    d.update(kw)
    return d


def chat(text, nick="路人", **kw):
    d = {"kind": "chat", "user": nick, "content": text}
    d.update(kw)
    return d


class QueueTest(unittest.TestCase):
    def setUp(self):
        bridge.QUEUE.clear()
        bridge._SEEN_EVENTS.clear()
        bridge._RECENT_TEXT.clear()
        for k in bridge.QUEUE_STATS:
            bridge.QUEUE_STATS[k] = 0
        self._max = bridge.MAX_QUEUE
        self.now = 1000.0

    def tearDown(self):
        bridge.MAX_QUEUE = self._max
        bridge.QUEUE.clear()

    def test_no_rate_limit_at_entry(self):
        # 刚回复完（_last_run=now）也必须能入队：限流在 worker 里，不在入口
        bridge._last_run = self.now
        self.assertTrue(bridge.enqueue(gift(), now=self.now))
        self.assertTrue(bridge.enqueue(chat("你好"), now=self.now + 0.1))
        self.assertEqual(len(bridge.QUEUE), 2)

    def test_same_gift_twice_not_deduped_by_text(self):
        # 旧逻辑按文本去重，会把同一人连送的第二个玫瑰丢掉
        self.assertTrue(bridge.enqueue(gift(), now=self.now))
        self.assertTrue(bridge.enqueue(gift(), now=self.now + 1))

    def test_priority_order(self):
        bridge.enqueue(chat("今天天气不错"), now=self.now)
        bridge.enqueue(chat("小雅你几岁了？"), now=self.now + 1)
        bridge.enqueue(chat("我是大哥", priority="vip"), now=self.now + 2)
        bridge.enqueue(gift(), now=self.now + 3)
        order = []
        while True:
            item = bridge.pick_next(now=self.now + 4)
            if item is None:
                break
            order.append(item["kind"] + ":" + item.get("priority", "") + ":" + item["content"][:4])
        self.assertEqual([o.split(":")[0] for o in order], ["gift", "chat", "chat", "chat"])
        self.assertIn("我是大哥", order[1])
        self.assertIn("小雅你几", order[2])

    def test_chat_expires_gift_survives(self):
        bridge.enqueue(chat("很久以前的弹幕"), now=self.now)
        bridge.enqueue(gift(), now=self.now)
        later = self.now + bridge.CHAT_TTL + 1
        self.assertEqual(bridge.pick_next(now=later)["kind"], "gift")
        self.assertIsNone(bridge.pick_next(now=later))
        self.assertEqual(bridge.QUEUE_STATS["expired"], 1)

    def test_event_id_dedupe(self):
        self.assertTrue(bridge.enqueue(gift(event_id="e1"), now=self.now))
        self.assertFalse(bridge.enqueue(gift(event_id="e1"), now=self.now + 5))
        self.assertTrue(bridge.enqueue(gift(event_id="e2"), now=self.now + 5))

    def test_chat_text_spam_dedupe(self):
        self.assertTrue(bridge.enqueue(chat("666", nick="a"), now=self.now))
        self.assertFalse(bridge.enqueue(chat("666", nick="b"), now=self.now + 1))
        self.assertTrue(bridge.enqueue(chat("666", nick="c"),
                                       now=self.now + bridge.TEXT_DEDUPE_WINDOW + 1))

    def test_full_queue_evicts_oldest_low_priority(self):
        bridge.MAX_QUEUE = 2
        bridge.enqueue(chat("一"), now=self.now)
        bridge.enqueue(chat("二"), now=self.now + 1)
        self.assertTrue(bridge.enqueue(chat("三"), now=self.now + 2))
        self.assertEqual([i["content"] for i in bridge.QUEUE], ["二", "三"])
        self.assertTrue(bridge.enqueue(gift(), now=self.now + 3))
        self.assertEqual(bridge.QUEUE_STATS["evicted"], 2)

    def test_full_queue_rejects_lower_priority(self):
        bridge.MAX_QUEUE = 1
        bridge.enqueue(gift(), now=self.now)
        self.assertFalse(bridge.enqueue(chat("普通弹幕"), now=self.now + 1))

    def test_gifts_always_admitted(self):
        bridge.MAX_QUEUE = 1
        bridge.enqueue(gift(nick="A"), now=self.now)
        self.assertTrue(bridge.enqueue(gift(nick="B"), now=self.now + 1))

    def test_merge_same_user_gifts_across_queue(self):
        bridge.enqueue(gift(nick="A", item="玫瑰", value=1), now=self.now)
        bridge.enqueue(gift(nick="B", item="啤酒", value=2), now=self.now + 1)
        bridge.enqueue(gift(nick="A", item="跑车", value=100), now=self.now + 2)
        first = bridge.pick_next(now=self.now + 3)
        self.assertEqual(first["user"], "A")
        self.assertEqual(first["gift_value"], 101)
        self.assertIn("跑车", first["content"])
        self.assertEqual(bridge.pick_next(now=self.now + 3)["user"], "B")

    def test_merge_sums_same_gift_name(self):
        bridge.enqueue(gift(nick="A", item="玫瑰"), now=self.now)
        bridge.enqueue(gift(nick="A", item="玫瑰"), now=self.now + 1)
        merged = bridge.pick_next(now=self.now + 2)
        self.assertIn("2个「玫瑰」", merged["content"])


class SentenceSplitTest(unittest.TestCase):
    def split(self, deltas):
        return list(bridge.iter_sentences(iter(deltas)))

    def test_first_sentence_cuts_early_at_comma(self):
        segs = self.split(["哈哈小明你好呀", "，今天", "想聊点什么", "呢？我刚", "吃完饭。"])
        self.assertEqual(segs[0], "哈哈小明你好呀，")
        self.assertEqual("".join(segs), "哈哈小明你好呀，今天想聊点什么呢？我刚吃完饭。")

    def test_trailing_punct_stays_with_sentence(self):
        segs = self.split(["真的吗", "！", "！太棒了", "～"])
        self.assertEqual(segs, ["真的吗！！", "太棒了～"])

    def test_short_interjection_merged(self):
        segs = self.split(["嗯。", "好的呀我知道啦。"])
        self.assertEqual(segs, ["嗯。好的呀我知道啦。"])

    def test_no_punct_flushed_at_end(self):
        self.assertEqual(self.split(["没有", "标点"]), ["没有标点"])


class EmotionTagTest(unittest.TestCase):
    def run_stream(self, deltas):
        out = {}
        text = "".join(bridge.strip_emotion_stream(iter(deltas), out))
        return out.get("emotion", ""), text

    def test_parse_full_text(self):
        self.assertEqual(bridge.parse_emotion("[害羞]哎呀别夸啦"), ("shy", "哎呀别夸啦"))
        self.assertEqual(bridge.parse_emotion("【惊讶】哇！"), ("surprised", "哇！"))
        self.assertEqual(bridge.parse_emotion("没有标签"), ("", "没有标签"))

    def test_stream_tag_split_across_deltas(self):
        self.assertEqual(self.run_stream(["[", "难过", "]", "呜呜"]), ("sad", "呜呜"))

    def test_stream_no_tag_passthrough(self):
        self.assertEqual(self.run_stream(["你好", "呀"]), ("", "你好呀"))

    def test_stream_bracket_but_not_emotion(self):
        self.assertEqual(self.run_stream(["[链接]", "点这里"]), ("", "[链接]点这里"))

    def test_stream_only_tag(self):
        self.assertEqual(self.run_stream(["[开心]"]), ("happy", ""))


class StreamReplyTest(unittest.TestCase):
    def setUp(self):
        self._stream = bridge.deepseek_stream
        self._tts = bridge.local_tts
        self.spoken = []
        bridge.local_tts = lambda text, voice=None: self.spoken.append(text) or f"/tmp/{len(self.spoken)}.wav"
        bridge.REPLIES.clear()

    def tearDown(self):
        bridge.deepseek_stream = self._stream
        bridge.local_tts = self._tts

    def test_segments_pushed_and_last_marked_final(self):
        bridge.deepseek_stream = lambda *a, **k: iter(["哈哈小明你好呀，", "今天开心吗？", "😄"])
        timings = {}
        full, emotion = bridge.stream_reply("你好", "小明", "默认", None, timings)
        self.assertEqual(full, "哈哈小明你好呀，今天开心吗？😄")
        self.assertEqual(emotion, "")
        self.assertEqual(self.spoken, ["哈哈小明你好呀，", "今天开心吗？"])   # emoji 不念
        entries = list(bridge.REPLIES)
        self.assertEqual(len(entries), 2)
        self.assertEqual(len({e["reply_id"] for e in entries}), 1)
        self.assertEqual([e["final"] for e in entries], [False, True])
        self.assertEqual(entries[-1]["reply"], full)   # 气泡最终显示完整文本（含 emoji）
        self.assertIn("first_audio", timings)

    def test_emotion_tag_stripped_and_attached(self):
        bridge.deepseek_stream = lambda *a, **k: iter(["[开", "心]哈哈谢谢", "你呀，", "太好啦！"])
        full, emotion = bridge.stream_reply("你好", "小明", "默认", None, {})
        self.assertEqual(emotion, "happy")
        self.assertEqual(full, "哈哈谢谢你呀，太好啦！")
        self.assertNotIn("[", "".join(self.spoken))
        self.assertTrue(all(e["emotion"] == "happy" for e in bridge.REPLIES))

    def test_empty_reply_raises(self):
        bridge.deepseek_stream = lambda *a, **k: iter(["😄"])
        with self.assertRaises(RuntimeError):
            bridge.stream_reply("你好", "小明", "默认", None, {})


if __name__ == "__main__":
    unittest.main()
