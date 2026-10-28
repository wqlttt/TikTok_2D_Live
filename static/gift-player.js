/* 礼物动作播放器：消费 bridge 下发的 ActionPlan。
 *
 * 设计原则：
 * - 只认 ActionPlan 的 timeline，不关心礼物规则；
 * - 渲染相关逻辑集中在这里，avatar.html 只负责 setModel/play；
 * - 新增 step 类型时，在 _exec() 里加一个 case。
 */
(function (global) {
  "use strict";

  var RESET_PARAMS = [
    "ParamAngleX", "ParamAngleY", "ParamAngleZ",
    "ParamBodyAngleX", "ParamBodyAngleY", "ParamBodyAngleZ",
    "ParamHandL", "ParamHandR", "ParamShoulder"
  ];

  function GiftPlayer(options) {
    options = options || {};
    this.model = null;
    this.coreModel = null;
    this.motionManager = null;
    this.stage = options.stage || null;
    this.active = false;
    this.hasMotion = false;
    this.duration = 0;
    this._timers = [];
    this._token = 0;

    if (options.model) this.setModel(options.model, options.stage);
    if (global.GiftEffects) global.GiftEffects.init(this.stage);
  }

  GiftPlayer.prototype.setModel = function (model, stage) {
    this.stop();
    this.model = model || null;
    this.stage = stage || this.stage || document.getElementById("stage");
    this.coreModel = model && model.internalModel ? model.internalModel.coreModel : null;
    this.motionManager = model && model.internalModel ? model.internalModel.motionManager : null;
    if (global.GiftEffects) global.GiftEffects.init(this.stage);
  };

  GiftPlayer.prototype.isActive = function () {
    return this.active;
  };

  // 只有动作时间轴里真的包含 motion 时，才需要暂停程序化待机手势。
  // 大礼物这种纯特效演出不会锁住 Live2D。
  GiftPlayer.prototype.isMotionActive = function () {
    return this.active && this.hasMotion;
  };

  GiftPlayer.prototype.play = function (action) {
    if (!action || !this.model) return false;

    this.stop();
    var token = ++this._token;
    var self = this;

    this.active = true;
    this.duration = Math.max(0.5, Number(action.duration) || 2);

    var timeline = Array.isArray(action.timeline) ? action.timeline : [];
    this.hasMotion = timeline.some(function (step) {
      return String(step && step.type || "").toLowerCase() === "motion";
    });
    timeline.forEach(function (step) {
      if (!step || typeof step !== "object") return;
      var at = Math.max(0, Number(step.at) || 0);
      var timer = setTimeout(function () {
        if (token === self._token) self._exec(step);
      }, at * 1000);
      self._timers.push(timer);
    });

    var endTimer = setTimeout(function () {
      if (token === self._token) self._finish();
    }, this.duration * 1000);
    this._timers.push(endTimer);
    return true;
  };

  GiftPlayer.prototype.stop = function () {
    var hadMotion = this.hasMotion;
    this._token++;
    this._timers.forEach(clearTimeout);
    this._timers = [];
    this.active = false;
    this.hasMotion = false;

    if (hadMotion && this.motionManager && this.motionManager.stopAllMotions) {
      try { this.motionManager.stopAllMotions(); } catch (e) { /* noop */ }
    }
    if (global.GiftEffects) global.GiftEffects.clear();
    if (hadMotion) this._resetPose();
  };

  GiftPlayer.prototype._finish = function () {
    var hadMotion = this.hasMotion;
    this._token++;
    this._timers = [];
    this.active = false;
    this.hasMotion = false;

    if (hadMotion && this.motionManager && this.motionManager.stopAllMotions) {
      try { this.motionManager.stopAllMotions(); } catch (e) { /* noop */ }
    }
    if (global.GiftEffects) global.GiftEffects.clear();
    if (hadMotion) this._resetPose();
  };

  GiftPlayer.prototype._exec = function (step) {
    if (!step || !step.type) return;
    var type = String(step.type).toLowerCase();

    try {
      switch (type) {
        case "motion":
          if (this.model && this.model.motion) {
            var p = this.model.motion(
              step.group || "TapBody",
              Number(step.index) || 0,
              Number(step.priority) || 3
            );
            if (p && p.catch) p.catch(function () { /* 模型没有该动作组时静默 */ });
          }
          break;

        case "expression":
          if (this.model && this.model.expression && step.id) {
            var e = this.model.expression(step.id);
            if (e && e.catch) e.catch(function () { /* 模型没有该表情时静默 */ });
          }
          break;

        case "effect":
          if (global.GiftEffects) {
            global.GiftEffects.spawn(step.name, step.duration, this.stage);
          }
          break;

        case "video":
          if (global.GiftEffects) {
            global.GiftEffects.video(step.src, step.duration, this.stage, {
              audio: !!step.audio,
              fit: step.fit,
              loop: !!step.loop
            });
          }
          break;

        case "toast":
          if (global.GiftEffects) {
            global.GiftEffects.toast(step.text, step.duration, this.stage);
          }
          break;

        case "speak":
          // 预留：默认不在这里触发 TTS，避免和 bridge 的 DeepSeek/TTS 重复。
          // 后续可以监听 gift:speak 事件，接入独立语音通道。
          document.dispatchEvent(new CustomEvent("gift:speak", { detail: step }));
          break;

        default:
          // 未知类型交给外部扩展，不阻塞整条时间轴。
          document.dispatchEvent(new CustomEvent("gift:step", { detail: step }));
          break;
      }
    } catch (err) {
      console.warn("[GiftPlayer] step failed:", step, err);
    }
  };

  GiftPlayer.prototype._resetPose = function () {
    if (!this.coreModel) return;
    RESET_PARAMS.forEach(function (id) {
      try { this.coreModel.setParameterValueById(id, 0); } catch (e) { /* 参数不存在 */ }
    }, this);
  };

  global.GiftPlayer = GiftPlayer;
})(window);
