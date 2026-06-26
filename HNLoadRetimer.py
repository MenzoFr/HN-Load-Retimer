import os
import re
import json
import time
import tempfile
import threading
import queue
import subprocess
import sys
from collections import Counter
from fractions import Fraction

import numpy as np
from PIL import Image, ImageTk
from scipy.ndimage import gaussian_filter

import tkinter as tk
import customtkinter as ctk
from tkinter import filedialog

# --------------------------------------------------------------------------- #
#  Tunables                                                                    #
# --------------------------------------------------------------------------- #
# Icon-match threshold. Kept low on purpose: the load icon is rendered at
# slightly different sizes/positions across recordings, so its correlation
# score varies a lot (e.g. 0.83 on one video, 0.48 on another where the icon
# is smaller). A coincidental bright blob can even out-score a real-but-small
# icon, so NCC alone can't separate loads from junk -- the background
# concentration check (BG_FLAT_FRAC) does the real false-positive rejection.
NCC_THRESH      = 0.40
MIN_LOAD_FRAMES = 2
HN2_MIN_LOAD_FRAMES = 5   # HN2 only: drop any load shorter than this after the
                          # fade-out end has been refined (real loads are longer;
                          # a sub-5-frame HN2 hit is noise, not a load)
MERGE_GAP       = 2
ONLY_TEMPLATES  = None

BLUR_LEVELS = (1.5, 3.0)
ICON_BRIGHT_MARGIN = 50
MIN_ICON_BRIGHT    = 0.02

# A real load screen's background is a single SOLID colour field behind the
# icon: almost every background pixel shares one exact gray value. We measure
# the "concentration" -- the fraction of background pixels within BG_TOL of the
# single most common value (the mode). This beats the absolute-brightness and
# loose-uniformity approaches on two fronts:
#   * Gamma-independent: it keys off the mode, not zero, so a raised-gamma load
#     (solid gray ~12 instead of pure black) still scores ~1.0.
#   * Overlay-tolerant: a timer / LiveSplit panel covers part of the frame but
#     the rest is still the one solid value, so a panel covering up to ~35% of
#     the frame still passes. A dim-but-textured gameplay scene (curtains,
#     walls, a stray candle that fools the icon match) has its dark pixels
#     SPREAD across many values -- only ~0.55 land on the mode -- so it fails.
# BG_TOL is kept tiny (just absorbs compression dithering of the flat field).
BG_TOL          = 1
BG_FLAT_FRAC    = 0.65
# A load screen is a *dark* solid field. A fade-to-white / bright transition is
# also "concentrated" and could otherwise sneak through, so require the solid
# background value (the mode) to be dark. Comfortably above any raised-gamma
# load (~12-50) while rejecting white/bright uniform frames (~200+).
LOAD_MAX_MODE   = 120

# --- HN2 ------------------------------------------------------------------- #
# HN2 uses a different load icon (the white van, hn2.png) and, crucially, its
# loads *fade out* into gameplay when they end (HN1 loads cut instantly). Loads
# still appear instantly -- there is no fade-IN -- so only the tail matters.
#
# Detecting the fade is a two-stage job:
#
#   1. The fast pass (identical to HN1, the overlay-tolerant concentration check)
#      runs on the 480px-wide working frames and finds where the loads roughly
#      are. It only needs to BRACKET each load: that downscale box-AVERAGES the
#      very first fade frames -- where gameplay is only a sprinkle of value-1..3
#      pixels -- back down to black, so the fast pass ends a load a few frames
#      *late* (well into the fade), which stage 2 then corrects.
#
#   2. So for HN2 we then REFINE each load's end at FULL resolution. A fade-out
#      announces itself in one of two ways, and we trip on whichever comes first:
#        (a) the load's SOLID VALUE (the frame mode) lifts -- the whole screen
#            brightening uniformly. The mode is rock-stable for the whole load
#            (persistent UI, mid-load UI steps, encode noise and overlays never
#            move it), so a lift is unambiguous. But some fades into a DARK
#            gameplay scene never move the mode at all, which is why we also need:
#        (b) the lit-pixel count JUMPS above the LOCAL recent level -- a faint
#            wide bleed of value+1..3 pixels the mode can't see. The baseline is
#            local (a short trailing window) so UI that steps in/out mid-load --
#            whose level changes but only ever steps DOWN into the fade, never
#            jumps up -- is not mistaken for the bleed.
#      The load ends on the frame before that onset.
HN2_REFINE_MARGIN    = 0     # refine: a full-res pixel THIS far above the solid
                             #   value counts as "lit"
