# 大礼物预渲染视频

把大礼物的演出视频放在这个目录，例如：

```text
static/videos/large_gift.webm
```

然后在 `config/gift_actions.json` 的 `large_gift.timeline` 里使用：

```json
{
  "at": 0,
  "type": "video",
  "src": "/static/videos/large_gift.webm",
  "duration": 5.0,
  "audio": false,
  "fit": "contain",
  "loop": false
}
```

建议：

- 透明背景用 **WebM + VP9 alpha**，OBS 浏览器源支持较好
- 不透明背景用 MP4/H.264 也可以
- 分辨率按直播间画面比例做，推荐 1080×1920 或 720×1280
- `fit: "contain"` 保留完整画面；`fit: "cover"` 铺满全屏（可能裁切）
- `loop: false` 表示 5 秒只播一次；`duration` 建议和视频时长一致
- `audio: false` 时视频静音，避免和主播/TTS 抢声音
- 视频文件不要太大，开播前预加载
- 视频加载失败时 `gift-effects.js` 会打印 `[GiftEffects] video failed`，并在 `duration` 后自动清理
