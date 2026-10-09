/**
 * CrossWatch Feature 3 — YOLO bbox canvas overlay (no bundler).
 *
 * Global API:
 *   loadDetections(apiBase, source) → Promise<data>
 *   setYoloDetections(data)
 *   attachYoloOverlay(videoEl, canvasEl)
 *
 * Parent init snippet::
 *
 *   <div style="position:relative">
 *     <video id="player" ...></video>
 *     <canvas id="yoloCanvas" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none"></canvas>
 *   </div>
 *   <script src="feat_yolo.js"></script>
 *   <script>
 *     attachYoloOverlay(player, document.getElementById("yoloCanvas"));
 *     // on clip select:
 *     const data = await loadDetections(BASE || "", v.source);
 *     setYoloDetections(data);
 *   </script>
 */

(function (global) {
  "use strict";

  /** @type {{ status?: string, fps?: number, frames: Array }} */
  let _detData = { status: "missing", fps: 0, frames: [] };
  /** @type {HTMLVideoElement|null} */
  let _video = null;
  /** @type {HTMLCanvasElement|null} */
  let _canvas = null;
  /** @type {CanvasRenderingContext2D|null} */
  let _ctx = null;
  let _raf = 0;
  let _attached = false;

  const VEHICLE = new Set([
    "car",
    "truck",
    "bus",
    "motorcycle",
    "bicycle",
    "van",
    "vehicle",
  ]);

  function setYoloDetections(data) {
    if (!data || typeof data !== "object") {
      _detData = { status: "missing", fps: 0, frames: [] };
    } else {
      _detData = {
        status: data.status || "ok",
        fps: Number(data.fps) || 0,
        frames: Array.isArray(data.frames) ? data.frames : [],
      };
    }
    drawFrame();
  }

  async function loadDetections(apiBase, source) {
    const base = String(apiBase || "").replace(/\/$/, "");
    const url =
      (base ? base + "/" : "/") +
      "api/detections?source=" +
      encodeURIComponent(source || "");
    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || "detections load failed (" + res.status + ")");
    }
    setYoloDetections(data);
    return data;
  }

  function nearestFrame(t) {
    const frames = _detData.frames || [];
    if (!frames.length) return null;
    let best = frames[0];
    let bestD = Math.abs(Number(best.time_sec) - t);
    for (let i = 1; i < frames.length; i++) {
      const fr = frames[i];
      const d = Math.abs(Number(fr.time_sec) - t);
      if (d < bestD) {
        best = fr;
        bestD = d;
      }
    }
    return best;
  }

  function strokeColor(label) {
    const L = String(label || "").toLowerCase();
    if (L === "person") return "#e8a317"; // amber
    if (VEHICLE.has(L)) return "#ffffff";
    return "rgba(232,228,220,0.75)";
  }

  function syncCanvasSize() {
    if (!_video || !_canvas) return;
    const w = _video.clientWidth || _video.offsetWidth || 0;
    const h = _video.clientHeight || _video.offsetHeight || 0;
    if (w <= 0 || h <= 0) return;
    if (_canvas.width !== w || _canvas.height !== h) {
      _canvas.width = w;
      _canvas.height = h;
    }
  }

  function clearCanvas() {
    if (!_ctx || !_canvas) return;
    _ctx.clearRect(0, 0, _canvas.width, _canvas.height);
  }

  function drawFrame() {
    if (!_video || !_canvas || !_ctx) return;
    syncCanvasSize();
    clearCanvas();

    const frames = _detData.frames || [];
    if (!frames.length) return;

    const fr = nearestFrame(_video.currentTime || 0);
    if (!fr) return;

    const shape = fr.shape || [1080, 1920];
    const ih = Number(shape[0]) || 1080;
    const iw = Number(shape[1]) || 1920;
    const cw = _canvas.width;
    const ch = _canvas.height;
    if (iw <= 0 || ih <= 0 || cw <= 0 || ch <= 0) return;

    const sx = cw / iw;
    const sy = ch / ih;
    const dets = fr.detections || [];

    _ctx.lineWidth = 2;
    _ctx.font = '600 12px "IBM Plex Sans", sans-serif';
    _ctx.textBaseline = "bottom";

    for (let i = 0; i < dets.length; i++) {
      const d = dets[i];
      const bbox = d && d.bbox;
      if (!bbox || bbox.length < 4) continue;
      const x1 = Number(bbox[0]) * sx;
      const y1 = Number(bbox[1]) * sy;
      const x2 = Number(bbox[2]) * sx;
      const y2 = Number(bbox[3]) * sy;
      const w = Math.max(1, x2 - x1);
      const h = Math.max(1, y2 - y1);
      const color = strokeColor(d.label);

      _ctx.strokeStyle = color;
      _ctx.strokeRect(x1, y1, w, h);

      const label = String(d.label || "object");
      const conf = Number(d.confidence);
      const text =
        label + (Number.isFinite(conf) && conf > 0 ? " " + Math.round(conf * 100) + "%" : "");
      const tw = _ctx.measureText(text).width;
      const th = 14;
      const lx = x1;
      const ly = Math.max(th + 2, y1);
      _ctx.fillStyle = "rgba(18,20,26,0.75)";
      _ctx.fillRect(lx, ly - th - 2, tw + 6, th + 2);
      _ctx.fillStyle = color;
      _ctx.fillText(text, lx + 3, ly - 2);
    }
  }

  function loop() {
    drawFrame();
    if (_video && !_video.paused && !_video.ended) {
      _raf = requestAnimationFrame(loop);
    } else {
      _raf = 0;
    }
  }

  function startLoop() {
    if (_raf) return;
    _raf = requestAnimationFrame(loop);
  }

  function stopLoop() {
    if (_raf) {
      cancelAnimationFrame(_raf);
      _raf = 0;
    }
    drawFrame();
  }

  function attachYoloOverlay(videoEl, canvasEl) {
    if (!videoEl || !canvasEl) {
      throw new Error("attachYoloOverlay requires video and canvas elements");
    }
    if (_attached && _video === videoEl && _canvas === canvasEl) {
      drawFrame();
      return;
    }

    if (_video && _attached) {
      _video.removeEventListener("play", startLoop);
      _video.removeEventListener("pause", stopLoop);
      _video.removeEventListener("ended", stopLoop);
      _video.removeEventListener("timeupdate", drawFrame);
      _video.removeEventListener("seeked", drawFrame);
      stopLoop();
    }

    _video = videoEl;
    _canvas = canvasEl;
    _ctx = canvasEl.getContext("2d");
    _attached = true;

    videoEl.addEventListener("play", startLoop);
    videoEl.addEventListener("pause", stopLoop);
    videoEl.addEventListener("ended", stopLoop);
    videoEl.addEventListener("timeupdate", drawFrame);
    videoEl.addEventListener("seeked", drawFrame);

    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(() => drawFrame());
      ro.observe(videoEl);
    }

    syncCanvasSize();
    drawFrame();
    if (!videoEl.paused && !videoEl.ended) startLoop();
  }

  global.loadDetections = loadDetections;
  global.setYoloDetections = setYoloDetections;
  global.attachYoloOverlay = attachYoloOverlay;
})(typeof window !== "undefined" ? window : globalThis);