HN2_REFINE_MODE_MARGIN = 1   # (a) the fade has begun once the frame mode rises
                             #   THIS far above the load's solid value
HN2_REFINE_DELTA_FRAC= 0.003 # (b) ...or once the lit count rises this fraction of
                             #   the frame above its local trailing baseline
HN2_REFINE_TRAIL     = 15    #   how many frames back that local baseline averages
HN2_REFINE_PIXELS    = 60    #   never treat fewer than this many lit pixels as bleed
HN2_REFINE_ICON_PAD  = 0.05  # ignore the icon + this fraction of width around it
HN2_REFINE_LOOKBACK  = 60    # how many frames back from the fast end to re-scan

WORK_WIDTH  = 480
WHITE_THR   = 100
BRIGHT_SKIP = 200
PAD         = 8
PROGRESS_EVERY = 1000

if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.join(_BASE, "dependencies")


def find_bin(name):
    exe = name + (".exe" if os.name == "nt" else "")
    local = os.path.join(SCRIPT_DIR, exe)
    return local if os.path.exists(local) else name


FFMPEG  = find_bin("ffmpeg")
FFPROBE = find_bin("ffprobe")
DENO    = find_bin("deno")
YTDLP   = find_bin("yt-dlp")


# --------------------------------------------------------------------------- #
#  Engine                                                                      #
# --------------------------------------------------------------------------- #
def parse_time(text):
    parts = [float(p) for p in text.strip().split(":")]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Bad time: {text!r}")


def fmt_time(seconds):
    seconds = max(0.0, seconds)
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}"


TIME_RE = re.compile(r"^\d+(:\d{1,2}){0,2}(\.\d+)?$")


def fmt_box(seconds):
    seconds = max(0.0, seconds)
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}" if h else f"{m}:{s:06.3f}"


def fmt_dur(seconds):
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60); s = seconds % 60
    return f"{m}m {s:04.1f}s"


def time_from_clipboard(text):
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("cmt", "vct", "lct"):
                if key in data:
                    return float(data[key])
    except Exception:
        pass
    if TIME_RE.match(text):
        try:
            return parse_time(text)
        except Exception:
            return None
    return None


def norm_region(region, sigma):
    b = gaussian_filter(region.astype(np.float32), sigma)
    b = b - b.mean()
    n = np.linalg.norm(b)
    return b / n if n > 0 else b


def download_video(url, log):
    log("Downloading at best resolution + fps ...")
    videos_dir = os.path.join(_BASE, "Videos")
    os.makedirs(videos_dir, exist_ok=True)
    base_cmd = [
        YTDLP, "--no-playlist", "-S", "res,fps",
        "--merge-output-format", "mp4",
        "--ffmpeg-location", SCRIPT_DIR,
        "--js-runtimes", (f"deno:{DENO}" if DENO != "deno" else "deno"),
        "-o", os.path.join(videos_dir, "%(id)s.%(ext)s"),
        "--no-simulate", "--print", "after_move:filepath",
    ]

    # First try yt-dlp's default player clients (fast, best formats). For YouTube
    # only, fall back to forcing the "web" client if that fails: YouTube's default
    # clients (android_vr / web_safari) reject UNLISTED videos as "unavailable",
    # but the web client fetches them fine (an unlisted link needs no sign-in).
    # Other sites (Twitch VODs, etc.) just use the single default attempt.
    is_youtube = "youtu" in url.lower()
    attempts = [("default", [])]
    if is_youtube:
        attempts.append(("web", ["--extractor-args",
                                 "youtube:player_client=web,default"]))
    last_err = ""
    for label, extra in attempts:
        if label != "default":
            log("Default download failed - retrying with web client "
                "(unlisted videos) ...")
        proc = subprocess.run(base_cmd + extra + [url], capture_output=True,
                              text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.returncode == 0:
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            path = lines[-1].strip() if lines else ""
            if path and os.path.exists(path):
                log(f"Saved: {os.path.basename(path)}")
                return path
            last_err = "Could not determine downloaded file path."
        else:
            last_err = (proc.stderr or "").strip()
    raise RuntimeError("yt-dlp failed:\n" + last_err)


def resolve_source(src, log):
    src = src.strip().strip('"').strip("'")
    if os.path.exists(src):
        log(f"Using local file: {os.path.basename(src)}")
        return os.path.abspath(src)
    if src.startswith(("http://", "https://", "www.")) or "youtu" in src:
        return download_video(src, log)
    raise RuntimeError("That isn't a URL and no file exists at that path.")


def probe(path):
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,r_frame_rate",
           "-of", "csv=p=0", path]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    if not out:
        raise RuntimeError("ffprobe could not read that file.")
    w, h, rate = out.split(",")[:3]
    return int(w), int(h), float(Fraction(rate))


