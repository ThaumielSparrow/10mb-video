// On-device encode: one Mediabunny conversion (WebCodecs underneath), run off the
// main thread so the page stays responsive. The page terminates this worker to
// cancel. In:  {file, plan, bitrate}  Out: {type: 'progress' | 'done' | 'error'}
const MEDIABUNNY = 'https://cdn.jsdelivr.net/npm/mediabunny@1.63.0/+esm';

self.onmessage = async ({ data: { file, plan, bitrate } }) => {
  try {
    const mb = await import(MEDIABUNNY);
    const webm = plan.passthrough === 'remux' && plan.remux_ext === '.webm';
    const target = new mb.BufferTarget();
    const output = new mb.Output({
      // fastStart keeps the index at the front so the result starts playing
      // (in Discord, in a browser) before it has fully downloaded.
      format: webm ? new mb.WebMOutputFormat() : new mb.Mp4OutputFormat({ fastStart: 'in-memory' }),
      target,
    });
    const input = new mb.Input({ source: new mb.BlobSource(file), formats: mb.ALL_FORMATS });
    const options = { input, output };

    if (plan.passthrough === 'remux') {
      // Already under the limit: copy the streams as-is, only dropping audio if asked.
      if (!plan.keep_audio) options.audio = { discard: true };
    } else {
      options.video = {
        codec: 'avc',
        width: plan.out_width,
        height: plan.out_height,
        fit: 'fill',
        quality: new mb.Quality({ bitrate: Math.round(bitrate), bitrateMode: 'variable' }),
        hardwareAcceleration: 'prefer-hardware',
        // Longer GOPs than the 2 s default: fewer key frames, more bits per frame.
        keyFrameInterval: 5,
        // Bake rotation into the frames so width/height are the display size.
        allowTransformationMetadata: false,
        forceTranscode: true,
      };
      if (plan.target_fps) options.video.frameRate = plan.target_fps;
      options.audio = plan.keep_audio
        ? { codec: 'aac', quality: new mb.Quality({ bitrate: plan.audio_bitrate }) }
        : { discard: true };
      if (plan.is_trimmed) options.trim = { start: plan.start, end: plan.end };
    }

    const conversion = await mb.Conversion.init(options);
    if (!conversion.isValid) {
      const reasons = conversion.discardedTracks.map((d) => d.reason).join(', ');
      throw new Error(`This browser can't convert this file (${reasons}).`);
    }
    conversion.onProgress = (p) => self.postMessage({ type: 'progress', p });
    await conversion.execute();
    const buffer = target.buffer;
    self.postMessage({ type: 'done', buffer, mime: webm ? 'video/webm' : 'video/mp4' }, [buffer]);
  } catch (e) {
    self.postMessage({ type: 'error', message: String((e && e.message) || e) });
  }
};
