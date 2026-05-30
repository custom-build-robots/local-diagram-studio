#!/usr/bin/env node
/*
 * record.js — Record an animated SVG to MP4.
 *
 * Usage:  node record.js <input.svg> <output.mp4>
 *
 * Settings come from environment variables (set by app.py from config.yaml):
 *   D2REC_FPS         capture target frames per second        (default 20)
 *   D2REC_DURATION    capture length in seconds               (default 6)
 *   D2REC_MAXWIDTH    rendered width cap, px                  (default 900)
 *   D2REC_SPEED       playback speed; 1.0 = match live SVG     (default 1.0)
 *
 * Technique (frame capture + ffmpeg):
 *   1. Embed the SVG inline in a minimal HTML page so animations run exactly as
 *      in the app.
 *   2. Headless Chromium screenshots the SVG element repeatedly.
 *   3. We measure the REAL wall-clock time spent capturing, derive the true
 *      average capture fps, and encode the MP4 at that rate. This keeps the
 *      clip's duration equal to real time, so the animation plays at the same
 *      speed as the live SVG instead of being sped up (each screenshot takes
 *      longer than 1/fps, so a fixed encode fps would compress = speed it up).
 *   4. Clean up temp frames. Exit non-zero with a clear message on failure.
 *
 * ffmpeg must be on PATH.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

function num(envName, fallback) {
  const v = parseFloat(process.env[envName]);
  return Number.isFinite(v) && v > 0 ? v : fallback;
}

const FPS = num("D2REC_FPS", 20);
const DURATION_SEC = num("D2REC_DURATION", 6);
const MAX_WIDTH = num("D2REC_MAXWIDTH", 900);
const SPEED = num("D2REC_SPEED", 1.0);
const PLAYBACK_FPS = 30; // output fps for smooth playback (duration preserved)

const FRAME_COUNT = Math.max(2, Math.round(FPS * DURATION_SEC));

function fail(msg) {
  console.error(`[record.js] ERROR: ${msg}`);
  process.exit(1);
}

function findFfmpeg() {
  const probe = spawnSync("ffmpeg", ["-version"], { encoding: "utf8" });
  if (probe.error || probe.status !== 0) {
    fail("ffmpeg not found on PATH. Install it (winget install Gyan.FFmpeg) and retry.");
  }
}

async function main() {
  const [, , inputSvg, outputMp4] = process.argv;
  if (!inputSvg || !outputMp4) {
    fail("Usage: node record.js <input.svg> <output.mp4>");
  }
  if (!fs.existsSync(inputSvg)) {
    fail(`Input SVG not found: ${inputSvg}`);
  }
  findFfmpeg();

  let playwright;
  try {
    playwright = require("playwright");
  } catch (e) {
    fail("playwright module not installed. Run `npm install` in the recorder/ folder.");
  }
  const { chromium } = playwright;

  const svg = fs.readFileSync(inputSvg, "utf8");

  // d2 SVGs declare only a viewBox (no width/height), so the element collapses
  // unless we give it an explicit size. Derive the display size from viewBox.
  let dispW = MAX_WIDTH, dispH = MAX_WIDTH;
  const vb = svg.match(/viewBox\s*=\s*"([\d.\s-]+)"/i);
  if (vb) {
    const parts = vb[1].trim().split(/\s+/).map(Number);
    if (parts.length === 4 && parts[2] > 0 && parts[3] > 0) {
      const [, , w, h] = parts;
      const scale = Math.min(1, MAX_WIDTH / w);
      dispW = Math.round(w * scale);
      dispH = Math.round(h * scale);
    }
  }

  // Minimal page; white background avoids transparent-frame artifacts in H.264.
  const html = `<!doctype html><html><head><meta charset="utf-8">
    <style>
      html,body { margin:0; padding:0; background:#ffffff; }
      #wrap { display:inline-block; }
      #wrap svg { display:block; width:${dispW}px; height:${dispH}px; }
    </style></head>
    <body><div id="wrap">${svg}</div></body></html>`;

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "d2rec-"));
  let browser;
  let elapsedSec = DURATION_SEC;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: dispW, height: dispH } });
    await page.setContent(html, { waitUntil: "networkidle" });

    // Screenshot the element directly so the capture is exactly the SVG size.
    const wrap = page.locator("#wrap");
    const frameInterval = 1000 / FPS;
    const t0 = Date.now();
    for (let i = 0; i < FRAME_COUNT; i++) {
      const framePath = path.join(tmpDir, `frame_${String(i).padStart(4, "0")}.png`);
      await wrap.screenshot({ path: framePath });
      await page.waitForTimeout(frameInterval);
    }
    elapsedSec = (Date.now() - t0) / 1000;
  } catch (e) {
    fail(`Frame capture failed: ${e.message}`);
  } finally {
    if (browser) await browser.close();
  }

  // True capture fps from wall-clock time → keeps playback speed = real time.
  const realFps = FRAME_COUNT / Math.max(0.001, elapsedSec);
  const inputFps = Math.max(0.1, realFps * SPEED);

  fs.mkdirSync(path.dirname(path.resolve(outputMp4)), { recursive: true });

  const ff = spawnSync(
    "ffmpeg",
    [
      "-y",
      "-framerate", inputFps.toFixed(4),
      "-i", path.join(tmpDir, "frame_%04d.png"),
      "-r", String(PLAYBACK_FPS),
      // even dimensions are required by yuv420p / libx264
      "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
      "-c:v", "libx264",
      "-pix_fmt", "yuv420p",
      outputMp4,
    ],
    { encoding: "utf8", stdio: ["ignore", "inherit", "inherit"] }
  );

  // best-effort cleanup
  try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (_) {}

  if (ff.error || ff.status !== 0) {
    fail("ffmpeg failed to assemble the MP4.");
  }
  console.log(
    `[record.js] Wrote ${outputMp4} ` +
    `(${FRAME_COUNT} frames over ${elapsedSec.toFixed(2)}s real; ` +
    `encode ${inputFps.toFixed(2)} fps, speed ${SPEED})`
  );
}

main().catch((e) => fail(e.message));
