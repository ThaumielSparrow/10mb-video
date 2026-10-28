// Shared bench routines; imported by both the page (main thread) and worker.js.
import {
  Input, Output, BlobSource, BufferTarget, Mp4OutputFormat, ALL_FORMATS,
  Conversion, Quality, VideoSampleSink, CanvasSink,
} from 'https://cdn.jsdelivr.net/npm/mediabunny@1.63.0/+esm';

export const log = (o) => fetch('/log', { method: 'POST', body: JSON.stringify(o) });
export const where = typeof window === 'undefined' ? 'worker' : 'main';

const openInput = (blob) => new Input({ source: new BlobSource(blob), formats: ALL_FORMATS });

// Decode every frame and drop it: the floor for any WebCodecs pipeline.
export async function decodeOnly(blob) {
  const track = await openInput(blob).getPrimaryVideoTrack();
  const sink = new VideoSampleSink(track);
  let n = 0;
  const t0 = performance.now();
  for await (const s of sink.samples()) { s.close(); n++; }
  return { ms: performance.now() - t0, frames: n };
}

// Decode + rotate + resize to the planned output size (what Conversion does before encoding).
export async function decodeResize(blob, c) {
  const track = await openInput(blob).getPrimaryVideoTrack();
  const sink = new CanvasSink(track, { width: c.width, height: c.height, fit: 'fill', poolSize: 2 });
  let n = 0;
  const t0 = performance.now();
  for await (const _ of sink.canvases()) n++;
  return { ms: performance.now() - t0, frames: n };
}

// Full production-shaped conversion. bitrateScale corrects for encoder undershoot.
export async function convert(blob, c, bitrateScale = 1) {
  const target = new BufferTarget();
  const output = new Output({ format: new Mp4OutputFormat({ fastStart: 'in-memory' }), target });
  const video = {
    codec: 'avc',
    width: c.width, height: c.height, fit: 'fill',
    quality: new Quality({ bitrate: Math.round(c.video_bitrate * bitrateScale), bitrateMode: 'variable' }),
    hardwareAcceleration: 'prefer-hardware',
    keyFrameInterval: 5,
    forceTranscode: true,
  };
  if (c.fps) video.frameRate = c.fps;
  const conversion = await Conversion.init({
    input: openInput(blob), output, video,
    audio: { codec: 'aac', quality: new Quality({ bitrate: c.audio_bitrate }) },
  });
  if (!conversion.isValid) throw new Error('invalid: ' + JSON.stringify(conversion.discardedTracks.map(d => d.reason)));
  const t0 = performance.now();
  await conversion.execute();
  return { ms: performance.now() - t0, buf: target.buffer };
}

const budgetBytes = (c) => (c.video_bitrate + c.audio_bitrate) * c.duration / 8;

export async function runCase(c, blob, steps) {
  const tag = (s) => `${c.name}__${s}${c.variant ? '_' + c.variant : ''}`;
  const rec = (s, r, extra = {}) => log({ tag: tag(s), where, ms: Math.round(r.ms), ...extra });
  try {
    if (steps.includes('decode')) await rec('decode_only', await decodeOnly(blob), {});
    if (steps.includes('resize')) await rec('decode_resize', await decodeResize(blob, c), {});
    if (steps.includes('convert')) {
      const a = await convert(blob, c);
      const mib = a.buf.byteLength / 1048576;
      await rec(`hw_${where}`, a, { mib: +mib.toFixed(3) });
      await fetch(`/result?file=${encodeURIComponent(tag(`hw_${where}`) + '.mp4')}`, { method: 'POST', body: a.buf });
      if (steps.includes('matched')) {
        // Second encode at a bitrate corrected by the measured undershoot, aiming at
        // 97% of the size budget — what a size-correcting retry would produce.
        const scale = Math.min(1.5, (0.97 * budgetBytes(c)) / a.buf.byteLength);
        const b = await convert(blob, c, scale);
        await rec(`hw_${where}_matched`, b, { mib: +(b.buf.byteLength / 1048576).toFixed(3), scale: +scale.toFixed(3) });
        await fetch(`/result?file=${encodeURIComponent(tag(`hw_${where}_matched`) + '.mp4')}`, { method: 'POST', body: b.buf });
      }
    }
  } catch (e) {
    await log({ tag: c.name, where, error: String((e && e.stack) || e) });
  }
}
