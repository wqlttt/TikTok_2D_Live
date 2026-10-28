/* 皮套动作引擎（AvatarMotion）：让 Live2D 不再呆板。
 *
 * 分层叠加（后一层叠在前一层之上）：
 *   1. 模型自带 Idle 动作（avatar.html 里已重新打开）
 *   2. 程序化细节：眨眼、眼神扫视、重心转移、偶尔耸肩
 *   3. 状态：待机 / 思考（等 LLM 时）/ 说话（跟着音量摆头、重音点头、比划）
 *   4. 情绪表情：开心 / 害羞 / 惊讶 / 难过 / 生气 / 思考，由参数组合而成
 *   5. 事件反应：新弹幕瞥一眼、收礼物开心点头
 *
 * 写参数的时机很关键（pixi-live2d-display 每帧顺序）：
 *   beforeMotionUpdate -> motion -> afterMotionUpdate -> saveParameters
 *   -> 眨眼/呼吸/物理/pose -> beforeModelUpdate -> 渲染 -> loadParameters
 * 本模块：
 *   - beforeMotionUpdate：把「叠加型」参数复位到默认值，否则偏移量逐帧累积；
 *   - afterMotionUpdate：在 motion 结果上叠加偏移 / 覆盖表情参数。
 *     这样之后的物理（头发、裙摆）会对我们的头部动作产生跟随，而且不会被
 *     loadParameters 丢掉——旧实现在独立的 requestAnimationFrame 里写参数，
 *     时序不确定，打开 Idle 动作后会互相覆盖。
 *
 * 所有参数写入前都检查模型是否存在该参数，换模型自动降级。
 */
