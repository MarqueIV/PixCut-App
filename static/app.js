/**
 * PixCut Kiosk - front-end logic
 *
 * State lives on the server (LayoutCanvas).  Every mutation posts to the API,
 * gets back the new canvas state, and refreshes the preview image.
 */

"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let stickerList = [];        // all available sticker names
let canvasState = {          // mirrors server LayoutCanvas._status()
  selections: [],            // [{name, count, scale}, ...]
  overflow_count: 0,
  total_placed: 0,
  total_requested: 0,
  kp: 42,
};
let printPollTimer = null;
let canvasBusy = false;
let cutlinesVisible = false;
let scaleDebounceTimers = {}; // name -> setTimeout id
let titleTapCount = 0;
let titleTapTimer = null;
let usbGeneration = -1;
let usbPollTimer = null;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  await Promise.all([loadStickers(), refreshState()]);
  renderGrid();
  setupTitleTap();
  startUsbPoll();
  setupGridDragScroll();
});

// ---------------------------------------------------------------------------
// Drag-to-scroll for sticker grid (touch screen emulates mouse on Raspberry Pi)
// ---------------------------------------------------------------------------
function setupGridDragScroll() {
  const grid = document.getElementById("sticker-grid");
  let startY = 0, startScroll = 0, dragging = false, dragMoved = false;

  grid.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    // Don't hijack native button/input interactions
    if (e.target.closest("button, input, select")) return;
    dragging = true;
    dragMoved = false;
    startY = e.clientY;
    startScroll = grid.scrollTop;
  });

  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dy = startY - e.clientY;
    if (!dragMoved && Math.abs(dy) > 5) dragMoved = true;
    if (dragMoved) grid.scrollTop = startScroll + dy;
  });

  window.addEventListener("mouseup", () => {
    dragging = false;
  });

  // Capture-phase click handler: suppress card click when the gesture was a drag
  grid.addEventListener("click", (e) => {
    if (dragMoved) {
      e.stopPropagation();
      dragMoved = false;
    }
  }, true);
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(text || res.statusText);
  }
  if (res.headers.get("Content-Type")?.includes("application/json")) {
    return res.json();
  }
  return null;
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------
async function loadStickers() {
  const data = await api("GET", "/api/stickers");
  stickerList = data.stickers || [];
}

function startUsbPoll() {
  if (usbPollTimer) return;
  usbPollTimer = setInterval(async () => {
    try {
      const data = await api("GET", "/api/usb/generation");
      if (usbGeneration < 0) {
        usbGeneration = data.generation;
        return;
      }
      if (data.generation !== usbGeneration) {
        usbGeneration = data.generation;
        await loadStickers();
        renderGrid();
      }
    } catch (_) {}
  }, 5000);
}

async function refreshState() {
  canvasState = await api("GET", "/api/canvas/state");
  updateUI();
}

// ---------------------------------------------------------------------------
// Canvas mutations
// ---------------------------------------------------------------------------
async function withBusy(fn) {
  if (canvasBusy) return;
  canvasBusy = true;
  showCanvasLoading(true);
  try {
    const state = await fn();
    if (state) {
      canvasState = state;
      updateUI();
      refreshPreview();
    }
  } catch (err) {
    console.error("Canvas error:", err);
  } finally {
    canvasBusy = false;
    showCanvasLoading(false);
  }
}

async function addSticker(name) {
  await withBusy(() => api("POST", "/api/canvas/add", { name, count: 1, scale: 1.0 }));
}

async function removeSticker(name) {
  await withBusy(() => api("POST", "/api/canvas/remove", { name }));
}

async function setCount(name, count) {
  if (count <= 0) {
    await removeSticker(name);
  } else {
    await withBusy(() => api("POST", "/api/canvas/set_count", { name, count }));
  }
}

async function setScale(name, scale) {
  await withBusy(() => api("POST", "/api/canvas/set_scale", { name, scale }));
}

