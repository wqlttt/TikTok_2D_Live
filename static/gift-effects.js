/* 礼物特效：只负责粒子/浮层，不依赖 Live2D。
 *
 * 大礼物粒子优先使用 canvas-confetti（ISC，https://github.com/catdad/canvas-confetti）；
 * 如果库没加载成功，才回退到下面的 CSS 特效。
 * 后续加新特效：在 spawnWithConfetti() 或 spawn() 的 switch 里加一个 case 即可。
 */
(function (global) {
  "use strict";

  var COLORS = ["#ff5f87", "#ffd166", "#7bdff2", "#b388ff", "#7cf5a5", "#ff9f1c"];
  var layer = null;
  var toastEl = null;
  var toastTimer = null;
  var confettiCanvas = null;
  var confettiFx = null;
  var shapeCache = {};
  var fxToken = 0;
  var videoNodes = [];

  function init(stage) {
    stage = stage || document.getElementById("stage");
    if (!stage) return;

    layer = stage.querySelector("#fx-layer");
    if (!layer) {
      layer = document.createElement("div");
      layer.id = "fx-layer";
      stage.appendChild(layer);
    }

    toastEl = stage.querySelector("#gift-toast");
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.id = "gift-toast";
      stage.appendChild(toastEl);
    }

    confettiCanvas = stage.querySelector("#fx-canvas");
    if (global.confetti && confettiCanvas && !confettiFx) {
      try {
        confettiFx = global.confetti.create(confettiCanvas, {
          resize: true,
          useWorker: true
        });
      } catch (err) {
        confettiFx = null;
        console.warn("[GiftEffects] canvas-confetti init failed:", err);
      }
    }
  }

  function ensure(stage) {
    if (!layer || !toastEl) init(stage);
  }

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  function node(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = text;
    return el;
  }

  function removeLater(el, seconds) {
    setTimeout(function () {
      if (el && el.parentNode) el.parentNode.removeChild(el);
    }, Math.max(0, seconds) * 1000);
  }

  function fire(options) {
    if (!confettiFx) return false;
    try {
      confettiFx(options);
      return true;
    } catch (err) {
      console.warn("[GiftEffects] confetti burst failed:", err);
      return false;
    }
  }

  function shape(text) {
    if (!global.confetti || !global.confetti.shapeFromText) return "circle";
    if (!shapeCache[text]) {
      try {
        shapeCache[text] = global.confetti.shapeFromText({ text: text, scalar: 2 });
      } catch (err) {
        shapeCache[text] = "circle";
      }
    }
    return shapeCache[text];
  }

  // 优先用 canvas-confetti 渲染；失败时回退到下面的 CSS 特效。
  function spawnWithConfetti(name, duration) {
    if (!confettiFx) return false;
    var d = Math.max(0.8, Number(duration) || 2);
    var token = ++fxToken;
    var colors = COLORS.slice();

    switch (name) {
      case "fireworks": {
        if (!fire({
          particleCount: 90,
          spread: 360,
          origin: { x: 0.5, y: 0.45 },
          startVelocity: 34,
          ticks: 130,
          gravity: 0.8,
          scalar: 1.1,
          colors: colors
        })) return false;
        var endAt = Date.now() + Math.min(3000, d * 1000);
        var timer = setInterval(function () {
          if (token !== fxToken || Date.now() > endAt) {
            clearInterval(timer);
            return;
          }
          fire({
            particleCount: 20,
            angle: rand(60, 120),
            spread: 72,
            origin: { x: rand(0.12, 0.88), y: rand(0.16, 0.5) },
            startVelocity: rand(22, 40),
            ticks: 110,
            gravity: 0.9,
            scalar: rand(0.7, 1.25),
            colors: colors
          });
        }, 220);
        return true;
      }

      case "confetti": {
        if (!fire({
          particleCount: 130,
          spread: 90,
          origin: { y: 0.58 },
          startVelocity: 45,
          ticks: 210,
          scalar: 1.1,
          colors: colors
        })) return false;
        setTimeout(function () {
          if (token !== fxToken) return;
          fire({
            particleCount: 65,
            angle: 60,
            spread: 60,
            origin: { x: 0, y: 0.7 },
            startVelocity: 55,
            ticks: 180,
            colors: colors
          });
          fire({
            particleCount: 65,
            angle: 120,
            spread: 60,
            origin: { x: 1, y: 0.7 },
            startVelocity: 55,
            ticks: 180,
            colors: colors
          });
        }, 160);
        return true;
      }

      case "hearts":
        return fire({
          particleCount: 42,
          spread: 70,
          origin: { y: 0.8 },
          startVelocity: 35,
          gravity: 0.7,
          ticks: 190,
          scalar: 1.8,
          shapes: [shape("❤️")]
        });

      case "rose_rain":
        return fire({
          particleCount: 48,
          angle: 90,
          spread: 120,
          origin: { y: -0.1 },
          startVelocity: 12,
          gravity: 0.5,
          ticks: 300,
          scalar: 2.0,
          shapes: [shape("🌹")]
        });

      case "sparkles":
        return fire({
          particleCount: 46,
          spread: 360,
          origin: { y: 0.5 },
          startVelocity: 22,
          gravity: 0.4,
          ticks: 130,
          scalar: 1.4,
          shapes: [shape("✦")]
        });

      default:
        return false;
    }
  }

  function hearts(duration) {
    for (var i = 0; i < 14; i++) {
      var el = node("span", "fx-heart", "❤");
      el.style.left = rand(4, 92) + "%";
      el.style.fontSize = rand(18, 32) + "px";
      el.style.animationDuration = rand(1.5, duration) + "s";
      layer.appendChild(el);
      removeLater(el, duration + 0.4);
    }
  }

  function roseRain(duration) {
    for (var i = 0; i < 22; i++) {
      var el = node("span", "fx-rose", "🌹");
      el.style.left = rand(2, 94) + "%";
      el.style.fontSize = rand(20, 34) + "px";
      el.style.animationDuration = rand(1.9, duration) + "s";
      el.style.animationDelay = rand(0, 0.5) + "s";
      layer.appendChild(el);
      removeLater(el, duration + 0.8);
    }
  }

  function confetti(duration) {
    for (var i = 0; i < 42; i++) {
      var el = node("i", "fx-confetti");
      el.style.left = rand(2, 98) + "%";
      el.style.background = COLORS[i % COLORS.length];
      el.style.setProperty("--dx", rand(-110, 110).toFixed(1) + "px");
      el.style.animationDuration = rand(1.7, duration) + "s";
      el.style.animationDelay = rand(0, 0.35) + "s";
      layer.appendChild(el);
      removeLater(el, duration + 0.6);
    }
  }

  function fireworks(duration) {
    var bursts = 3;
    for (var b = 0; b < bursts; b++) {
      var cx = rand(14, 86);
      var cy = rand(16, 55);
      for (var i = 0; i < 18; i++) {
        var el = node("i", "fx-spark");
        var angle = rand(0, Math.PI * 2);
        var distance = rand(35, 105);
        el.style.left = cx + "%";
        el.style.top = cy + "%";
        el.style.background = COLORS[(i + b) % COLORS.length];
        el.style.setProperty("--dx", (Math.cos(angle) * distance).toFixed(1) + "px");
        el.style.setProperty("--dy", (Math.sin(angle) * distance).toFixed(1) + "px");
        el.style.animationDuration = rand(0.65, 1.05) + "s";
        el.style.animationDelay = (b * 0.12) + "s";
        layer.appendChild(el);
        removeLater(el, Math.max(1.0, duration));
      }
    }
  }

  function sparkles(duration) {
    for (var i = 0; i < 24; i++) {
      var el = node("span", "fx-star", "✦");
      el.style.left = rand(8, 92) + "%";
      el.style.top = rand(18, 78) + "%";
      el.style.setProperty("--dx", rand(-24, 24).toFixed(1) + "px");
      el.style.setProperty("--dy", rand(-42, -12).toFixed(1) + "px");
      el.style.animationDuration = rand(0.9, duration) + "s";
      el.style.animationDelay = rand(0, 0.4) + "s";
      layer.appendChild(el);
      removeLater(el, duration + 0.5);
    }
  }

  function spawn(name, duration, stage) {
    ensure(stage);
    if (!layer) return;
    var effectName = String(name || "").toLowerCase();
    var d = Math.max(0.5, Number(duration) || 2);

    // 首选 canvas-confetti（开源库，粒子质量/性能更稳定）。
    if (spawnWithConfetti(effectName, d)) return;

    // 离线或 CDN 失败时的兜底：纯 CSS 特效。
    switch (effectName) {
      case "hearts":
        hearts(d);
        break;
      case "rose_rain":
        roseRain(d);
        break;
      case "confetti":
        confetti(d);
        break;
      case "fireworks":
        fireworks(d);
        break;
      case "sparkles":
      default:
        sparkles(d);
        break;
    }
  }

  // 5 秒大礼物视频入口：options = { audio, fit: "contain"|"cover", loop }
  function video(src, duration, stage, options) {
    ensure(stage);
    if (!layer || !src) return false;
    options = options || {};

    var el = document.createElement("video");
    el.className = "fx-video" + (options.fit === "cover" ? " fx-video-cover" : "");
    el.src = src;
    el.autoplay = true;
    el.playsInline = true;
    el.preload = "auto";
    el.loop = !!options.loop;
    el.muted = !options.audio;

    function dispose() {
      if (el.parentNode) el.parentNode.removeChild(el);
      videoNodes = videoNodes.filter(function (item) { return item !== el; });
    }

    el.addEventListener("ended", dispose);
    el.addEventListener("error", function () {
      console.warn("[GiftEffects] video failed:", src);
      dispose();
    });

    // 视频挂到 body，避免被 avatar 卡片裁切
    document.body.appendChild(el);
    videoNodes.push(el);

    var playing = el.play();
    if (playing && playing.catch) {
      playing.catch(function (err) {
        console.warn("[GiftEffects] video autoplay blocked:", err);
      });
    }
    setTimeout(dispose, Math.max(0.5, Number(duration) || 5) * 1000);
    return true;
  }

  function toast(text, duration, stage) {
    ensure(stage);
    if (!toastEl) return;
    toastEl.textContent = text || "";
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove("show");
    }, Math.max(1, Number(duration) || 2.4) * 1000);
  }

  function clear() {
    fxToken++;
    if (confettiFx && confettiFx.reset) {
      try { confettiFx.reset(); } catch (err) { /* noop */ }
    }
    videoNodes.forEach(function (el) {
      if (el && el.parentNode) el.parentNode.removeChild(el);
    });
    videoNodes = [];
    if (layer) layer.textContent = "";
    if (toastEl) {
      toastEl.classList.remove("show");
      toastEl.textContent = "";
    }
    clearTimeout(toastTimer);
  }

  global.GiftEffects = {
    init: init,
    spawn: spawn,
    video: video,
    toast: toast,
    clear: clear
  };
})(window);
