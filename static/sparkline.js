/* pd-system-monitor sparklines.
   Subscribes to "system-monitor-update" events via the dashboard shell's
   PD.SSE helper (shared visibility-aware EventSource — see
   personal-dashboard/docs/knowledge/plugin-sse-from-js.md), maintains
   per-metric ring buffers in localStorage, and renders compact inline SVG
   polylines into elements with class .pd-sysmon-spark and a data-metric
   attribute.

   Buffer: 60 samples per metric (5 min @ 5s cadence). ~5 KB total.
*/
(function () {
  "use strict";

  const STORAGE_KEY = "pd-sysmon-spark-v1";
  const MAX_SAMPLES = 60;

  // Map each rendered metric key to a function that pulls a numeric value
  // from the parsed event data dict.
  const METRIC_EXTRACTORS = {
    ram_pct: (d) => d?.data?.ram?.percent,
    swap_pct: (d) => d?.data?.swap?.percent,
    swap_io_out: (d) => d?.data?.swap_io?.out_kbps,
    disk_pct: (d) => d?.data?.disk?.percent,
    vram_pct: (d) => {
      const gpus = d?.data?.gpu?.gpus;
      return Array.isArray(gpus) && gpus.length ? gpus[0].vram_percent : null;
    },
    load1: (d) => d?.data?.load_avg?.["1m"],
    ups_load_pct: (d) => d?.data?.ups?.live?.load_pct,
  };

  function loadBuffers() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return typeof parsed === "object" && parsed ? parsed : {};
    } catch (e) {
      return {};
    }
  }

  function saveBuffers(buffers) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(buffers));
    } catch (e) {
      /* quota exceeded — drop silently */
    }
  }

  function pushSample(buffers, key, value) {
    if (value === null || value === undefined || Number.isNaN(value)) return;
    const arr = buffers[key] || (buffers[key] = []);
    arr.push(Number(value));
    while (arr.length > MAX_SAMPLES) arr.shift();
  }

  function buildPolyline(values, w, h) {
    if (!values || values.length < 2) return "";
    let lo = Infinity;
    let hi = -Infinity;
    for (const v of values) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    // Pad so a flat line still appears mid-height.
    if (hi - lo < 0.0001) {
      lo -= 1;
      hi += 1;
    }
    const span = hi - lo;
    const stepX = w / (values.length - 1);
    const pts = values.map((v, i) => {
      const x = (i * stepX).toFixed(1);
      const y = (h - ((v - lo) / span) * h).toFixed(1);
      return `${x},${y}`;
    });
    return pts.join(" ");
  }

  function renderSpark(el, values) {
    const w = 100;
    const h = 20;
    const points = buildPolyline(values, w, h);
    if (!points) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML =
      `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">` +
      `<polyline fill="none" stroke="currentColor" stroke-width="1.2" ` +
      `stroke-linejoin="round" stroke-linecap="round" points="${points}"/>` +
      `</svg>`;
  }

  function renderAll(buffers) {
    const els = document.querySelectorAll(".pd-sysmon-spark[data-metric]");
    els.forEach((el) => {
      const key = el.getAttribute("data-metric");
      renderSpark(el, buffers[key] || []);
    });
  }

  let buffers = loadBuffers();
  renderAll(buffers);

  if (!window.PD || !window.PD.SSE || typeof window.PD.SSE.on !== "function") {
    console.warn("[pd-sysmon-spark] PD.SSE not available; sparklines won't update live");
    return;
  }

  PD.SSE.on("system-monitor-update", function (evt) {
    let payload;
    try {
      payload = JSON.parse(evt.data);
    } catch (e) {
      return;
    }
    let touched = false;
    for (const [key, extract] of Object.entries(METRIC_EXTRACTORS)) {
      const value = extract(payload);
      if (value !== null && value !== undefined && !Number.isNaN(value)) {
        pushSample(buffers, key, value);
        touched = true;
      }
    }
    if (touched) {
      saveBuffers(buffers);
      renderAll(buffers);
    }
  });
})();
