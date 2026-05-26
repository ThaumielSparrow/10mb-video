"""Local benchmark harness — speed + quality (SSIM, optionally VMAF) across encoder configs.

Encodes every sample in test/ with each config in CONFIGS, measures wall-clock
encode time, output size, and quality vs the original. Writes a JSON results
file and prints a summary table.

    uv run python bench.py
    uv run python bench.py --target-mb 5
    uv run python bench.py --configs current_speed,speed_2pass_superfast

The bench bypasses VideoCompressor and invokes ffmpeg directly so experimental
configs (different presets, tunes, rc-lookahead, etc.) can be tried without
touching production code.

QUALITY METRICS
- SSIM always works (every ffmpeg has the filter).
- VMAF is reported when a libvmaf-enabled ffmpeg is available. msys2's
  ffmpeg is built without libvmaf, so by default only SSIM appears.

Enabling VMAF locally:
  1. Download a libvmaf-enabled ffmpeg. Easiest: a GPL build from
     https://github.com/BtbN/FFmpeg-Builds/releases (the *-gpl* variants
     include libvmaf; the lgpl ones don't).
  2. Point an env var at the binary, e.g.
       export VMAF_FFMPEG="/c/tools/ffmpeg-vmaf/bin/ffmpeg.exe"
  3. Re-run the bench — the harness auto-detects libvmaf in that binary and
     adds a VMAF column.

NOTE on ffmpeg version: HF Spaces (Debian bookworm-slim + apt) ships ffmpeg
5.1.x. Local msys2/Windows builds are typically much newer (8.x). All flags
used here (-preset, -tune, -b:v / -maxrate / -bufsize, -rc-lookahead, -pass)
are stable across both versions, so the *relative* ordering of configs in this
bench is what informs decisions — absolute timings will not match HF's slower
CPU and older ffmpeg.
"""

import argparse
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from utils import compute_bitrate_plan, get_video_metadata

SAMPLE_DIR = Path("test")
OUT_DIR = SAMPLE_DIR / "_bench_out"
RESULTS_PATH = SAMPLE_DIR / "_bench_results.json"

# ffmpeg binary used for quality measurement. Defaults to PATH ffmpeg; override
# with VMAF_FFMPEG=/path/to/ffmpeg.exe to point at a libvmaf-enabled build
# (msys2's ffmpeg is compiled without libvmaf).
_raw_quality_ffmpeg = os.environ.get("VMAF_FFMPEG", "ffmpeg")
# Auto-convert msys-style absolute paths (/c/...) to Windows form (C:/...)
# so users don't have to think about the distinction.
_msys_path = re.match(r"^/([a-zA-Z])(/.*)$", _raw_quality_ffmpeg)
QUALITY_FFMPEG = f"{_msys_path.group(1).upper()}:{_msys_path.group(2)}" if _msys_path else _raw_quality_ffmpeg


