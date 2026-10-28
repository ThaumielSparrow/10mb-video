---
title: 10mb-video
emoji: 📼
colorFrom: blue
colorTo: green
sdk: docker
pinned: false
short_description: online video compressor
---


Video compressor inspired by [8mb.video](https://8mb.video).

Supports predefined upload limits for some popular apps such as Discord as well as custom sizes.

**Fast** compresses on your own device with the browser's hardware video encoder (WebCodecs, via [Mediabunny](https://mediabunny.dev)), so the clip is never uploaded. Browsers without a usable hardware H.264 encoder fall back to the server automatically.

**Best quality** uploads the clip and runs a 2-pass libx264 encode on the server (CPU only).
