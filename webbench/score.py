"""Score WebCodecs bench outputs against the server (x264) pipeline at the same target.

usage: python webbench/score.py [samples_dir] [out_dir]
Defaults: test/ and webbench/out/. Run from the repo root after server.py finished.
"""
import json
import os
import re
import subprocess
import sys
import time

REPO = os.getcwd()
sys.path.insert(0, REPO)
SAMPLES = sys.argv[1] if len(sys.argv) > 1 else "test"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join("tools", "wcbench", "out")


def ssim(reference, encoded):
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", encoded, "-i", reference, "-lavfi",
         "[0:v][1:v]scale2ref=flags=lanczos[m][r];[m][r]ssim", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.search(r"All:(\d+\.\d+)", r.stderr)
    return float(m.group(1)) if m else None


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                        "stream=width,height,avg_frame_rate", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return r.stdout.strip()


times = {}
log_path = os.path.join(OUT, "log.jsonl")
if os.path.exists(log_path):
    for line in open(log_path, encoding="utf-8"):
        o = json.loads(line)
        if "caps" in o or "env" in o:
            print("browser:", json.dumps(o.get("caps") or o.get("env")))
        if "tag" in o and "ms" in o:
            times.setdefault(o["tag"], []).append(o["ms"])
            extra = {k: v for k, v in o.items() if k not in ("tag", "ms")}
            print(f"  {o['tag']:52s} {o['ms'] / 1000:7.1f}s  {extra}")
        if "error" in o or "fatal" in o:
            print("ERROR:", o)

rows = []
for f in sorted(os.listdir(OUT)):
    if not f.endswith(".mp4"):
        continue
    tag = f[:-4]
    src = os.path.join(SAMPLES, tag.split("__")[0])
    p = os.path.join(OUT, f)
    rows.append((tag, times.get(tag), os.path.getsize(p) / 1048576, probe(p), ssim(src, p)))

# Server reference at the same targets (wall time on THIS machine, all cores).
from compressor import VideoCompressor  # noqa: E402

comp = VideoCompressor()
for name in sorted({r[0].split("__")[0] for r in rows}):
    for mode in ("Prioritize Speed", "Prioritize Quality"):
        t = time.monotonic()
        out = comp.compress(None, os.path.join(SAMPLES, name), 10, False, None, None, mode, "Auto", "Auto",
                            lambda *a, **k: None)
        el = time.monotonic() - t
        rows.append((f"{name}__server_x264_{mode.split()[1].lower()}", [round(el * 1000)],
                     os.path.getsize(out) / 1048576, probe(out), ssim(os.path.join(SAMPLES, name), out)))

print(f"\n{'config':58s} {'time(s)':>12s} {'MiB':>6s} {'geometry':>16s} {'SSIM':>7s}")
for tag, ms, mib, geo, s in rows:
    tstr = "/".join(f"{m / 1000:.1f}" for m in ms) if ms else "-"
    print(f"{tag:58s} {tstr:>12s} {mib:6.2f} {geo:>16s} {s if s is None else round(s, 5)!s:>7s}")
