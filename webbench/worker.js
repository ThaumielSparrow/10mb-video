import { runCase, log } from './lib.js';

self.onmessage = async ({ data: cases }) => {
  for (const c of cases) {
    const blob = await (await fetch('/samples/' + encodeURIComponent(c.name))).blob();
    await runCase(c, blob, c.steps || ['decode', 'resize', 'convert', 'matched']);
  }
  await log({ worker: 'done' });
  self.postMessage('done');
};
