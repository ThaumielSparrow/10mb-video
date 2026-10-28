"""Local harness: serves a WebCodecs bench page + samples, receives encoded outputs.

usage (from the repo root):
    python webbench/server.py . test webbench/out [port]
then open http://localhost:8765/ in the browser under test (WebCodecs needs a
secure context: localhost or https). Exits after the page POSTs /done.
Set BENCH_HOST=0.0.0.0 to reach it from a phone (needs https or a browser flag).
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

REPO, SAMPLES, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
PORT = int(sys.argv[4]) if len(sys.argv) > 4 else 8765
sys.path.insert(0, REPO)
from utils import compute_bitrate_plan, get_video_metadata, pick_auto_fps_cap, pick_auto_resolution  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(OUT, exist_ok=True)


def plan_for(name, target_mb):
    """Mirror of the production plan (Auto res, Auto fps) for one sample."""
    meta = get_video_metadata(os.path.join(SAMPLES, name))
    vb, ab = compute_bitrate_plan(target_mb, meta["duration"], meta["has_audio"], False, meta["bitrate"])
    w, h, fps = meta["width"], meta["height"], meta["fps"]
    cap = pick_auto_fps_cap(fps, w, h, vb) if fps > 30 else 0
    eff = cap or fps
    short, long_ = min(w, h), max(w, h)
    t = pick_auto_resolution(short, long_, eff, vb)
    t = t if t < short else short
    out_long = round(t * long_ / short / 2) * 2
    ow, oh = (out_long, t) if w >= h else (t, out_long)
    return {"name": name, "target_mb": target_mb, "video_bitrate": vb, "audio_bitrate": ab,
            "width": ow, "height": oh, "fps": cap or None, "duration": meta["duration"]}


TARGET_MB = float(os.environ.get("BENCH_TARGET_MB", "10"))
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
# Every clip in the samples dir that's bigger than the target (others would be
# passed through untouched in production, so there's nothing to bench).
CASES = [
    plan_for(f, TARGET_MB) for f in sorted(os.listdir(SAMPLES))
    if f.lower().endswith(VIDEO_EXTS) and os.path.getsize(os.path.join(SAMPLES, f)) > TARGET_MB * 1048576
]
# BENCH_LOWRES=1: add a variant of each case one resolution-ladder step lower,
# to test whether the hardware encoder does better with more bits per pixel.
if os.environ.get("BENCH_LOWRES"):
    ladder = [1080, 720, 480, 360, 240]
    extra = []
    for c in CASES:
        short = min(c["width"], c["height"])
        lower = next((h for h in ladder if h < short), None)
        if lower:
            scale = lower / short
            extra.append({**c, "variant": f"{lower}p", "steps": ["convert"],
                          "width": round(c["width"] * scale / 2) * 2, "height": round(c["height"] * scale / 2) * 2})
        c["steps"] = ["convert"]
    CASES += extra
if os.environ.get("BENCH_CONVERT_ONLY"):
    for c in CASES:
        c["steps"] = ["convert"]
STATIC = {"/": "bench.html", "/bench.html": "bench.html", "/lib.js": "lib.js", "/worker.js": "worker.js"}
done = threading.Event()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in STATIC:
            ctype = "text/html" if STATIC[p].endswith(".html") else "text/javascript"
            self._send(200, open(os.path.join(HERE, STATIC[p]), "rb").read(), ctype)
        elif p == "/cases":
            self._send(200, json.dumps(CASES).encode(), "application/json")
        elif p.startswith("/samples/"):
            f = os.path.join(SAMPLES, os.path.basename(unquote(p)))
            self._send(200, open(f, "rb").read(), "application/octet-stream")
        else:
            self._send(404)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if u.path == "/result":
            with open(os.path.join(OUT, q["file"]), "wb") as f:
                f.write(body)
        elif u.path == "/log":
            line = body.decode("utf-8", "replace")
            print(line, flush=True)
            with open(os.path.join(OUT, "log.jsonl"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        elif u.path == "/done":
            done.set()
        self._send(200)


srv = ThreadingHTTPServer((os.environ.get("BENCH_HOST", "127.0.0.1"), PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"serving on {PORT}; cases: {json.dumps(CASES)}", flush=True)
done.wait(timeout=float(os.environ.get("BENCH_TIMEOUT", "900")))
srv.shutdown()