def _ffmpeg_filter_set(binary):
    """Return the set of filter names exposed by `binary -filters`."""
    try:
        result = subprocess.run(
            [binary, "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    out = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.add(parts[1])
    return out


_FILTERS = _ffmpeg_filter_set(QUALITY_FFMPEG)
VMAF_AVAILABLE = "libvmaf" in _FILTERS
XPSNR_AVAILABLE = "xpsnr" in _FILTERS


def _bitrate_args(vbitrate):
    return [
        "-b:v", str(int(vbitrate)),
        "-maxrate", str(int(vbitrate * 1.5)),
        "-bufsize", str(int(vbitrate * 2)),
    ]


def _audio_args(abitrate):
    if abitrate <= 0:
        return ["-an"]
    return ["-c:a", "aac", "-b:a", str(int(abitrate))]


def build_single_pass(input_path, output_path, vbitrate, abitrate, preset, tune=None, extra=None):
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-c:v", "libx264", "-preset", preset,
        *_bitrate_args(vbitrate),
        "-threads", "2",
    ]
    if tune:
        cmd += ["-tune", tune]
    if extra:
        cmd += list(extra)
    cmd += _audio_args(abitrate)
    cmd += [str(output_path)]
    return [cmd]


def build_two_pass(input_path, output_path, vbitrate, abitrate, preset_p1, preset_p2, log_prefix, extra_p2=None):
    common = [
        "-c:v", "libx264",
        *_bitrate_args(vbitrate),
        "-threads", "2",
        "-passlogfile", str(log_prefix),
    ]
    p1 = [
        "ffmpeg", "-y", "-i", str(input_path),
        *common, "-preset", preset_p1,
        "-pass", "1", "-an", "-f", "mp4", os.devnull,
    ]
    p2 = [
        "ffmpeg", "-y", "-i", str(input_path),
        *common, "-preset", preset_p2,
        "-pass", "2",
    ]
    if extra_p2:
        p2 += list(extra_p2)
    p2 += _audio_args(abitrate)
    p2 += [str(output_path)]
    return [p1, p2]


# Each config is (name, builder). builder(input, output, vbitrate, abitrate, log_prefix) → list of cmds.
# NOTE: libx264 requires the same -preset on both passes of a two-pass encode.
# A pass-1 preset that strips features (ultrafast disables mbtree/cabac/etc.)
# produces a stats file pass 2 refuses with EINVAL.
CONFIGS = [
    ("current_speed",
        lambda i, o, vb, ab, _: build_single_pass(i, o, vb, ab, "superfast", tune="fastdecode")),
    ("speed_no_fastdecode",
        lambda i, o, vb, ab, _: build_single_pass(i, o, vb, ab, "superfast")),
    ("speed_2pass_superfast",
        lambda i, o, vb, ab, lp: build_two_pass(i, o, vb, ab, "superfast", "superfast", lp)),
    ("speed_2pass_veryfast",
        lambda i, o, vb, ab, lp: build_two_pass(i, o, vb, ab, "veryfast", "veryfast", lp)),
    ("current_quality_medium",
        lambda i, o, vb, ab, lp: build_two_pass(i, o, vb, ab, "medium", "medium", lp)),
]

# Pipeline configs route through VideoCompressor.compress so we measure the
# full production experience (Auto resolution, trim-bitrate probe, container
# overhead buffer, etc.) — not just the raw ffmpeg encoder. Configs above
# the dashed line stress the encoder; configs below stress the pipeline.
# Each tuple: (name, speed_mode, output_resolution).
# (name, speed_mode, output_resolution, fps_mode)
PIPELINE_CONFIGS = [
    ("pipeline_speed_auto", "Prioritize Speed", "Auto", "Auto"),
    ("pipeline_speed_original", "Prioritize Speed", "Original", "Auto"),
    ("pipeline_quality_auto", "Prioritize Quality", "Auto", "Auto"),
]

_pipeline_compressor = None


def _get_compressor():
    global _pipeline_compressor
    if _pipeline_compressor is None:
        from compressor import VideoCompressor
        _pipeline_compressor = VideoCompressor()
    return _pipeline_compressor


def run_pipeline_encode(source_path, target_mb, speed_mode, output_resolution, fps_mode):
    """Run a full VideoCompressor.compress pipeline. Returns (output_path, elapsed_s, error)."""
    from compressor import CompressionCancelled
    compressor = _get_compressor()
    job_id = uuid.uuid4().hex[:12]
    start = time.monotonic()
    try:
        out = compressor.compress(
            job_id=job_id,
            input_path=str(source_path),
            target_mb=target_mb,
            remove_audio=False,
            start_time=None,
            end_time=None,
            speed_mode=speed_mode,
            output_resolution=output_resolution,
            fps_mode=fps_mode,
            progress_callback=lambda *a, **k: None,
        )
        return Path(out), time.monotonic() - start, None
    except CompressionCancelled as e:
        return None, time.monotonic() - start, f"Cancelled: {e}"
    except Exception as e:
        return None, time.monotonic() - start, str(e)


def run_encode(cmds):
    """Run cmds sequentially. Returns (elapsed_seconds, error_tail_or_none)."""
    start = time.monotonic()
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            tail = "\n".join(result.stderr.strip().splitlines()[-5:])
            return time.monotonic() - start, tail
    return time.monotonic() - start, None


# When the encoded output has different dimensions than the reference (e.g.
# pipeline configs that auto-downscale 1080p → 480p), use scale2ref to lanczos-
# upscale the encoded back to source dimensions before comparing. This is the
# standard methodology Netflix uses for VMAF on downscaled encodes — it
# measures "what does the user actually see" at the source's display size.
# When dimensions already match, scale2ref is effectively a no-op.
_SCALE2REF_PREFIX = "[0:v][1:v]scale2ref=flags=lanczos[main][ref];[main][ref]"


def measure_ssim(reference, encoded):
    """ssim filter writes 'All:0.XXX' to stderr at the end. Returns float or None.
    Order: encoded first, reference second (so scale2ref scales encoded up)."""
    cmd = [
        "ffmpeg",
        "-i", str(encoded),
        "-i", str(reference),
        "-lavfi", _SCALE2REF_PREFIX + "ssim",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    match = re.search(r"All:(\d+\.\d+)", result.stderr)
    return float(match.group(1)) if match else None


def _measure_vmaf(reference, encoded, model_version=None):
    """libvmaf — first input distorted, second reference. Score 0-100.
    Pass model_version=\"vmaf_v0.6.1neg\" to use the NEG (no-enhancement-gain)
    variant, which is more reliable on animation and synthetic content.
    n_threads=8 cuts measurement time ~3x vs the default auto-detect."""
    if not VMAF_AVAILABLE:
        return None
    opts = ["n_threads=8"]
    if model_version:
        opts.append(f"model=version={model_version}")
    filter_expr = "libvmaf" + (("=" + ":".join(opts)) if opts else "")
    cmd = [
        QUALITY_FFMPEG,
        "-i", str(encoded),
        "-i", str(reference),
        "-lavfi", _SCALE2REF_PREFIX + filter_expr,
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    for pat in (r"VMAF score[:=]\s*(\d+\.\d+)", r"mean[:=]\s*(\d+\.\d+)"):
        m = re.search(pat, result.stderr)
        if m:
            return float(m.group(1))
    return None


def measure_vmaf(reference, encoded):
    """Default VMAF model — best for natural/filmed content."""
    return _measure_vmaf(reference, encoded)


def measure_vmaf_neg(reference, encoded):
    """VMAF NEG model — sharpening-resistant; better on animation/screen content
    where the default model under-scores (or is gameable by sharpening tricks)."""
    return _measure_vmaf(reference, encoded, "vmaf_v0.6.1neg")


def measure_xpsnr(reference, encoded):
    """XPSNR — perceptually weighted PSNR. Higher = better, typical range 30-50
    dB. More reliable than vanilla PSNR for synthetic content (screen recordings,
    gameplay) where VMAF is unreliable. Returns the Y (luma) component."""
    if not XPSNR_AVAILABLE:
        return None
    cmd = [
        QUALITY_FFMPEG,
        "-i", str(encoded),
        "-i", str(reference),
        "-lavfi", _SCALE2REF_PREFIX + "xpsnr",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Final summary line: "XPSNR  y: 38.45  u: 42.12  v: 41.89  (minimum: ...)"
    m = re.search(r"XPSNR\s+y:\s*(\d+\.\d+|inf)", result.stderr)
    if not m:
        return None
    val = m.group(1)
    return None if val == "inf" else float(val)


def cleanup_pass_logs(log_prefix):
    for ext in ("-0.log", "-0.log.mbtree", ".log", ".log.mbtree"):
        p = Path(str(log_prefix) + ext)
        if p.exists():
            p.unlink()


def bench_source(source_path, target_mb, configs, pipeline_configs):
    meta = get_video_metadata(str(source_path))
    if not meta:
        raise SystemExit(f"Could not probe {source_path}")

    plan = compute_bitrate_plan(
        target_mb=target_mb,
        duration=meta["duration"],
        has_audio=meta["has_audio"],
        remove_audio=False,
        source_bitrate_cap=meta.get("bitrate"),
    )
    if plan is None:
        raise SystemExit("compute_bitrate_plan returned None")
    vbitrate, abitrate = plan

    print(f"\n=== {source_path.name} — target {target_mb} MB ===")
    print(f"Source: {meta['size_bytes'] / 1e6:.1f} MB, {meta['duration']:.1f}s, "
          f"{meta.get('width')}x{meta.get('height')}, bitrate {meta['bitrate'] / 1000:.0f} kbps")
    print(f"Plan:   {vbitrate / 1000:.0f} kbps video / {abitrate / 1000:.0f} kbps audio\n")

    def _record(name, out_path, elapsed, err):
        """Measure quality of `out_path` against `source_path`, print one line, append row."""
        if err:
            print(f"FAILED in {elapsed:.1f}s — {err.splitlines()[-1] if err else err}")
            rows.append({
                "config": name, "elapsed_s": round(elapsed, 2),
                "output_mb": None, "ssim": None, "vmaf": None,
                "vmaf_neg": None, "xpsnr": None, "error": err,
            })
            return

        out_mb = out_path.stat().st_size / 1e6
        ssim = measure_ssim(source_path, out_path)
        vmaf = measure_vmaf(source_path, out_path)
        vmaf_neg = measure_vmaf_neg(source_path, out_path)
        xpsnr = measure_xpsnr(source_path, out_path)
        delta = out_mb - target_mb

        bits = [f"{elapsed:6.1f}s", f"{out_mb:5.2f} MB ({delta:+.2f})"]
        bits.append(f"SSIM {ssim:.4f}" if ssim is not None else "SSIM N/A")
        if vmaf is not None:
            bits.append(f"VMAF {vmaf:5.2f}")
        if vmaf_neg is not None:
            bits.append(f"V-NEG {vmaf_neg:5.2f}")
        if xpsnr is not None:
            bits.append(f"XPSNR {xpsnr:5.2f}")
        print("  ".join(bits))

        rows.append({
            "config": name,
            "elapsed_s": round(elapsed, 2),
            "output_mb": round(out_mb, 3),
            "ssim": ssim,
            "vmaf": vmaf,
            "vmaf_neg": vmaf_neg,
            "xpsnr": xpsnr,
            "error": None,
        })

    rows = []
    for name, build in configs:
        out_path = OUT_DIR / f"{source_path.stem}__{name}.mp4"
        log_prefix = OUT_DIR / f"{source_path.stem}__{name}_log"
        print(f"  {name:36s} ", end="", flush=True)
        cmds = build(source_path, out_path, vbitrate, abitrate, log_prefix)
        elapsed, err = run_encode(cmds)
        cleanup_pass_logs(log_prefix)
        _record(name, out_path, elapsed, err)

    # Pipeline configs use VideoCompressor.compress() so they exercise Auto
    # resolution, the trim-bitrate probe, and any other production-pipeline
    # logic. Output paths live in the compressor's tempdir; bench just reads
    # them for quality measurement and lets the compressor's own TTL prune.
    for name, speed_mode, output_resolution, fps_mode in pipeline_configs:
        print(f"  {name:36s} ", end="", flush=True)
        out_path, elapsed, err = run_pipeline_encode(
            source_path, target_mb, speed_mode, output_resolution, fps_mode,
        )
        _record(name, out_path, elapsed, err)

    return {
        "source": source_path.name,
        "target_mb": target_mb,
        "source_meta": {
            "duration_s": meta["duration"],
            "size_mb": round(meta["size_bytes"] / 1e6, 2),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "fps": meta.get("fps"),
            "has_audio": meta["has_audio"],
            "source_bitrate_kbps": round(meta["bitrate"] / 1000),
        },
        "plan_kbps": {"video": round(vbitrate / 1000), "audio": round(abitrate / 1000)},
        "results": rows,
    }


def print_summary_table(runs):
    print("\n" + "=" * 80)
    print("Summary")
    print("=" * 80)
    for run in runs:
        print(f"\n{run['source']} — target {run['target_mb']} MB "
              f"(plan: {run['plan_kbps']['video']} kbps video)\n")
        baseline_t = next(
            (r["elapsed_s"] for r in run["results"] if r["config"] == "current_speed"),
            None,
        )
        # Dynamically build columns based on which metrics produced data.
        any_vmaf = any(r.get("vmaf") is not None for r in run["results"])
        any_neg = any(r.get("vmaf_neg") is not None for r in run["results"])
        any_xpsnr = any(r.get("xpsnr") is not None for r in run["results"])

        cols = [("config", 36, "s"), ("time", 7, ">s"), ("vs base", 8, ">s"),
                ("size MB", 8, ">s"), ("SSIM", 7, ">s")]
        if any_vmaf:
            cols.append(("VMAF", 6, ">s"))
        if any_neg:
            cols.append(("V-NEG", 6, ">s"))
        if any_xpsnr:
            cols.append(("XPSNR", 6, ">s"))

        header_parts = []
        for name, w, fmt in cols:
            if fmt.startswith(">"):
                header_parts.append(f"{name:>{w}s}")
            else:
                header_parts.append(f"{name:<{w}s}")
        print("  " + "  ".join(header_parts))
        print("  " + "  ".join("-" * w for _, w, _ in cols))

        for r in run["results"]:
            if r["error"]:
                print(f"  {r['config']:36s}  FAILED")
                continue
            ratio = (f"{r['elapsed_s'] / baseline_t:.2f}x" if baseline_t else "-")
            ssim_str = f"{r['ssim']:.4f}" if r["ssim"] is not None else "N/A"
            parts = [
                f"{r['config']:<36s}",
                f"{r['elapsed_s']:6.1f}s",
                f"{ratio:>8s}",
                f"{r['output_mb']:>8.2f}",
                f"{ssim_str:>7s}",
            ]
            if any_vmaf:
                parts.append(f"{r['vmaf']:>6.2f}" if r.get("vmaf") is not None else f"{'N/A':>6s}")
            if any_neg:
                parts.append(f"{r['vmaf_neg']:>6.2f}" if r.get("vmaf_neg") is not None else f"{'N/A':>6s}")
            if any_xpsnr:
                parts.append(f"{r['xpsnr']:>6.2f}" if r.get("xpsnr") is not None else f"{'N/A':>6s}")
            print("  " + "  ".join(parts))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=None,
                        help="Single source file. Default: all *.mp4 in test/.")
    parser.add_argument("--target-mb", type=float, default=10.0,
                        help="Target output size in MB. Default 10.")
    parser.add_argument("--configs", type=str, default=None,
                        help="Comma-separated config names. Default: all.")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    metrics_on = ["SSIM"]
    if VMAF_AVAILABLE:
        metrics_on += ["VMAF (default)", "VMAF NEG (animation/synthetic)"]
    if XPSNR_AVAILABLE:
        metrics_on.append("XPSNR (perceptually weighted, good for screen rec)")
    print(f"Metrics: {', '.join(metrics_on)}")
    print(f"Quality ffmpeg: {QUALITY_FFMPEG}")
    if not VMAF_AVAILABLE:
        print("  (Set VMAF_FFMPEG=/path/to/libvmaf-enabled/ffmpeg.exe to enable VMAF metrics.)")

    if args.source:
        sources = [args.source]
    else:
        sources = sorted(SAMPLE_DIR.glob("*.mp4"))
        if not sources:
            raise SystemExit(f"No .mp4 files in {SAMPLE_DIR}/")

    if args.configs:
        wanted = set(args.configs.split(","))
        configs = [c for c in CONFIGS if c[0] in wanted]
        pipeline_configs = [c for c in PIPELINE_CONFIGS if c[0] in wanted]
        if not configs and not pipeline_configs:
            all_names = [c[0] for c in CONFIGS] + [c[0] for c in PIPELINE_CONFIGS]
            raise SystemExit(f"No matching configs in {all_names}")
    else:
        configs = CONFIGS
        pipeline_configs = PIPELINE_CONFIGS

    runs = [bench_source(src, args.target_mb, configs, pipeline_configs) for src in sources]

    with RESULTS_PATH.open("w") as f:
        json.dump({"runs": runs}, f, indent=2)

    print_summary_table(runs)
    print(f"\nFull results: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