// Debounced scale: fire 400 ms after the user stops dragging.
function onScaleInput(name, sliderEl) {
  clearTimeout(scaleDebounceTimers[name]);
  const pct = parseInt(sliderEl.value, 10);
  // Update the label immediately.
  const label = sliderEl.closest(".scale-wrap")?.querySelector(".scale-label");
  if (label) label.textContent = pct + "%";
  scaleDebounceTimers[name] = setTimeout(() => {
    setScale(name, pct / 100);
  }, 400);
}

async function clearCanvas() {
  await withBusy(async () => {
    const state = await api("POST", "/api/canvas/clear");
    return state;
  });
}

// ---------------------------------------------------------------------------
// Secret admin panel (tap title 5 times within 3 s)
// ---------------------------------------------------------------------------
function setupTitleTap() {
  document.getElementById("title").addEventListener("click", () => {
    titleTapCount++;
    clearTimeout(titleTapTimer);
    if (titleTapCount >= 5) {
      titleTapCount = 0;
      openAdmin();
      return;
    }
    titleTapTimer = setTimeout(() => { titleTapCount = 0; }, 3000);
  });
}

async function openAdmin() {
  // Fetch settings and backgrounds independently — one failure shouldn't block the other.
  let s = null;
  try {
    s = await api("GET", "/api/settings");
    document.getElementById("admin-kp").value = s.kp ?? 42;
    document.getElementById("admin-cut-margin").value = s.margin_mm ?? 1.0;
    document.getElementById("admin-padding").value = s.padding_mm ?? 2.0;
    document.getElementById("admin-margin").value = s.left_margin_mm ?? 3.0;
    document.getElementById("admin-perf-cut").checked = s.perf_cut ?? false;
    document.getElementById("admin-perf-kp").value = s.perf_kp ?? 53;
    document.getElementById("admin-perf-dash").value = s.perf_dash_mm ?? 8.0;
    document.getElementById("admin-perf-gap").value = s.perf_gap_mm ?? 0.05;
    togglePerfFields();
  } catch (_) {}

  try {
    const bgData = await api("GET", "/api/backgrounds");
    const bgSelect = document.getElementById("admin-bg-image");
    bgSelect.innerHTML = '<option value="">— None —</option>';
    for (const name of (bgData.backgrounds || [])) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      if (s && name === s.bg_image) opt.selected = true;
      bgSelect.appendChild(opt);
    }
    if (!s?.bg_image) bgSelect.value = "";
  } catch (_) {}

  document.getElementById("admin-overlay").classList.remove("hidden");
}

function togglePerfFields() {
  const enabled = document.getElementById("admin-perf-cut").checked;
  document.getElementById("perf-fields").classList.toggle("hidden", !enabled);
}

function closeAdmin() {
  document.getElementById("admin-overlay").classList.add("hidden");
}

async function saveAdmin() {
  const kp = parseInt(document.getElementById("admin-kp").value, 10);
  const cutMargin = parseFloat(document.getElementById("admin-cut-margin").value);
  const padding = parseFloat(document.getElementById("admin-padding").value);
  const margin = parseFloat(document.getElementById("admin-margin").value);
  const perfCut = document.getElementById("admin-perf-cut").checked;
  const perfKp = parseInt(document.getElementById("admin-perf-kp").value, 10);
  const perfDash = parseFloat(document.getElementById("admin-perf-dash").value);
  const perfGap = parseFloat(document.getElementById("admin-perf-gap").value);

  if (isNaN(kp) || kp < 1 || kp > 100) { alert("KP must be 1-100"); return; }
  if (isNaN(cutMargin) || cutMargin < 0 || cutMargin > 20) { alert("Cut margin must be 0-20 mm"); return; }
  if (isNaN(padding) || padding < 0 || padding > 20) { alert("Sticker gap must be 0-20 mm"); return; }
  if (isNaN(margin) || margin < 0 || margin > 20) { alert("Left margin must be 0-20 mm"); return; }
  if (perfCut) {
    if (isNaN(perfKp) || perfKp < 1 || perfKp > 100) { alert("Perf KP must be 1-100"); return; }
    if (isNaN(perfDash) || perfDash < 0.1 || perfDash > 20) { alert("Dash length must be 0.1-20 mm"); return; }
    if (isNaN(perfGap) || perfGap < 0.01 || perfGap > 5) { alert("Gap length must be 0.01-5 mm"); return; }
  }

  const bgImage = document.getElementById("admin-bg-image").value;

  try {
    const payload = {
      kp, margin_mm: cutMargin, padding_mm: padding, left_margin_mm: margin,
      perf_cut: perfCut, perf_kp: perfKp,
      perf_dash_mm: perfDash, perf_gap_mm: perfGap,
      bg_image: bgImage,
    };
    const state = await api("POST", "/api/settings", payload);
    if (state) { canvasState = state; refreshPreview(); }
    closeAdmin();
  } catch (err) {
    alert("Failed to save settings: " + err.message);
  }
}