(function (global) {
  "use strict";

  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function rand(a, b) { return a + Math.random() * (b - a); }
  function smooth01(x) { x = clamp(x, 0, 1); return x * x * (3 - 2 * x); }

  // 平滑噪声：几个互不成倍数频率的正弦叠加，摆动永不重复，避免「节拍器感」
  function noise(t, seed) {
    return Math.sin(t * 0.731 + seed) * 0.5 +
           Math.sin(t * 1.379 + seed * 2.13) * 0.3 +
           Math.sin(t * 2.317 + seed * 3.71) * 0.2;
  }

  // 临界阻尼弹簧：所有参数都往目标值「弹」过去，不再瞬间跳变
  function Spring(value, stiffness) {
    this.v = value || 0;
    this.vel = 0;
    this.target = this.v;
    this.k = stiffness || 40;
  }
  Spring.prototype.step = function (dt) {
    // 子步长积分，保证高刚度时也稳定
    while (dt > 0) {
      var h = Math.min(dt, 1 / 120);
      var c = 2 * Math.sqrt(this.k);
      var a = this.k * (this.target - this.v) - c * this.vel;
      this.vel += a * h;
      this.v += this.vel * h;
      dt -= h;
    }
    return this.v;
  };
  Spring.prototype.kick = function (impulse) { this.vel += impulse; };

  // ---------------------------------------------------------------- 情绪预设
  // eyeOpen: 睁眼倍率；eyeSmile: 眯眼笑；brow*: 眉毛；cheek: 脸红
// mouthForm: 相对模型默认嘴型的偏移（正=更笑）。Hiyori 默认就是微笑(=1)，写绝对值会把她变严肃
  // tilt/pitch: 头部歪/抬（度）；gazeX/gazeY: 眼神偏向；sway: 摆动幅度倍率
  var EMOTIONS = {
    neutral:   { eyeOpen: 1.0,  eyeSmile: 0.0, browY: 0.0,  browAngle: 0.0,  browForm: 0.0,  mouthForm: 0, cheek: 0.0, tilt: 0,  pitch: 0,  gazeX: 0,    gazeY: 0,    sway: 1.0 },
    happy:     { eyeOpen: 0.8,  eyeSmile: 0.5,  browY: 0.35, browAngle: 0.2, browForm: 0.4,  mouthForm: 0.8,  cheek: 0.35, tilt: 4, pitch: 2,  gazeX: 0,    gazeY: 0.05, sway: 1.3 },
    shy:       { eyeOpen: 0.7,  eyeSmile: 0.4, browY: -0.1, browAngle: -0.3, browForm: -0.2, mouthForm: 0.1, cheek: 1.0, tilt: 8,  pitch: -6, gazeX: 0.45, gazeY: -0.45, sway: 0.7 },
    surprised: { eyeOpen: 1.3,  eyeSmile: 0.0, browY: 1.0,  browAngle: 0.4,  browForm: 0.6,  mouthForm: -0.8,  cheek: 0.0, tilt: 0,  pitch: 6,  gazeX: 0,    gazeY: 0.1,  sway: 0.6 },
    sad:       { eyeOpen: 0.75, eyeSmile: 0.0, browY: -0.4, browAngle: -0.8, browForm: -0.6, mouthForm: -1.6, cheek: 0.0, tilt: -5, pitch: -7, gazeX: -0.2, gazeY: -0.4, sway: 0.5 },
    angry:     { eyeOpen: 0.85, eyeSmile: 0.0, browY: -0.6, browAngle: 1.0,  browForm: 1.0,  mouthForm: -1.3, cheek: 0.45, tilt: -3, pitch: -3, gazeX: 0.2, gazeY: 0,    sway: 1.1 },
    thinking:  { eyeOpen: 0.9,  eyeSmile: 0.0, browY: 0.25, browAngle: -0.2, browForm: -0.3, mouthForm: -0.6, cheek: 0.0, tilt: 7,  pitch: 5,  gazeX: 0.45, gazeY: 0.55, sway: 0.4 }
  };
  // 校准结论（Hiyori 截图）：眯眼笑(EyeSmile)只在眼睛几乎闭上时显出「^^」；
  // BrowForm 负值=担忧八字眉，正值=压眉。所以「开心」开头先笑成 ^^ 一下再睁眼。
  var LAUGH_SECONDS = 1.1;
  var MOTION_KEEP_IDLE = 0.5, MOTION_KEEP_SPEAK = 0.25;   // 保留多少 Idle 动作的头部幅度
  var EMO_KEYS = ["eyeOpen", "eyeSmile", "browY", "browAngle", "browForm", "mouthForm", "cheek",
                  "tilt", "pitch", "gazeX", "gazeY", "sway"];

  // ---------------------------------------------------------------- 手势
  // 值是「参数半量程」的比例（-1..1），A/B 两套手臂同时写（Hiyori 用 pose 切换两套手臂）。
  // 每个手势 = 时长 + 关键姿态函数 p(0..1) -> 偏移；进出自动平滑。
  var GESTURES = {
    // 已用 Hiyori 截图校准：手臂参数为正 = 向外张开
    open:   { dur: 1.8, pose: function () { return { armL: 0.9, armR: 0.9, handL: 0.8, handR: 0.8 }; } },
    emph:   { dur: 1.1, pose: function () { return { armR: 0.75, handR: 1.0 }; } },
    emphL:  { dur: 1.1, pose: function () { return { armL: 0.75, handL: 1.0 }; } },
    wave:   { dur: 2.2, pose: function (p, t) { return { armR: 0.55 + 0.4 * Math.sin(t * 9), handR: 0.6 * Math.sin(t * 9 + 1) }; } },
    shrug:  { dur: 1.3, pose: function () { return { shoulder: 1.0, armL: 0.45, armR: 0.45, handL: 0.7, handR: 0.7 }; } }
  };
  var SPEAK_GESTURES = ["open", "emph", "emphL", "emph", "shrug"];

  // ---------------------------------------------------------------- 参数映射
  var ADDITIVE = {   // 叠加在 motion 之上（每帧先复位再叠加）
    angleX: ["ParamAngleX"], angleY: ["ParamAngleY"], angleZ: ["ParamAngleZ"],
    bodyX: ["ParamBodyAngleX"], bodyY: ["ParamBodyAngleY"], bodyZ: ["ParamBodyAngleZ"],
    armL: ["ParamArmLA", "ParamArmLB"], armR: ["ParamArmRA", "ParamArmRB"],
    // 不含 ParamHandLB/RB：Hiyori 上它们默认值就在最大值，叠加正偏移无效
    handL: ["ParamHandL"], handR: ["ParamHandR"],
    shoulder: ["ParamShoulder"]
  };
  var ABSOLUTE = {   // 直接覆盖（表情、眼睛、嘴由本模块全权负责）
    eyeOpen: ["ParamEyeLOpen", "ParamEyeROpen"],
    eyeSmile: ["ParamEyeLSmile", "ParamEyeRSmile"],
    eyeBallX: ["ParamEyeBallX"], eyeBallY: ["ParamEyeBallY"],
    browY: ["ParamBrowLY", "ParamBrowRY"],
    browAngle: ["ParamBrowLAngle", "ParamBrowRAngle"],
    browForm: ["ParamBrowLForm", "ParamBrowRForm"],
    mouthOpen: ["ParamMouthOpenY"], mouthForm: ["ParamMouthForm"],
    cheek: ["ParamCheek"]
  };
  // 叠加型里按「量程比例」给值的通道（手臂单位因模型而异，用比例更稳）
  var RELATIVE = { armL: 1, armR: 1, handL: 1, handR: 1, shoulder: 1 };

  function AvatarMotion(options) {
    options = options || {};
    this.isMotionLocked = options.isMotionLocked || function () { return false; };
    this.model = null;
    this.core = null;
    this.params = {};       // 通道 -> [{idx, def, min, max}]
    this.debug = null;      // 测试用：{ParamId: value} 最后覆盖
    this._bound = null;
    this._reset();
  }

  AvatarMotion.prototype._reset = function () {
    var now = performance.now() / 1000;
    this.t = 0;
    this.lastTs = 0;
    this.speaking = false;
    this.thinking = false;
    this.level = 0; this.levelFast = 0; this.levelSlow = 0;
    this.mouth = new Spring(0, 900);
    this.lastNod = 0;
    this.emotion = "neutral";
    this.emotionUntil = 0;
    this.laughUntil = 0;
    this.emo = {};
    for (var i = 0; i < EMO_KEYS.length; i++) {
      this.emo[EMO_KEYS[i]] = new Spring(EMOTIONS.neutral[EMO_KEYS[i]], 30);
    }
    this.thinkW = new Spring(0, 12);
    this.speakW = new Spring(0, 10);
    // 眼神：眼球快（扫视），头慢（跟随）
    this.gazeX = new Spring(0, 260); this.gazeY = new Spring(0, 260);
    this.nextSaccade = 0;
    this.lookUntil = 0; this.lookX = 0; this.lookY = 0;
    this.headX = new Spring(0, 22); this.headY = new Spring(0, 26); this.headZ = new Spring(0, 22);
    this.nod = new Spring(0, 140);         // 点头冲量
    this.bodyX = new Spring(0, 9); this.bodyZ = new Spring(0, 9); this.bodyY = new Spring(0, 9);
    this.weightX = 0; this.nextWeightShift = now + rand(4, 8);
    this.arm = { armL: new Spring(0, 30), armR: new Spring(0, 30), handL: new Spring(0, 40),
                 handR: new Spring(0, 40), shoulder: new Spring(0, 60) };
    this.gesture = null;     // {name, start, dur}
    this.nextIdleGesture = now + rand(10, 18);
    // 眨眼
    this.nextBlink = now + rand(1.5, 4);
    this.blinkStart = -1;
    this.doubleBlink = false;
  };

  // ------------------------------------------------------------ 绑定模型

  AvatarMotion.prototype.attach = function (model) {
    this.detach();
    if (!model || !model.internalModel) return;
    var im = model.internalModel;
    this.model = model;
    this.core = im.coreModel;
    this._reset();
    // 关掉内置眨眼（它只在没有 motion 时运行，打开 Idle 后会失效），由本模块接管
    im.eyeBlink = undefined;
    this.params = {};
    var count = this.core.getParameterCount();
    var self = this;
    function scan(table) {
      Object.keys(table).forEach(function (ch) {
        var list = [];
        table[ch].forEach(function (id) {
          var idx = self.core.getParameterIndex(id);
          // 不存在的 id 会被分配到 >= count 的「虚拟下标」
          if (idx >= 0 && idx < count) {
            list.push({
              id: id, idx: idx,
              def: self.core.getParameterDefaultValue(idx),
              min: self.core.getParameterMinimumValue(idx),
              max: self.core.getParameterMaximumValue(idx)
            });
          }
        });
        if (list.length) self.params[ch] = list;
      });
    }
    // 口型/眨眼优先用模型 model3.json 里声明的 LipSync / EyeBlink 参数组
    // （例如 Mao 的口型参数不是 ParamMouthOpenY，旧实现在 Mao 上嘴从来不动）
    var mm = im.motionManager || {};
    var abs = {};
    Object.keys(ABSOLUTE).forEach(function (k) { abs[k] = ABSOLUTE[k]; });
    if (mm.lipSyncIds && mm.lipSyncIds.length) abs.mouthOpen = mm.lipSyncIds.slice();
    if (mm.eyeBlinkIds && mm.eyeBlinkIds.length) abs.eyeOpen = mm.eyeBlinkIds.slice();
    scan(ADDITIVE);
    scan(abs);
    this._bound = {
      before: function () { self._beforeMotion(); },
      after: function () { self._afterMotion(); }
    };
    im.on("beforeMotionUpdate", this._bound.before);
    im.on("afterMotionUpdate", this._bound.after);
  };

  AvatarMotion.prototype.detach = function () {
    if (this.model && this.model.internalModel && this._bound) {
      try {
        this.model.internalModel.off("beforeMotionUpdate", this._bound.before);
        this.model.internalModel.off("afterMotionUpdate", this._bound.after);
      } catch (e) { /* 模型已销毁 */ }
    }
    this.model = null; this.core = null; this._bound = null; this.params = {};
  };

  AvatarMotion.prototype.supports = function (channel) { return !!this.params[channel]; };

  // ------------------------------------------------------------ 外部输入

  AvatarMotion.prototype.setSpeaking = function (on) {
    if (on && !this.speaking) this.lastNod = 0;
    this.speaking = !!on;
    if (!on) this.mouth.target = 0;
  };
  // 每帧音量（0..1），同时驱动嘴和动作幅度
  AvatarMotion.prototype.setLevel = function (v) {
    this.level = clamp(v, 0, 1);
    this.mouth.target = this.speaking ? this.level : 0;
  };
  AvatarMotion.prototype.setThinking = function (on) { this.thinking = !!on; };

  AvatarMotion.prototype.setEmotion = function (name, seconds) {
    if (!EMOTIONS[name]) return;
    if (name === "happy" && this.emotion !== "happy") this.laughUntil = this._now() + LAUGH_SECONDS;
    this.emotion = name;
    this.emotionUntil = name === "neutral" ? 0 : this._now() + (seconds || 4);
  };

  // 新一句开始说：按概率比划一下
  AvatarMotion.prototype.onSentence = function (index) {
    var p = index === 0 ? 0.75 : 0.45;
    if (Math.random() < p) {
      this.gesture_(SPEAK_GESTURES[Math.floor(Math.random() * SPEAK_GESTURES.length)]);
    }
  };

  // 眼神瞥向某处（x,y ∈ -1..1，右/上为正）
  AvatarMotion.prototype.glance = function (x, y, seconds) {
    if (this.speaking && Math.random() < 0.6) return;   // 说话时多数时候专心看镜头
    this.lookX = clamp(x, -1, 1); this.lookY = clamp(y, -1, 1);
    this.lookUntil = this._now() + (seconds || 1.0);
    this.nextSaccade = this.lookUntil + rand(0.2, 0.6);
  };

  // 事件反应：gift(value) / danmaku(yFrac)
  AvatarMotion.prototype.react = function (kind, info) {
    info = info || {};
    if (kind === "gift") {
      var big = (info.value || 0) >= 100;
      if (big) {
        this.setEmotion("surprised", 0.9);
        var self = this;
        setTimeout(function () { self.setEmotion("happy", 4); self.gesture_("wave"); }, 900);
      } else {
        this.setEmotion("happy", 3);
        if (Math.random() < 0.5) this.gesture_("wave");
      }
      this.nod.kick(-190);
    } else if (kind === "danmaku") {
      // 弹幕从右侧飘入，在舞台上方
      this.glance(rand(0.45, 0.8), 0.35 + 0.3 * (1 - (info.yFrac || 0.2)), rand(0.7, 1.3));
      if (!this.speaking && Math.random() < 0.35) this.nod.kick(-80);
    }
  };

  AvatarMotion.prototype.gesture_ = function (name) {
    if (!GESTURES[name]) return;
    if (this.gesture && this._now() - this.gesture.start < this.gesture.dur * 0.7) return;
    this.gesture = { name: name, start: this._now(), dur: GESTURES[name].dur };
  };

  AvatarMotion.prototype._now = function () { return performance.now() / 1000; };

  // ------------------------------------------------------------ 每帧

  AvatarMotion.prototype._beforeMotion = function () {
    var core = this.core;
    if (!core) return;
    var self = this;
    Object.keys(ADDITIVE).forEach(function (ch) {
      var list = self.params[ch];
      if (!list) return;
      for (var i = 0; i < list.length; i++) core.setParameterValueByIndex(list[i].idx, list[i].def);
    });
  };

  AvatarMotion.prototype._afterMotion = function () {
    var core = this.core;
    if (!core) return;
    var now = this._now();
    var dt = this.lastTs ? clamp(now - this.lastTs, 0, 0.1) : 1 / 60;
    this.lastTs = now;
    var t = now;
    var locked = this.isMotionLocked();

    // ---- 音量包络 + 重音检测（音量突增 -> 点头）
    this.levelFast += (this.level - this.levelFast) * (this.level > this.levelFast ? 0.5 : 0.12);
    this.levelSlow += (this.level - this.levelSlow) * 0.03;
    if (this.speaking && this.levelFast - this.levelSlow > 0.13 && now - this.lastNod > 0.38) {
      this.lastNod = now;
      this.nod.kick(-rand(110, 190));   // 峰值约 3~6 度
      if (Math.random() < 0.3) this.arm.handR.kick(rand(1.5, 3));
    }
    if (!this.speaking) this.levelFast *= 0.9;

    // ---- 状态权重
    this.speakW.target = this.speaking ? 1 : 0;
    this.thinkW.target = (this.thinking && !this.speaking) ? 1 : 0;
    var sw = this.speakW.step(dt), tw = this.thinkW.step(dt);

    // ---- 情绪：目标 = 当前情绪预设，与「思考」预设按权重混合
    if (this.emotionUntil && now > this.emotionUntil) { this.emotion = "neutral"; this.emotionUntil = 0; }
    var base = EMOTIONS[this.emotion] || EMOTIONS.neutral;
    var think = EMOTIONS.thinking;
    var e = {};
    for (var i = 0; i < EMO_KEYS.length; i++) {
      var k = EMO_KEYS[i];
      this.emo[k].target = base[k] * (1 - tw) + think[k] * tw;
    }
    if (now < this.laughUntil) {          // 笑成 ^^
      this.emo.eyeOpen.target = 0.05;
      this.emo.eyeSmile.target = 1;
    }
    for (var j = 0; j < EMO_KEYS.length; j++) e[EMO_KEYS[j]] = this.emo[EMO_KEYS[j]].step(dt);

    // ---- 眼神扫视
    if (now < this.lookUntil) {
      this.gazeX.target = this.lookX; this.gazeY.target = this.lookY;
    } else if (now >= this.nextSaccade) {
      if (sw > 0.5) {
        // 说话：大部分时间看镜头，小幅游移
        this.gazeX.target = rand(-0.15, 0.15); this.gazeY.target = rand(-0.08, 0.1);
        this.nextSaccade = now + rand(0.8, 2.2);
      } else if (tw > 0.5) {
        this.gazeX.target = e.gazeX + rand(-0.15, 0.15); this.gazeY.target = e.gazeY + rand(-0.1, 0.1);
        this.nextSaccade = now + rand(0.6, 1.4);
      } else {
        // 待机：时而看镜头，时而四处看看
        var center = Math.random() < 0.45;
        this.gazeX.target = center ? rand(-0.1, 0.1) : rand(-0.6, 0.6);
        this.gazeY.target = center ? rand(-0.05, 0.08) : rand(-0.35, 0.3);
        this.nextSaccade = now + rand(0.9, 3.2);
      }
      this.gazeX.target += e.gazeX * (1 - tw) * 0.8;
      this.gazeY.target += e.gazeY * (1 - tw) * 0.8;
    }
    var gx = this.gazeX.step(dt), gy = this.gazeY.step(dt);

    // ---- 眨眼（偶尔连眨；说话时更频繁）
    var blink = 1;
    if (this.blinkStart < 0 && now >= this.nextBlink) { this.blinkStart = now; }
    if (this.blinkStart >= 0) {
      var bt = now - this.blinkStart;
      var close = 0.07, hold = 0.04, open = 0.13;
      if (bt < close) blink = 1 - bt / close;
      else if (bt < close + hold) blink = 0;
      else if (bt < close + hold + open) blink = (bt - close - hold) / open;
      else {
        this.blinkStart = -1;
        if (!this.doubleBlink && Math.random() < 0.18) {
          this.doubleBlink = true; this.nextBlink = now + 0.12;
        } else {
          this.doubleBlink = false;
          this.nextBlink = now + (sw > 0.5 ? rand(1.4, 3.5) : rand(2.2, 5.5));
        }
      }
    }
    // 眼神大幅移动时顺带眨眼更自然（真人扫视常伴随眨眼）——用很小概率实现
    blink = smooth01(blink);

    // ---- 重心转移（待机慢慢左右换重心）
    if (now >= this.nextWeightShift) {
      this.weightX = rand(-1, 1) * 4;
      this.nextWeightShift = now + rand(5, 11);
    }

    // ---- 待机小动作：偶尔耸肩/摊手
    if (!locked && sw < 0.3 && tw < 0.3 && now >= this.nextIdleGesture) {
      this.gesture_(Math.random() < 0.6 ? "shrug" : "emphL");
      this.nextIdleGesture = now + rand(12, 22);
    }

    // ---- 头部：噪声摆动（随音量放大）+ 眼神跟随 + 情绪姿态 + 点头冲量
    var amp = e.sway * (0.6 + sw * (0.5 + this.levelFast * 1.6)) * (locked ? 0.4 : 1);
    var idleAmp = 1 - sw * 0.5;
    this.headX.target = gx * 12 + noise(t * (0.6 + sw * 0.9), 1.1) * 3.2 * amp;
    this.headY.target = gy * 9 + e.pitch + noise(t * (0.5 + sw * 1.1), 2.7) * 2.2 * amp;
    this.headZ.target = e.tilt + noise(t * (0.45 + sw * 0.8), 4.3) * (2.2 + 2.3 * sw) * amp * idleAmp
                        + noise(t * 1.7, 9.1) * 2.2 * sw * this.levelFast;
    var hx = this.headX.step(dt), hy = this.headY.step(dt), hz = this.headZ.step(dt);
    this.nod.target = 0;
    var nod = this.nod.step(dt);

    // ---- 身体：比头慢半拍跟随（follow-through），叠加重心
    this.bodyX.target = hx * 0.35 + this.weightX;
    this.bodyZ.target = hz * 0.35 + this.weightX * 0.4;
    this.bodyY.target = (hy + nod) * 0.15;
    var bx = this.bodyX.step(dt), bz = this.bodyZ.step(dt), by = this.bodyY.step(dt);

    // ---- 手势
    var g = { armL: 0, armR: 0, handL: 0, handR: 0, shoulder: 0 };
    if (this.gesture && !locked) {
      var gp = (now - this.gesture.start) / this.gesture.dur;
      if (gp >= 1) this.gesture = null;
      else {
        var env = smooth01(gp / 0.25) * smooth01((1 - gp) / 0.3);
        var pose = GESTURES[this.gesture.name].pose(gp, now - this.gesture.start);
        for (var gk in pose) g[gk] = pose[gk] * env;
      }
    }
    // 说话时双手随音量轻微起伏（比原来的固定正弦自然得多）
    if (sw > 0.01 && !locked) {
      g.handL += sw * (0.12 + 0.25 * this.levelFast) * (0.5 + 0.5 * noise(t * 1.3, 5.5));
      g.handR += sw * (0.12 + 0.25 * this.levelFast) * (0.5 + 0.5 * noise(t * 1.1, 7.7));
    }
    var arm = {};
    for (var ak in this.arm) { this.arm[ak].target = g[ak]; arm[ak] = this.arm[ak].step(dt); }

    // ---- 嘴
    var mouth = this.mouth.step(dt);

    // ================================================== 写入
    // 先压缩模型自带 Idle 动作对头/身体的幅度：Hiyori 的 Idle 本身就能把头甩到 ±25°，
    // 再叠加内置呼吸(±7°)和本层，会顶到 ±30° 的极限、显得乱晃。
    // 说话时由本层主导头部；礼物 motion 播放时不压缩，让动作完整呈现。
    var keep = locked ? 1 : (MOTION_KEEP_IDLE + (MOTION_KEEP_SPEAK - MOTION_KEEP_IDLE) * sw);
    if (keep < 1) {
      this._scaleMotion("angleX", keep); this._scaleMotion("angleY", keep); this._scaleMotion("angleZ", keep);
      this._scaleMotion("bodyX", keep); this._scaleMotion("bodyY", keep); this._scaleMotion("bodyZ", keep);
    }
    this._add("angleX", hx);
    this._add("angleY", hy + nod);
    this._add("angleZ", hz);
    this._add("bodyX", bx);
    this._add("bodyY", by);
    this._add("bodyZ", bz);
    for (var rk in RELATIVE) this._add(rk, arm[rk]);

    this._set("eyeOpen", e.eyeOpen * blink);
    this._set("eyeSmile", e.eyeSmile);
    this._set("eyeBallX", gx);
    this._set("eyeBallY", gy);
    this._set("browY", e.browY);
    this._set("browAngle", e.browAngle);
    this._set("browForm", e.browForm);
    this._set("mouthOpen", clamp(mouth, 0, 1));
    // 说话时嘴型稍收，避免大笑嘴型配张嘴显得夸张
    this._setRel("mouthForm", e.mouthForm * (1 - 0.3 * sw));
    this._set("cheek", e.cheek);

    if (this.debug) {
      for (var id in this.debug) {
        var di = core.getParameterIndex(id);
        if (di >= 0 && di < core.getParameterCount()) core.setParameterValueByIndex(di, this.debug[id]);
      }
    }
  };

  // 叠加：RELATIVE 通道按「半量程比例」换算，其余按参数原单位（度）
  AvatarMotion.prototype._add = function (ch, v) {
    var list = this.params[ch];
    if (!list || !v) return;
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      var delta = RELATIVE[ch] ? v * (p.max - p.min) / 2 : v;
      var cur = this.core.getParameterValueByIndex(p.idx);
      this.core.setParameterValueByIndex(p.idx, clamp(cur + delta, p.min, p.max));
    }
  };

  AvatarMotion.prototype._set = function (ch, v) {
    var list = this.params[ch];
    if (!list) return;
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      this.core.setParameterValueByIndex(p.idx, clamp(v, p.min, p.max));
    }
  };

  // 把 motion 写出的值向默认值收缩：v = def + (v - def) * k
  AvatarMotion.prototype._scaleMotion = function (ch, k) {
    var list = this.params[ch];
    if (!list) return;
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      var cur = this.core.getParameterValueByIndex(p.idx);
      this.core.setParameterValueByIndex(p.idx, p.def + (cur - p.def) * k);
    }
  };

  // 相对模型默认值写入
  AvatarMotion.prototype._setRel = function (ch, v) {
    var list = this.params[ch];
    if (!list) return;
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      this.core.setParameterValueByIndex(p.idx, clamp(p.def + v, p.min, p.max));
    }
  };

  AvatarMotion.EMOTIONS = Object.keys(EMOTIONS);
  AvatarMotion.GESTURES = Object.keys(GESTURES);
  global.AvatarMotion = AvatarMotion;
})(window);