def extract_gameplay_frame(video):
    """Grab a bright (likely gameplay) full-resolution frame for cropping."""
    best, best_mean = None, -1.0
    for t in (2, 8, 20, 45, 80):
        tmp = os.path.join(tempfile.gettempdir(), f"hn1_frame_{t}.png")
        r = subprocess.run([FFMPEG, "-v", "error", "-ss", str(t), "-i", video,
                            "-frames:v", "1", "-y", tmp], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode == 0 and os.path.exists(tmp):
            try:
                m = float(np.asarray(Image.open(tmp).convert("L")).mean())
                if m > best_mean:
                    best_mean, best = m, tmp
            except Exception:
                pass
    return best


def load_templates(work_w, work_h, log, game="HN1"):
    if ONLY_TEMPLATES:
        names = ONLY_TEMPLATES
    elif game == "HN2":
        names = ("hn2",)
    else:
        names = ("1", "2", "3")
    templates = []
    bg_mask = np.ones((work_h, work_w), bool)
    for n in names:
        p = os.path.join(SCRIPT_DIR, f"{n}.png")
        if not os.path.exists(p):
            continue
        g = np.asarray(Image.open(p).convert("L").resize((work_w, work_h)))
        ys, xs = np.where(g > WHITE_THR)
        if len(xs) == 0:
            continue
        y0 = max(0, ys.min() - PAD); y1 = min(work_h, ys.max() + PAD)
        x0 = max(0, xs.min() - PAD); x1 = min(work_w, xs.max() + PAD)
        templates.append({"name": n, "bbox": (y0, y1, x0, x1),
                          "T": {s: norm_region(g[y0:y1, x0:x1], s) for s in BLUR_LEVELS}})
        bg_mask[y0:y1, x0:x1] = False
    if not templates:
        want = " / ".join(f"{n}.png" for n in names)
        raise RuntimeError(f"No {want} found next to the program.")
    log(f"Loaded templates: {', '.join(t['name'] for t in templates)}")
    return templates, bg_mask


def detect(frame, templates):
    best, best_name = 0.0, None
    for t in templates:
        y0, y1, x0, x1 = t["bbox"]
        region = frame[y0:y1, x0:x1]
        med = np.median(region)
        if np.mean(region > med + ICON_BRIGHT_MARGIN) < MIN_ICON_BRIGHT:
            continue
        for s in BLUR_LEVELS:
            score = float((t["T"][s] * norm_region(region, s)).sum())
            if score > best:
                best, best_name = score, t["name"]
    return best, best_name


def bg_is_load(bg):
    """Is the background a solid load field? Overlay-tolerant: a LiveSplit panel
    or running timer covering part of the frame still passes, because the rest of
    the background is still the one solid value (the concentration stays high).
    For HN2 this only needs to BRACKET the load -- the precise fade-out end is
    found later, at full resolution, by hn2_refine_end (which is itself timer-safe
    because its baseline absorbs any static overlay)."""
    mode = np.bincount(bg, minlength=256).argmax()
    if mode > LOAD_MAX_MODE:                         # bright/white frame, not a load
        return False
    conc = np.mean(np.abs(bg.astype(np.int16) - mode) <= BG_TOL)
    return conc >= BG_FLAT_FRAC


def analyse(video, start, duration, work_w, work_h, templates, bg_mask, progress,
            crop=None):
    frame_size = work_w * work_h
    vf = ""
    if crop:
        cx, cy, cw, ch = crop
        vf += f"crop={cw}:{ch}:{cx}:{cy},"
    vf += f"scale={work_w}:{work_h},format=gray"
    cmd = [FFMPEG, "-v", "error", "-ss", f"{start}", "-i", video,
           "-t", f"{duration}", "-an", "-vf", vf,
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=frame_size * 4,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    total = 0
    marks = []
    while True:
        buf = proc.stdout.read(frame_size)
        if len(buf) < frame_size:
            break
        idx = total
        total += 1
        frame = np.frombuffer(buf, dtype=np.uint8).reshape(work_h, work_w)
        if frame.mean() < BRIGHT_SKIP:
            score, name = detect(frame, templates)
            if score >= NCC_THRESH:
                if bg_is_load(frame[bg_mask]):
                    marks.append((idx, name))
        if total % PROGRESS_EVERY == 0:
            progress(total)
    proc.stdout.close()
    proc.wait()
    progress(total)
    return total, marks


def build_segments(marks):
    if not marks:
        return None, []
    dominant = Counter(n for _, n in marks).most_common(1)[0][0]
    idxs = sorted(i for i, n in marks if n == dominant)
    segs = []
    seg_start = prev = idxs[0]
    for i in idxs[1:]:
        if i - prev <= MERGE_GAP + 1:
            prev = i
        else:
            segs.append((seg_start, prev)); seg_start = prev = i
    segs.append((seg_start, prev))
    segs = [(a, b) for a, b in segs if (b - a + 1) >= MIN_LOAD_FRAMES]
    return dominant, segs


def hn2_refine_end(video, start, fps, a, b, icon_bbox, work_w, work_h, crop, w, h):
    """Pull a HN2 load's end (frame `b`, found late on the downscale) back to the
    true first fade frame, judged at full resolution. Returns the new last load
    frame index."""
    # Frame geometry of what ffmpeg will hand us (the cropped area at full res).
    fw, fh = (crop[2], crop[3]) if crop else (w, h)
    # The icon's bbox is in working (480px) coords -> scale up to full res and
    # pad generously so the van's own animation never reads as gameplay bleed.
    y0, y1, x0, x1 = icon_bbox
    sx, sy = fw / work_w, fh / work_h
    pad = int(HN2_REFINE_ICON_PAD * fw)
    X0 = max(0, int(x0 * sx) - pad); X1 = min(fw, int(x1 * sx) + pad)
    Y0 = max(0, int(y0 * sy) - pad); Y1 = min(fh, int(y1 * sy) + pad)

    lo = max(a, b - HN2_REFINE_LOOKBACK)
    n = (b - lo) + 3                       # scan the tail plus a couple past `b`
    if n <= 0:
        return b
    vf = ""
    if crop:
        cx, cy, cw, ch = crop
        vf += f"crop={cw}:{ch}:{cx}:{cy},"
    vf += "format=gray"
    out = subprocess.run(
        [FFMPEG, "-v", "error", "-ss", f"{start + lo / fps:.6f}", "-i", video,
         "-frames:v", str(n), "-an", "-vf", vf,
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout

    fsize = fw * fh
    modes = []
    hists = []                             # per-frame histogram of NON-icon pixels
    for i in range(n):
        chunk = out[i * fsize:(i + 1) * fsize]
        if len(chunk) < fsize:
            break
        fr = np.frombuffer(chunk, dtype=np.uint8).reshape(fh, fw)
        hist_full = np.bincount(fr.ravel(), minlength=256)
        hist_icon = np.bincount(fr[Y0:Y1, X0:X1].ravel(), minlength=256)
        modes.append(int(hist_full.argmax()))
        hists.append(hist_full - hist_icon)
    if not modes:
        return b

    # The solid load value M0 is the most common per-frame mode in the window: it
    # holds steady through persistent UI, mid-load UI steps, noise and overlays,
    # so the whole solid stretch shares it and only the fade moves off it.
    M0 = Counter(modes).most_common(1)[0][0]
    lit = M0 + HN2_REFINE_MARGIN
    counts = [int(h[lit + 1:].sum()) for h in hists]   # non-icon pixels above M0

    # Scan forward for the fade onset: the first frame (held for two frames so a
    # lone spike/noisy mode can't trip it) where EITHER the mode lifts off M0 (a),
    # OR the lit count jumps a chunk of the frame above its local trailing
    # baseline (b). Whichever fires first is the onset; the load ends just before.
    delta = max(HN2_REFINE_PIXELS, int(HN2_REFINE_DELTA_FRAC * fsize))
    onset = None
    for i in range(1, len(modes) - 1):
        mode_fade = (modes[i] > M0 + HN2_REFINE_MODE_MARGIN and
                     modes[i + 1] > M0 + HN2_REFINE_MODE_MARGIN)
        base = min(counts[max(0, i - HN2_REFINE_TRAIL):i])
        count_fade = counts[i] >= base + delta and counts[i + 1] >= base + delta
        if mode_fade or count_fade:
            onset = i
            break
    if onset is None:
        return b
    return max(a, lo + onset - 1)


def run_retime(source, start_str, end_str, log, progress, set_total, set_fps,
               set_times, crop=None, prevideo=None, game="HN1"):
    t0 = time.perf_counter()
    t_dl = time.perf_counter()
    video = prevideo or resolve_source(source, log)
    download_time = time.perf_counter() - t_dl
    w, h, fps = probe(video)
    log(f"Detected: {w}x{h} @ {fps:.3f} fps")
    set_fps(f"{w}x{h}  •  {fps:.3f} fps")

    cw = crop[2] if crop else w
    ch = crop[3] if crop else h
    if crop:
        log(f"Windowed: analysing game area {crop[2]}x{crop[3]} at ({crop[0]},{crop[1]})")
    work_w = WORK_WIDTH
    work_h = int(round(ch * (work_w / cw))); work_h += work_h % 2
    log(f"Game: {game}")
    templates, bg_mask = load_templates(work_w, work_h, log, game=game)

    start = parse_time(start_str); end = parse_time(end_str)
    snap_s = round(start * fps) / fps
    snap_e = round(end * fps) / fps
    if abs(snap_s - start) > 1e-9 or abs(snap_e - end) > 1e-9:
        log(f"Snapped to {fps:.3f} fps frames: {fmt_box(snap_s)} -> {fmt_box(snap_e)}")
    start, end = snap_s, snap_e
    set_times(fmt_box(start), fmt_box(end))
    if end <= start:
        raise RuntimeError("End time must be after start time.")
    duration = end - start
    set_total(int(round(duration * fps)))

    log("Analysing frames ...")
    total, marks = analyse(video, start, duration, work_w, work_h,
                           templates, bg_mask, progress, crop=crop)
    if total == 0:
        raise RuntimeError("No frames analysed - check your start/end times.")

    dominant, segs = build_segments(marks)

    if game == "HN2" and segs:
        # The fast pass ends each load a few frames into the fade-out; refine
        # every end at full resolution so the load stops on the true first fade
        # frame (the subtle one the downscale can't see).
        log("Refining fade-out ends (full resolution) ...")
        icon_bbox = templates[0]["bbox"]
        segs = [(a, hn2_refine_end(video, start, fps, a, b, icon_bbox,
                                   work_w, work_h, crop, w, h)) for a, b in segs]
        # The refine can shrink a load below the threshold, so enforce the HN2
        # minimum length here on the final (post-fade) frame count.
        segs = [(a, b) for a, b in segs if (b - a + 1) >= HN2_MIN_LOAD_FRAMES]

    load_frames = sum(b - a + 1 for a, b in segs)
    gameplay = total - load_frames

    lines = []
    if segs:
        lines.append("Detected loads:")
        for n, (a, b) in enumerate(segs, 1):
            length = b - a + 1
            lines.append(f"  {n:>3}.  {fmt_time(start + a / fps)} -> "
                         f"{fmt_time(start + (b + 1) / fps)}"
                         f"   ({length} f / {length / fps:.3f}s)")
        lines.append("")
    total_time = time.perf_counter() - t0
    retime_time = total_time - download_time
    lines.append(f"Elapsed total : {fmt_dur(total_time)}  (with download)")
    lines.append(f"Retime only   : {fmt_dur(retime_time)}  (analysis)")
    lines.append(f"Loads removed : {len(segs)} segments, {load_frames} frames "
                 f"({fmt_time(load_frames / fps)})")
    lines.append(f"FINAL TIME    : {fmt_time(gameplay / fps)}")
    return "\n".join(lines), fmt_time(gameplay / fps)


# --------------------------------------------------------------------------- #
#  UI                                                                          #
# --------------------------------------------------------------------------- #
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

ACCENT = "#2ea043"
ACCENT_HOVER = "#3fb950"
CARD = "#1c1c20"
FIELD = "#101013"


class CropDialog(ctk.CTkToplevel):
    """Show a frame and let the user drag a box around the game window."""
    def __init__(self, master, pil_image):
        super().__init__(master)
        self.title("Select the game area")
        self.configure(fg_color="#16161a")
        self.result = None
        self.iw, self.ih = pil_image.size
        self.scale = min(960 / self.iw, 600 / self.ih, 1.0)
        disp = pil_image.resize((int(self.iw * self.scale), int(self.ih * self.scale)),
                                Image.LANCZOS)
        self.tkimg = ImageTk.PhotoImage(disp)

        ctk.CTkLabel(self, text='Drag a box around the game window, then click "Use selection".').pack(pady=(10, 4))
        self.canvas = tk.Canvas(self, width=disp.width, height=disp.height,
                                highlightthickness=0, bg="#000", cursor="crosshair")
        self.canvas.pack(padx=12)
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg)
        self.rect = None; self.x0 = self.y0 = 0
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)

        btns = ctk.CTkFrame(self, fg_color="transparent"); btns.pack(pady=10)
        ctk.CTkButton(btns, text="Use selection", fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, command=self._ok).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Cancel", fg_color="#2a2a30",
                      hover_color="#34343c", command=self._cancel).pack(side="left", padx=6)
        self.after(120, lambda: (self.lift(), self.focus_force(), self.grab_set()))

    def _press(self, e):
        self.x0, self.y0 = e.x, e.y
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                 outline=ACCENT_HOVER, width=2)

    def _drag(self, e):
        if self.rect:
            self.canvas.coords(self.rect, self.x0, self.y0, e.x, e.y)

    def _ok(self):
        if not self.rect:
            return
        x1, y1, x2, y2 = self.canvas.coords(self.rect)
        x1, x2 = sorted((x1, x2)); y1, y2 = sorted((y1, y2))
        cx = max(0, int(x1 / self.scale)); cy = max(0, int(y1 / self.scale))
        cw = max(2, int((x2 - x1) / self.scale)); ch = max(2, int((y2 - y1) / self.scale))
        cw = min(cw, self.iw - cx); ch = min(ch, self.ih - cy)
        self.result = (cx, cy, cw, ch)
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.last_report = ""
        self.last_final = ""
        self._total = 1
        self.crop_rect = None
        self.resolved_video = None

        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HN1.LoadRetimer")
        except Exception:
            pass

        self.title("HN Load Retimer")
        self.geometry("740x680")
        self.minsize(660, 600)
        try:
            _ico = os.path.join(SCRIPT_DIR, "icon.ico")
            if os.path.exists(_ico):
                self.after(200, lambda: self.iconbitmap(_ico))
        except Exception:
            pass

        # header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(18, 4))
        try:
            _logo = os.path.join(SCRIPT_DIR, "icon_logo.png")
            if os.path.exists(_logo):
                self._logo_img = ctk.CTkImage(Image.open(_logo), size=(40, 40))
                ctk.CTkLabel(header, image=self._logo_img, text="").pack(side="left", padx=(0, 10))
        except Exception:
            pass
        ctk.CTkLabel(header, text="HN Load Retimer",
                     font=ctk.CTkFont("Segoe UI", 22, "bold")).pack(side="left")
        self.fps_lbl = ctk.CTkLabel(header, text="", text_color="#9aa0a6",
                                    font=ctk.CTkFont("Segoe UI", 12))
        self.fps_lbl.pack(side="right", pady=(8, 0))

        # input card
        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14)
        card.pack(fill="x", padx=20, pady=10)
        card.columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text="YouTube/Twitch link or video file",
                     font=ctk.CTkFont("Segoe UI", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 2))
        self.src = ctk.CTkEntry(card, height=38, fg_color=FIELD, border_width=0,
                                placeholder_text="https://youtube.com/...  or  C:\\path\\run.mp4")
        self.src.grid(row=1, column=0, sticky="ew", padx=(16, 8), pady=(0, 10))
        self.src.bind("<KeyRelease>", self.on_src_change)
        ctk.CTkButton(card, text="Browse", width=90, height=38,
                      fg_color="#2a2a30", hover_color="#34343c",
                      command=self.browse).grid(row=1, column=1, padx=(0, 16), pady=(0, 10))

        # run type row
        rt = ctk.CTkFrame(card, fg_color="transparent")
        rt.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 10))
        ctk.CTkLabel(rt, text="Game").pack(side="left", padx=(0, 8))
        self.game = ctk.CTkOptionMenu(rt, values=["HN1", "HN2"], width=80,
                                      fg_color="#2a2a30", button_color="#34343c",
                                      button_hover_color="#3d3d46")
        self.game.set("HN1")
        self.game.pack(side="left", padx=(0, 18))
        ctk.CTkLabel(rt, text="Run type").pack(side="left", padx=(0, 8))
        self.mode = ctk.CTkOptionMenu(rt, values=["Fullscreen run", "Windowed run"],
                                      width=170, command=self.on_mode,
                                      fg_color="#2a2a30", button_color="#34343c",
                                      button_hover_color="#3d3d46")
        self.mode.pack(side="left")
        self.sel_btn = ctk.CTkButton(rt, text="Select game area", width=150,
                                     state="disabled", fg_color="#2a2a30",
                                     hover_color="#34343c", command=self.select_area)
        self.sel_btn.pack(side="left", padx=10)
        self.crop_lbl = ctk.CTkLabel(rt, text="", text_color="#9aa0a6",
                                     font=ctk.CTkFont("Segoe UI", 11))
        self.crop_lbl.pack(side="left")

        # times row
        times = ctk.CTkFrame(card, fg_color="transparent")
        times.grid(row=3, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 14))
        ctk.CTkLabel(times, text="Start").grid(row=0, column=0, columnspan=2, sticky="w")
        ctk.CTkLabel(times, text="End").grid(row=0, column=2, columnspan=2, sticky="w", padx=(12, 0))
        pbtn = dict(width=58, height=36, fg_color="#2a2a30", hover_color="#34343c")
        self.start = ctk.CTkEntry(times, width=104, height=36, fg_color=FIELD, border_width=0)
        self.end = ctk.CTkEntry(times, width=104, height=36, fg_color=FIELD, border_width=0)
        ctk.CTkButton(times, text="Paste", command=lambda: self.paste_into(self.start),
                      **pbtn).grid(row=1, column=0, sticky="w")
        self.start.grid(row=1, column=1, sticky="w", padx=(6, 0))
        ctk.CTkButton(times, text="Paste", command=lambda: self.paste_into(self.end),
                      **pbtn).grid(row=1, column=2, sticky="w", padx=(12, 0))
        self.end.grid(row=1, column=3, sticky="w", padx=(6, 0))
        self.start.insert(0, "0:00"); self.end.insert(0, "1:32")
        self.run_btn = ctk.CTkButton(times, text="Retime", height=36, width=140,
                                     fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                     font=ctk.CTkFont("Segoe UI", 13, "bold"),
                                     command=self.start_run)
        self.run_btn.grid(row=1, column=4, sticky="e", padx=(16, 0))
        times.columnconfigure(4, weight=1)

        # result
        res = ctk.CTkFrame(self, fg_color="transparent")
        res.pack(fill="x", padx=20, pady=(2, 0))
        ctk.CTkLabel(res, text="Final time", text_color="#9aa0a6",
                     font=ctk.CTkFont("Segoe UI", 12)).pack(side="left")
        self.result = ctk.CTkLabel(res, text="—", text_color=ACCENT_HOVER,
                                   font=ctk.CTkFont("Consolas", 26, "bold"))
        self.result.pack(side="left", padx=12)
        self.copy_btn = ctk.CTkButton(res, text="Copy", width=80, height=30,
                                      fg_color="#2a2a30", hover_color="#34343c",
                                      state="disabled", command=self.copy_report)
        self.copy_btn.pack(side="right")

        self.bar = ctk.CTkProgressBar(self, height=10,
                                      fg_color="#2a2a30", progress_color="#2a2a30")
        self.bar.pack(fill="x", padx=20, pady=12)
        self.bar.set(0)

        self.log = ctk.CTkTextbox(self, fg_color=FIELD, corner_radius=12,
                                  font=ctk.CTkFont("Consolas", 12), wrap="word")
        self.log.pack(fill="both", expand=True, padx=20, pady=(0, 18))
        self.log.configure(state="disabled")

        self.after(100, self.poll)

    # ----- helpers ----- #
    def browse(self):
        path = filedialog.askopenfilename(
            title="Choose a video",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.webm *.avi *.flv"),
                       ("All files", "*.*")])
        if path:
            self.src.delete(0, "end"); self.src.insert(0, path)
            self.on_src_change()

    def paste_into(self, entry):
        try:
            text = self.clipboard_get()
        except Exception:
            self.log_line("Clipboard is empty."); return
        secs = time_from_clipboard(text)
        if secs is None:
            self.log_line("Clipboard isn't a timestamp or YouTube debug info."); return
        entry.delete(0, "end"); entry.insert(0, fmt_box(secs))

    def log_line(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n"); self.log.see("end")
        self.log.configure(state="disabled")

    def copy_report(self):
        # Copy a ready-to-paste mod note with the final time as MM:SS.mmm
        # (or H:MM:SS.mmm for runs over an hour), e.g. "Mod Note: Retimed to 5:05.333."
        try:
            note = f"Mod Note: Retimed to {fmt_box(parse_time(self.last_final))}."
        except Exception:
            note = f"Mod Note: Retimed to {self.last_final}."
        self.clipboard_clear(); self.clipboard_append(note)

    def windowed(self):
        return self.mode.get() == "Windowed run"

    # ----- mode / source ----- #
    def on_mode(self, choice):
        self.crop_rect = None; self.resolved_video = None
        self.crop_lbl.configure(text="")
        if self.windowed():
            self.sel_btn.configure(state="normal")
            self.run_btn.configure(state="disabled")
            self.log_line('Windowed mode: set the source, then "Select game area".')
        else:
            self.sel_btn.configure(state="disabled")
            self.run_btn.configure(state="normal")

    def on_src_change(self, _e=None):
        if self.windowed():
            self.crop_rect = None; self.resolved_video = None
            self.crop_lbl.configure(text="")
            self.run_btn.configure(state="disabled")

    # ----- select game area ----- #
    def select_area(self):
        source = self.src.get().strip()
        if not source:
            self.log_line("Enter a link or pick a file first."); return
        self.sel_btn.configure(state="disabled", text="Preparing…")
        self.run_btn.configure(state="disabled")
        self.log.configure(state="normal"); self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        threading.Thread(target=self.prepare_worker, args=(source,), daemon=True).start()

    def prepare_worker(self, source):
        try:
            video = resolve_source(source, lambda m: self.q.put(("log", m)))
            w, h, _ = probe(video)
            self.q.put(("log", f"Grabbing a frame to crop ({w}x{h}) ..."))
            fp = extract_gameplay_frame(video)
            if not fp:
                raise RuntimeError("Could not extract a frame to crop.")
            self.q.put(("crop_frame", (fp, video)))
        except Exception as e:
            self.q.put(("log", "ERROR: " + str(e)))
            self.q.put(("prep_fail", None))

    # ----- run ----- #
    def start_run(self):
        source = self.src.get().strip()
        if not source:
            self.log_line("Enter a link or pick a file first."); return
        if self.windowed() and not self.crop_rect:
            self.log_line('Windowed mode: click "Select game area" first.'); return
        self.run_btn.configure(state="disabled", text="Working…")
        self.sel_btn.configure(state="disabled")
        self.copy_btn.configure(state="disabled")
        self.result.configure(text="—")
        self.fps_lbl.configure(text="")
        self.bar.configure(progress_color=ACCENT); self.bar.set(0)
        self.log.configure(state="normal"); self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        crop = self.crop_rect if self.windowed() else None
        prevideo = self.resolved_video if self.windowed() else None
        threading.Thread(target=self.worker,
                         args=(source, self.start.get(), self.end.get(), crop,
                               prevideo, self.game.get()),
                         daemon=True).start()

    def worker(self, source, s, e, crop, prevideo, game):
        try:
            report, final = run_retime(
                source, s, e,
                log=lambda m: self.q.put(("log", m)),
                progress=lambda n: self.q.put(("progress", n)),
                set_total=lambda n: self.q.put(("total", n)),
                set_fps=lambda t: self.q.put(("fps", t)),
                set_times=lambda a, b: self.q.put(("times", (a, b))),
                crop=crop, prevideo=prevideo, game=game)
            self.q.put(("report", (report, final)))
        except Exception as ex:
            self.q.put(("log", "ERROR: " + str(ex)))
        finally:
            self.q.put(("done", None))

    # ----- queue pump ----- #
    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log_line(val)
                elif kind == "fps":
                    self.fps_lbl.configure(text=val)
                elif kind == "times":
                    s, e = val
                    self.start.delete(0, "end"); self.start.insert(0, s)
                    self.end.delete(0, "end"); self.end.insert(0, e)
                elif kind == "total":
                    self._total = max(1, val)
                elif kind == "progress":
                    self.bar.set(min(val / self._total, 1.0))
                elif kind == "crop_frame":
                    fp, video = val
                    dlg = CropDialog(self, Image.open(fp))
                    self.wait_window(dlg)
                    self.sel_btn.configure(state="normal", text="Select game area")
                    if dlg.result:
                        self.crop_rect = dlg.result
                        self.resolved_video = video
                        cx, cy, cw, ch = dlg.result
                        self.crop_lbl.configure(text=f"area {cw}x{ch}")
                        self.log_line(f"Game area set: {cw}x{ch} at ({cx},{cy})")
                        self.run_btn.configure(state="normal")
                    else:
                        self.log_line("Cropping cancelled.")
                elif kind == "prep_fail":
                    self.sel_btn.configure(state="normal", text="Select game area")
                elif kind == "report":
                    report, final = val
                    self.last_report = report
                    self.last_final = final
                    self.result.configure(text=final)
                    self.copy_btn.configure(state="normal")
                    self.log_line(""); self.log_line(report)
                elif kind == "done":
                    self.run_btn.configure(state="normal", text="Retime")
                    if self.windowed():
                        self.sel_btn.configure(state="normal")
                    self.bar.set(1.0)
        except queue.Empty:
            pass
        self.after(100, self.poll)


if __name__ == "__main__":
    App().mainloop()