// ---------------------------------------------------------------------------
// Preview refresh
// ---------------------------------------------------------------------------
function refreshPreview() {
  const img = document.getElementById("canvas-preview");
  img.src = "/api/canvas/preview.jpg?t=" + Date.now();
  if (cutlinesVisible) refreshCutlineOverlay();
}

// ---------------------------------------------------------------------------
// Cutline overlay
// ---------------------------------------------------------------------------
function toggleCutlines() {
  cutlinesVisible = !cutlinesVisible;
  const btn = document.getElementById("btn-cutlines");
  btn.textContent = cutlinesVisible ? "Hide Cutlines" : "Show Cutlines";
  btn.classList.toggle("active", cutlinesVisible);
  if (cutlinesVisible) {
    refreshCutlineOverlay();
  } else {
    document.getElementById("cutline-overlay").classList.add("hidden");
  }
}

async function refreshCutlineOverlay() {
  const overlay = document.getElementById("cutline-overlay");
  const img = document.getElementById("canvas-preview");
  const wrap = document.getElementById("canvas-preview-wrap");
  try {
    const res = await fetch("/api/canvas/preview.svg?inline=1");
    if (!res.ok) { overlay.classList.add("hidden"); return; }
    const text = await res.text();
    const parser = new DOMParser();
    const doc = parser.parseFromString(text, "image/svg+xml");
    const src = doc.documentElement;
    const vb = src.getAttribute("viewBox");

    // Size and position the overlay to exactly cover the preview image.
    const imgRect = img.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    overlay.setAttribute("viewBox", vb || "0 0 1 1");
    overlay.style.left   = (imgRect.left - wrapRect.left) + "px";
    overlay.style.top    = (imgRect.top  - wrapRect.top)  + "px";
    overlay.style.width  = imgRect.width  + "px";
    overlay.style.height = imgRect.height + "px";

    // Replace overlay contents with paths from the fetched SVG.
    overlay.innerHTML = "";
    for (const el of src.querySelectorAll("path, circle, rect, polyline, polygon, line")) {
      const clone = document.createElementNS("http://www.w3.org/2000/svg", el.tagName);
      for (const attr of el.attributes) {
        if (attr.name !== "style" && attr.name !== "fill" && attr.name !== "stroke" && attr.name !== "stroke-width") {
          clone.setAttribute(attr.name, attr.value);
        }
      }
      overlay.appendChild(clone);
    }
    overlay.classList.remove("hidden");
  } catch (_) {
    overlay.classList.add("hidden");
  }
}

