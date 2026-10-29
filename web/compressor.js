// Runs as Gradio gr.HTML js_on_load: element, props and server (Python
// functions from app.py) are in scope. Flow: pick a file -> probe it in the
// browser -> ask Python for the plan -> encode on this device (WebCodecs) when
// the browser can, otherwise upload it and run the server's x264 pipeline.
(() => {
  const MEDIABUNNY = 'https://cdn.jsdelivr.net/npm/mediabunny@1.63.0/+esm';
  const MiB = 1024 * 1024;
  const $ = (sel) => element.querySelector(sel);
  const ui = {
    root: $('.tm'), pick: $('.tm-pick'), file: $('.tm-file'),
    source: $('.tm-source'), sourceName: $('.tm-source-name'), sourceFacts: $('.tm-source-facts'), change: $('.tm-change'),
    tape: $('.tm-tape'), strip: $('.tm-strip'), ticks: $('.tm-ticks'), cap: $('.tm-cap span'), src: $('.tm-src-mark span'), tapeNote: $('.tm-tape-note'),
    custom: $('.tm-custom'), customMb: $('.tm-custom-mb'), modeHint: $('.tm-mode-hint'),
    res: $('.tm-res'), fps: $('.tm-fps'), start: $('.tm-start'), end: $('.tm-end'), noAudio: $('.tm-noaudio'),
    summary: $('.tm-summary'), go: $('.tm-go'), cancel: $('.tm-cancel'), status: $('.tm-status'),
    result: $('.tm-result'), video: $('.tm-video'), resultLine: $('.tm-result-line'),
    download: $('.tm-download'), share: $('.tm-share'),
  };

  let mbPromise = null;
  const mediabunny = () => (mbPromise ??= import(MEDIABUNNY));

  const state = {
    file: null, meta: null, track: null, audioTrack: null, probeError: null,
    plan: null, path: 'server', reason: '', busy: false, abort: null,
    resultUrl: null, resultFile: null, resultBytes: null,
  };

  // ---------- formatting ----------
  const mb = (bytes) => {
    const v = bytes / MiB;
    return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} MB`;
  };
  const clock = (s) => {
    const m = Math.floor(s / 60);
    return m ? `${m}:${String(Math.round(s % 60)).padStart(2, '0')}` : `${s.toFixed(1)} s`;
  };
  const CODECS = { avc: 'h264', hevc: 'hevc', vp8: 'vp8', vp9: 'vp9', av1: 'av1' };

  // ---------- settings ----------
  function settings() {
    const target = element.querySelector('input[name="tm-target"]:checked').value;
    return {
      target_mb: target === 'custom' ? Number(ui.customMb.value) : Number(target),
      mode: element.querySelector('input[name="tm-mode"]:checked').value,
      resolution: ui.res.value,
      fps_mode: ui.fps.value,
      start: ui.start.value === '' ? 0 : Number(ui.start.value),
      end: ui.end.value === '' ? null : Number(ui.end.value),
      remove_audio: ui.noAudio.checked,
    };
  }

  // ---------- probing (no upload: Mediabunny reads only what it needs) ----------
  async function probe(file) {
    const m = await mediabunny();
    const input = new m.Input({ source: new m.BlobSource(file), formats: m.ALL_FORMATS });
    const [duration, track, audioTrack] = await Promise.all([
      input.computeDuration(), input.getPrimaryVideoTrack(), input.getPrimaryAudioTrack(),
    ]);
    if (!track) throw new Error('No video track found.');
    const [codec, width, height, stats] = await Promise.all([
      track.getCodec(), track.getDisplayWidth(), track.getDisplayHeight(), track.computePacketStats(120),
    ]);
    const ext = (file.name.match(/\.[^.]+$/) || [''])[0].toLowerCase();
    return {
      track, audioTrack,
      meta: {
        duration, size_bytes: file.size, has_audio: !!audioTrack,
        video_codec: CODECS[codec] || codec, container_ext: ext,
        width, height, fps: stats.averagePacketRate,
      },
    };
  }

  // ---------- can this browser encode the plan on-device? ----------
  function avcCodec(w, h, fps) {
    // High profile; level 4.0 covers 1080p30, 4.2 1080p60, 5.1 anything bigger.
    if (w * h > 1920 * 1088) return 'avc1.640033';
    return fps > 30 ? 'avc1.64002a' : 'avc1.640028';
  }

  async function deviceBlocker(plan) {
    if (!('VideoEncoder' in self) || !('Worker' in self)) return "Browser can't encode video";
    if (!state.track) return "Browser can't read this file";
    if (plan.passthrough === 'remux') return null; // stream copy: nothing to decode or encode
    if (!(await state.track.canDecode())) return `Browser can't decode ${state.meta.video_codec || 'this'} video`;
    const fps = plan.target_fps || state.meta.fps || 30;
    try {
      const support = await VideoEncoder.isConfigSupported({
        codec: avcCodec(plan.out_width, plan.out_height, fps),
        width: plan.out_width, height: plan.out_height,
        bitrate: Math.round(plan.video_bitrate), framerate: fps,
        hardwareAcceleration: 'prefer-hardware',
      });
      // Software-only encoders were no faster than hardware in testing and
      // clearly worse than the server's x264, so they don't qualify.
      if (!support.supported) return 'No hardware encoder found';
    } catch {
      return "Browser can't encode video";
    }
    if (plan.keep_audio) {
      const m = await mediabunny();
      if (!state.audioTrack || !(await state.audioTrack.canDecode()) || !(await m.canEncodeAudio('aac'))) {
        return "Browser can't encode AAC audio";
      }
    }
    return null;
  }

  // ---------- planning ----------
  let planSeq = 0;
  async function replan() {
    renderTarget();
    if (!state.file) { render(); return; }
    const seq = ++planSeq;
    const s = settings();
    if (!state.meta) {
      state.plan = null;
      state.path = 'server';
      state.reason = state.probeError || "Browser can't read this file";
      render();
      return;
    }
    let plan;
    let path = 'server';
    let reason = '';
    try {
      plan = await server.plan_job({ meta: state.meta, settings: s, device: s.mode === 'fast' });
      if (!plan.error && plan.passthrough !== 'unchanged' && s.mode === 'fast') {
        reason = await deviceBlocker(plan);
        if (!reason) path = 'device';
        else plan = await server.plan_job({ meta: state.meta, settings: s, device: false });
      }
    } catch {
      plan = { error: "Can't reach the server. Check your connection." };
    }
    if (seq !== planSeq) return; // a newer change is already being planned
    Object.assign(state, { plan, path: plan.passthrough === 'unchanged' ? 'none' : path, reason });
    render();
  }

  // ---------- rendering ----------
  function renderTarget() {
    const custom = element.querySelector('input[name="tm-target"]:checked').value === 'custom';
    ui.custom.hidden = !custom;
  }

  function niceStep(maxMb) {
    for (const step of [0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500]) {
      if (maxMb / step <= 10) return step;
    }
    return 1000;
  }

  function renderTape(liveBytes, finalBytes = state.resultBytes) {
    const s = settings();
    const target = s.target_mb * MiB;
    const plan = state.plan;
    if (!state.file || !(target > 0)) { ui.tape.hidden = true; return; }
    ui.tape.hidden = false;
    const scaleMax = target * 1.25;
    const pct = (b) => `${Math.min(100, (b / scaleMax) * 100).toFixed(2)}%`;
    const step = niceStep(scaleMax / MiB);
    ui.strip.style.setProperty('--tm-major', pct(step * MiB));
    ui.strip.style.setProperty('--tm-minor', pct((step * MiB) / 2));
    ui.strip.style.setProperty('--tm-cap', pct(target));
    ui.ticks.replaceChildren(...Array.from({ length: Math.floor(scaleMax / MiB / step) }, (_, i) => {
      const span = document.createElement('span');
      const v = (i + 1) * step;
      span.textContent = Number.isInteger(v) ? v : v.toFixed(2).replace(/0$/, '');
      span.style.left = pct(v * MiB);
      return span;
    }).filter((sp) => {
      // Keep labels off the right edge and out from under the limit line.
      const at = parseFloat(sp.style.left);
      return at < 96 && Math.abs(at - (target / scaleMax) * 100) > 3;
    }));
    ui.cap.textContent = `${s.target_mb} MB limit`;

    const overflow = state.file.size > scaleMax;
    ui.tape.classList.toggle('is-overflow', overflow);
    ui.tape.classList.toggle('is-src-right', !overflow && state.file.size / scaleMax > 0.55);
    ui.strip.style.setProperty('--tm-src', pct(state.file.size));
    ui.src.textContent = `Original ${mb(state.file.size)}`;

    ui.tape.classList.toggle('is-live', liveBytes != null);
    ui.tape.classList.toggle('is-final', finalBytes != null);
    ui.tape.classList.toggle('is-over', finalBytes != null && finalBytes > target);
    const shown = finalBytes ?? liveBytes ?? (plan && !plan.error ? (plan.passthrough ? state.file.size : plan.est_bytes) : 0);
    ui.strip.style.setProperty('--tm-fill', pct(shown));
    if (finalBytes != null) ui.tapeNote.textContent = `Final size: ${mb(finalBytes)}`;
    else if (liveBytes != null) ui.tapeNote.textContent = `${mb(liveBytes)} so far`;
    else if (plan && !plan.error && !plan.passthrough) ui.tapeNote.textContent = `Estimated size: ${mb(plan.est_bytes)}`;
    else ui.tapeNote.textContent = '';
  }

  function render() {
    const s = settings();
    const plan = state.plan;
    renderTape();

    if (!state.file) {
      ui.modeHint.textContent = s.mode === 'quality'
        ? 'Compresses on server. Slower, higher quality.'
        : 'Compresses on your device if supported, otherwise on server.';
    } else if (s.mode === 'quality') {
      ui.modeHint.textContent = 'Compresses on server. Slower, higher quality.';
    } else if (state.path === 'device') {
      ui.modeHint.textContent = 'Compresses on this device only, nothing will be uploaded.';
    } else if (state.path === 'none') {
      ui.modeHint.textContent = '';
    } else {
      ui.modeHint.textContent = `${state.reason}. Compresses on server.`;
    }

    const lines = [];
    if (plan?.error) {
      lines.push(`<span class="is-warn">${escapeHtml(plan.error)}</span>`);
    } else if (plan?.passthrough === 'unchanged') {
      lines.push(`Already under ${s.target_mb} MB, nothing to compress.`);
    } else if (plan?.passthrough === 'remux') {
      lines.push(`Already under ${s.target_mb} MB. ${s.remove_audio ? 'Removes audio' : 'Repackages as MP4'} without re-encoding.`);
    } else if (plan) {
      const fps = plan.target_fps || Math.round(state.meta.fps || 0);
      lines.push(`Output: ${plan.out_width} × ${plan.out_height}${fps ? ` at ${fps} fps` : ''}, ${Math.round(plan.video_bitrate / 1000).toLocaleString()} kbps video.`);
      if (plan.low_quality) lines.push('<span class="is-warn">Very low bitrate, expect blocky video. Trim the clip or raise the limit.</span>');
      if (s.resolution !== 'Auto' && plan.auto_short && plan.auto_short < (plan.target_short || plan.source_short)) {
        lines.push(`Auto would use ${plan.auto_short}p.`);
      }
    } else if (state.file) {
      lines.push('Details available after upload.');
    }
    ui.summary.innerHTML = lines.join(' ');
    ui.go.disabled = !state.file || state.busy || !!plan?.error;
    ui.go.textContent = state.path === 'none' ? 'Use original' : 'Compress';
  }

  function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
  }

  function setStatus(text, isError = false) {
    ui.status.textContent = text;
    ui.status.classList.toggle('is-error', isError);
  }

  function setBusy(busy) {
    state.busy = busy;
    ui.go.hidden = busy;
    ui.cancel.hidden = !busy;
    ui.change.disabled = busy;
    element.querySelectorAll('.tm-controls input, .tm-controls select').forEach((el) => { el.disabled = busy; });
    render();
  }

  // ---------- choosing a file ----------
  async function choose(file) {
    if (!file || state.busy) return;
    clearResult();
    Object.assign(state, { file, meta: null, track: null, audioTrack: null, probeError: null, plan: null });
    ui.root.dataset.stage = 'chosen';
    ui.source.hidden = false;
    ui.sourceName.textContent = file.name;
    ui.sourceFacts.textContent = `${mb(file.size)}. Reading…`;
    ui.start.value = '0';
    ui.end.value = '';
    setStatus('');
    if (file.size > props.max_mb * MiB) {
      ui.sourceFacts.textContent = mb(file.size);
      state.plan = { error: `Too large. Max input is ${props.max_mb} MB.` };
      render();
      return;
    }
    try {
      const probed = await probe(file);
      if (state.file !== file) return;
      Object.assign(state, probed);
      const { width, height, fps, duration } = state.meta;
      ui.sourceFacts.textContent = `${mb(file.size)}, ${clock(duration)}, ${width} × ${height}${fps ? ` at ${Math.round(fps)} fps` : ''}${state.meta.has_audio ? '' : ', no audio'}`;
      ui.end.placeholder = duration.toFixed(1);
      ui.end.max = ui.start.max = duration.toFixed(1);
    } catch (e) {
      if (state.file !== file) return;
      state.probeError = "Browser can't read this file";
      ui.sourceFacts.textContent = `${mb(file.size)}.`;
    }
    await replan();
  }

  function clearResult() {
    if (state.resultUrl?.startsWith('blob:')) URL.revokeObjectURL(state.resultUrl);
    Object.assign(state, { resultUrl: null, resultFile: null, resultBytes: null });
    ui.result.hidden = true;
    ui.video.removeAttribute('src');
    ui.video.load();
  }

  function outputName(ext) {
    const base = state.file.name.replace(/\.[^.]+$/, '');
    return `${base}_${settings().target_mb}mb${ext}`;
  }

  function showResult(url, bytes, file, seconds) {
    const target = settings().target_mb * MiB;
    Object.assign(state, { resultUrl: url, resultFile: file, resultBytes: bytes });
    ui.video.src = url;
    ui.download.href = url;
    ui.download.download = file ? file.name : outputName('.mp4');
    ui.resultLine.textContent = `${bytes <= target ? 'Under' : 'Over'} the ${settings().target_mb} MB limit.${seconds ? ` Took ${clock(seconds)}.` : ''}`;
    ui.share.hidden = !(file && navigator.canShare?.({ files: [file] }));
    ui.result.hidden = false;
    renderTape();
  }

  // ---------- on-device encode ----------
  function encodeOnce(bitrate, onProgress) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(new Blob([props.worker_src], { type: 'text/javascript' }));
      const worker = new Worker(url, { type: 'module' });
      URL.revokeObjectURL(url);
      state.abort = () => { worker.terminate(); reject(new DOMException('Cancelled', 'AbortError')); };
      worker.onmessage = ({ data }) => {
        if (data.type === 'progress') onProgress(data.p);
        else {
          worker.terminate();
          if (data.type === 'done') resolve(data);
          else reject(new Error(data.message));
        }
      };
      worker.onerror = (e) => { worker.terminate(); reject(new Error(e.message || 'Encoder crashed.')); };
      worker.postMessage({ file: state.file, plan: state.plan, bitrate });
    });
  }

  async function runOnDevice() {
    const plan = state.plan;
    const target = plan.target_bytes;
    const audioBytes = (plan.audio_bitrate * plan.duration) / 8;
    let bitrate = plan.video_bitrate;
    let lock = null;
    try { lock = await navigator.wakeLock?.request('screen'); } catch { /* not available */ }
    try {
      // Hardware encoders don't hit a bitrate exactly; re-encode at a corrected
      // rate if the result lands over the limit (never hand back an oversized file).
      for (let attempt = 0; attempt < 3; attempt++) {
        const label = attempt ? 'Over the limit, retrying smaller' : 'Compressing on device';
        const { buffer, mime } = await encodeOnce(bitrate, (p) => {
          setStatus(`${label} (${Math.round(p * 100)}%)`);
          renderTape(p * (plan.passthrough ? state.file.size : plan.est_bytes));
        });
        if (buffer.byteLength <= target || plan.passthrough) return { buffer, mime };
        bitrate *= Math.max(0.5, (0.93 * target - audioBytes) / (buffer.byteLength - audioBytes));
      }
      throw new Error('Couldn\'t get under the limit on this device.');
    } finally {
      lock?.release?.();
    }
  }

  // ---------- server encode ----------
  function uploadWithProgress(file, onProgress) {
    return new Promise((resolve, reject) => {
      const base = (window.gradio_config?.root || location.href.replace(/[?#].*$/, '')).replace(/\/?$/, '/');
      const xhr = new XMLHttpRequest();
      const form = new FormData();
      form.append('files', file);
      xhr.open('POST', `${base}gradio_api/upload`);
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
      xhr.onload = () => {
        if (xhr.status === 413) return reject(new Error(`File is over the ${props.max_mb} MB upload limit.`));
        if (xhr.status !== 200) return reject(new Error(`Upload failed (HTTP ${xhr.status}).`));
        try { resolve(JSON.parse(xhr.responseText)[0]); } catch { reject(new Error('Upload failed.')); }
      };
      xhr.onerror = () => reject(new Error('Upload failed. Check your connection.'));
      state.abort = () => { xhr.abort(); reject(new DOMException('Cancelled', 'AbortError')); };
      xhr.send(form);
    });
  }

  async function runOnServer() {
    const path = await uploadWithProgress(state.file, (p) => setStatus(`Uploading (${Math.round(p * 100)}%)`));
    setStatus('Starting…');
    const started = await server.start_job({ path, settings: settings() });
    if (started.error) throw new Error(started.error);
    let cancelled = false;
    state.abort = () => { cancelled = true; server.cancel_job({ job_id: started.job_id }); };
    for (;;) {
      if (cancelled) throw new DOMException('Cancelled', 'AbortError');
      const s = await server.job_status({ job_id: started.job_id });
      if (s.state === 'queued') setStatus(`Queued (position ${s.position})`);
      else if (s.state === 'running') {
        setStatus(`Compressing on server (${Math.round(s.progress * 100)}%)`);
        if (state.plan && !state.plan.error) renderTape(s.progress * state.plan.est_bytes);
      } else if (s.state === 'done') {
        const base = (window.gradio_config?.root || location.href.replace(/[?#].*$/, '')).replace(/\/?$/, '/');
        return { url: base + s.url, size: s.size };
      } else if (s.state === 'cancelled') throw new DOMException('Cancelled', 'AbortError');
      else throw new Error(s.error || 'Server couldn\'t compress this file.');
      await new Promise((r) => setTimeout(r, 800));
    }
  }

  async function compress() {
    if (!state.file || state.busy) return;
    clearResult();
    const t0 = performance.now();
    const elapsed = () => (performance.now() - t0) / 1000;
    if (state.path === 'none') {
      const url = URL.createObjectURL(state.file);
      showResult(url, state.file.size, state.file, 0);
      setStatus('');
      return;
    }
    setBusy(true);
    try {
      if (state.path === 'device') {
        try {
          const { buffer, mime } = await runOnDevice();
          const ext = mime === 'video/webm' ? '.webm' : '.mp4';
          const file = new File([buffer], outputName(ext), { type: mime });
          showResult(URL.createObjectURL(file), file.size, file, elapsed());
          setStatus('');
          return;
        } catch (e) {
          if (e.name === 'AbortError') throw e;
          // Anything the browser couldn't finish still has the server.
          setStatus(`${e.message} Retrying on server…`);
          state.path = 'server';
          state.reason = 'On-device encode failed';
        }
      }
      const { url, size } = await runOnServer();
      // Fetch the (small) result so Share works without another round trip.
      let file = null;
      try {
        const blob = await (await fetch(url)).blob();
        file = new File([blob], outputName('.mp4'), { type: blob.type || 'video/mp4' });
      } catch { /* Download still works from the URL */ }
      showResult(file ? URL.createObjectURL(file) : url, size, file, elapsed());
      setStatus('');
    } catch (e) {
      if (e.name === 'AbortError') {
        setStatus('Cancelled.');
        renderTape();
      } else {
        setStatus(e.message || String(e), true);
      }
    } finally {
      state.abort = null;
      setBusy(false);
    }
  }

  // ---------- events ----------
  ui.file.addEventListener('change', () => {
    const file = ui.file.files[0];
    ui.file.value = ''; // so picking the same file again still fires change
    choose(file);
  });
  ui.change.addEventListener('click', () => ui.file.click());
  ['dragenter', 'dragover'].forEach((t) => ui.pick.addEventListener(t, (e) => { e.preventDefault(); ui.pick.classList.add('is-drag'); }));
  ['dragleave', 'drop'].forEach((t) => ui.pick.addEventListener(t, () => ui.pick.classList.remove('is-drag')));
  ui.pick.addEventListener('drop', (e) => { e.preventDefault(); choose(e.dataTransfer.files[0]); });

  let debounce = null;
  element.querySelector('.tm-controls').addEventListener('input', () => {
    renderTarget();
    clearTimeout(debounce);
    debounce = setTimeout(replan, 200);
  });
  ui.go.addEventListener('click', compress);
  ui.cancel.addEventListener('click', () => state.abort?.());
  ui.share.addEventListener('click', async () => {
    try { await navigator.share({ files: [state.resultFile] }); } catch { /* dismissed */ }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && state.busy && state.path === 'device') {
      setStatus('Keep this tab open until it finishes.');
    }
  });

  // Start fetching the encoder library while the user picks a file.
  mediabunny().catch(() => {});
  render();
})();