function showCanvasLoading(show) {
  document.getElementById("canvas-loading").classList.toggle("hidden", !show);
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
function selectionMap() {
  const m = {};
  for (const s of canvasState.selections) m[s.name] = s;
  return m;
}

// ---------------------------------------------------------------------------
// Folder grouping helpers
// ---------------------------------------------------------------------------
function getFolder(name) {
  const idx = name.lastIndexOf("/");
  return idx >= 0 ? name.slice(0, idx) : "";
}

function folderLabel(folder) {
  if (!folder) return null;
  if (folder.startsWith("usb/")) {
    const parts = folder.split("/");
    const usbLabel = parts[1];
    const sub = parts.slice(2).join(" / ");
    return sub ? `USB: ${usbLabel} / ${sub}` : `USB: ${usbLabel}`;
  }
  return folder.split("/").join(" / ");
}

function renderGrid() {
  const grid = document.getElementById("sticker-grid");
  const sel = selectionMap();
  grid.innerHTML = "";

  // Group stickers by containing folder, preserving order.
  const groups = new Map();
  for (const name of stickerList) {
    const folder = getFolder(name);
    if (!groups.has(folder)) groups.set(folder, []);
    groups.get(folder).push(name);
  }

  for (const [folder, names] of groups) {
    const heading = folderLabel(folder);
    if (heading !== null) {
      const hdr = document.createElement("div");
      hdr.className = "sticker-folder-header";
      hdr.textContent = heading;
      grid.appendChild(hdr);
    }

    for (const name of names) {
      const entry = sel[name];
      const selected = !!entry;
      const count = entry?.count || 0;
      const scalePct = Math.round((entry?.scale || 1.0) * 100);

      const card = document.createElement("div");
      card.className = "sticker-card" + (selected ? " selected" : "");
      card.dataset.name = name;

      const thumb = document.createElement("img");
      thumb.className = "sticker-thumb";
      thumb.src = "/api/sticker/" + name.split("/").map(encodeURIComponent).join("/");
      thumb.alt = name;
      thumb.loading = "lazy";
      card.appendChild(thumb);

      const fileLabel = document.createElement("div");
      fileLabel.className = "sticker-label";
      fileLabel.textContent = name.split("/").pop().replace(/\.png$/i, "");
      card.appendChild(fileLabel);

      if (selected) {
        // Count controls (+/−/badge)
        const controls = document.createElement("div");
        controls.className = "count-controls";
        controls.addEventListener("click", (e) => e.stopPropagation());

        const btnMinus = document.createElement("button");
        btnMinus.className = "count-btn";
        btnMinus.textContent = "−";
        btnMinus.setAttribute("aria-label", "Remove one");
        btnMinus.onclick = () => setCount(name, count - 1);
        controls.appendChild(btnMinus);

        const countBadge = document.createElement("span");
        countBadge.className = "count-badge";
        countBadge.textContent = count;
        controls.appendChild(countBadge);

        const btnPlus = document.createElement("button");
        btnPlus.className = "count-btn";
        btnPlus.textContent = "+";
        btnPlus.setAttribute("aria-label", "Add one more");
        btnPlus.onclick = () => setCount(name, count + 1);
        controls.appendChild(btnPlus);

        card.appendChild(controls);

        // Scale slider
        const scaleWrap = document.createElement("div");
        scaleWrap.className = "scale-wrap";
        scaleWrap.addEventListener("click", (e) => e.stopPropagation());

        const slider = document.createElement("input");
        slider.type = "range";
        slider.className = "scale-slider";
        slider.min = "25";
        slider.max = "200";
        slider.step = "5";
        slider.value = scalePct;
        slider.setAttribute("aria-label", "Sticker size");
        slider.addEventListener("input", () => onScaleInput(name, slider));
        scaleWrap.appendChild(slider);

        const scaleLabel = document.createElement("span");
        scaleLabel.className = "scale-label";
        scaleLabel.textContent = scalePct + "%";
        scaleWrap.appendChild(scaleLabel);

        card.appendChild(scaleWrap);

        // Remove (×) button
        const btnX = document.createElement("button");
        btnX.className = "remove-btn";
        btnX.textContent = "×";
        btnX.setAttribute("aria-label", "Remove sticker");
        btnX.addEventListener("click", (e) => {
          e.stopPropagation();
          removeSticker(name);
        });
        card.appendChild(btnX);
      }

      card.addEventListener("click", () => {
        if (!selected) addSticker(name);
      });

      grid.appendChild(card);
    }
  }
}

function updateUI() {
  renderGrid();
  updateCountLabel();
  updateOverflowBanner();
  updatePrintButton();
}

function updateCountLabel() {
  const n = canvasState.total_placed || 0;
  document.getElementById("count-label").textContent =
    n === 0 ? "0 selected" : n === 1 ? "1 sticker" : n + " stickers";
}

function updateOverflowBanner() {
  const banner = document.getElementById("overflow-banner");
  banner.classList.toggle("hidden", (canvasState.overflow_count || 0) === 0);
}

function updatePrintButton() {
  document.getElementById("btn-print").disabled = (canvasState.total_placed || 0) === 0;
}

// ---------------------------------------------------------------------------
// Print
// ---------------------------------------------------------------------------
async function startPrint() {
  try {
    await api("POST", "/api/print/start");
  } catch (err) {
    showPrintOverlay("Could not start print job", err.message);
    return;
  }
  showPrintOverlay("Printing…", "Connecting to printer…");
  pollPrintStatus();
}

function pollPrintStatus() {
  if (printPollTimer) clearInterval(printPollTimer);
  printPollTimer = setInterval(async () => {
    try {
      const status = await api("GET", "/api/print/status");
      updatePrintOverlay(status);
      if (status.status === "done" || status.status === "error") {
        clearInterval(printPollTimer);
        printPollTimer = null;
      }
    } catch (err) {
      console.error("Status poll failed:", err);
    }
  }, 1500);
}

function updatePrintOverlay(status) {
  const title = document.getElementById("print-title");
  const msg = document.getElementById("print-message");
  const icon = document.getElementById("print-icon");
  const errorActions = document.getElementById("print-error-actions");
  const doneActions = document.getElementById("print-done-actions");

  msg.textContent = status.message || "";

  if (status.status === "done") {
    title.textContent = "Done!";
    icon.innerHTML = '<span class="status-icon success">✓</span>';
    doneActions.classList.remove("hidden");
    errorActions.classList.add("hidden");
  } else if (status.status === "error") {
    title.textContent = "Print Error";
    icon.innerHTML = '<span class="status-icon error">✕</span>';
    errorActions.classList.remove("hidden");
    doneActions.classList.add("hidden");
  } else {
    title.textContent = "Printing…";
    icon.innerHTML = '<div class="spinner large"></div>';
  }
}

function showPrintOverlay(title, message) {
  const overlay = document.getElementById("print-overlay");
  document.getElementById("print-title").textContent = title;
  document.getElementById("print-message").textContent = message;
  document.getElementById("print-error-actions").classList.add("hidden");
  document.getElementById("print-done-actions").classList.add("hidden");
  document.getElementById("print-icon").innerHTML = '<div class="spinner large"></div>';
  overlay.classList.remove("hidden");
}

async function dismissPrint() {
  if (printPollTimer) {
    clearInterval(printPollTimer);
    printPollTimer = null;
  }
  try {
    await api("POST", "/api/print/reset");
  } catch (_) { /* ignore */ }
  document.getElementById("print-overlay").classList.add("hidden");
}

// ---------------------------------------------------------------------------
// Canvas rulers
// ---------------------------------------------------------------------------
const RULER_H = 18;   // horizontal ruler height (px)
const RULER_W = 20;   // vertical ruler width (px)
const CANVAS_W_IN = 4;
const CANVAS_H_IN = 7;

const RULER_BG   = "#15171e";   // solid background - no transparency
const RULER_TICK = "#c8ccd8";   // light tick/label colour

function buildHRulerContent(width, totalIn, h) {
  const ppi = width / totalIn;
  // Top ruler starts after the corner square (RULER_W), so subtract that offset.
  const parts = [`<rect width="${width}" height="${h}" fill="${RULER_BG}"/>`];
  // Separator line along the bottom edge
  parts.push(`<line x1="0" y1="${h}" x2="${width}" y2="${h}" stroke="#2e3340" stroke-width="1"/>`);
  for (let i = 0; i <= totalIn * 2; i++) {
    const x = (i * ppi / 2).toFixed(2);
    const major = i % 2 === 0;
    const tickH = major ? h * 0.6 : h * 0.35;
    parts.push(`<line x1="${x}" y1="${h}" x2="${x}" y2="${(h - tickH).toFixed(1)}" stroke="${RULER_TICK}" stroke-width="1"/>`);
    if (major && i > 0) {
      parts.push(`<text x="${(+x - 2).toFixed(1)}" y="${(h - tickH - 1).toFixed(1)}" fill="${RULER_TICK}" font-size="9" font-family="monospace">${i / 2}"</text>`);
    }
  }
  return parts.join("");
}

function buildVRulerContent(imgHeight, totalIn, w) {
  const ppi = imgHeight / totalIn;
  const parts = [`<rect width="${w}" height="${imgHeight}" fill="${RULER_BG}"/>`];
  parts.push(`<line x1="${w}" y1="0" x2="${w}" y2="${imgHeight}" stroke="#2e3340" stroke-width="1"/>`);
  for (let i = 0; i <= totalIn * 2; i++) {
    const y = (i * ppi / 2).toFixed(2);
    const major = i % 2 === 0;
    const tickW = major ? w * 0.6 : w * 0.35;
    parts.push(`<line x1="${w}" y1="${y}" x2="${(w - tickW).toFixed(1)}" y2="${y}" stroke="${RULER_TICK}" stroke-width="1"/>`);
    if (major && i > 0) {
      const xc = (w / 2).toFixed(1);
      if ((i / 2) === 7) { 
       var yc = (+y - 5).toFixed(1);
      } else {
       var yc = (+y - 2).toFixed(1);
      }
      parts.push(`<text x="${xc}" y="${yc}" fill="${RULER_TICK}" font-size="9" font-family="monospace" text-anchor="middle" transform="rotate(-90,${xc},${yc})">${i / 2}"</text>`);
    }
  }
  return parts.join("");
}

function updateRulers() {
  const img = document.getElementById("canvas-preview");
  const wrap = document.getElementById("canvas-preview-wrap");
  const rulerTop = document.getElementById("ruler-top");
  const rulerLeft = document.getElementById("ruler-left");

  const imgRect = img.getBoundingClientRect();
  const wrapRect = wrap.getBoundingClientRect();
  const iw = imgRect.width;
  const ih = imgRect.height;
  const il = imgRect.left - wrapRect.left;
  const it = imgRect.top - wrapRect.top;

  if (iw < 4 || ih < 4) return;

  // Snap to whole pixels so rulers butt flush against the image edge.
  const snapL = Math.floor(il);
  const snapT = Math.floor(it);

  // Layout - rulers live OUTSIDE the image in the padding zone (no overlay):
  //  +----------+----------------------------+
  //  |  corner  |     horizontal ruler       |  <- above image
  //  +----------+----------------------------+
  //  | vertical |     canvas image           |
  //  |  ruler   |                            |
  //  +----------+----------------------------+
  const rulerCorner = document.getElementById("ruler-corner");

  rulerCorner.style.left   = (snapL - RULER_W) + "px";
  rulerCorner.style.top    = (snapT - RULER_H) + "px";
  rulerCorner.style.width  = RULER_W + "px";
  rulerCorner.style.height = RULER_H + "px";
  rulerCorner.classList.remove("hidden");

  rulerTop.setAttribute("width", iw);
  rulerTop.setAttribute("height", RULER_H);
  rulerTop.style.left = snapL + "px";
  rulerTop.style.top  = (snapT - RULER_H) + "px";
  rulerTop.innerHTML = buildHRulerContent(iw, CANVAS_W_IN, RULER_H);
  rulerTop.classList.remove("hidden");

  rulerLeft.setAttribute("width", RULER_W);
  rulerLeft.setAttribute("height", ih);
  rulerLeft.style.left = (snapL - RULER_W) + "px";
  rulerLeft.style.top  = snapT + "px";
  rulerLeft.innerHTML = buildVRulerContent(ih, CANVAS_H_IN, RULER_W);
  rulerLeft.classList.remove("hidden");
}

// Wire up rulers and cutline overlay on image load and window resize
document.addEventListener("DOMContentLoaded", () => {
  const img = document.getElementById("canvas-preview");
  img.addEventListener("load", () => {
    updateRulers();
    if (cutlinesVisible) refreshCutlineOverlay();
  });
  window.addEventListener("resize", () => {
    updateRulers();
    if (cutlinesVisible) refreshCutlineOverlay();
  });
  if (img.complete && img.naturalWidth) updateRulers();
});
