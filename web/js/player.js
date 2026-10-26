/* Vela picture path.
   Tesla freezes <video> once the car leaves Park, and it also blacks out
   drawImage(video). Frames are decoded with WebCodecs and painted on a canvas.
   Audio goes through the Web Audio API. Compatibility mode uses JSMpeg. */

(function () {
  function webCodecsAvailable() {
    return window.isSecureContext && typeof VideoDecoder === "function" && typeof AudioDecoder === "function";
  }

  function descriptionFor(mp4, track, kind) {
    const trak = mp4.getTrackById(track.id);
    const entry = trak && trak.mdia.minf.stbl.stsd.entries[0];
    if (!entry) return null;
    if (kind === "video" && entry.avcC) {
      const stream = new DataStream(undefined, 0, DataStream.BIG_ENDIAN);
      entry.avcC.write(stream);
      return new Uint8Array(stream.buffer, 8);
    }
    if (kind === "audio" && entry.esds && entry.esds.esd && entry.esds.esd.descs[0]) {
      const specific = entry.esds.esd.descs[0].descs && entry.esds.esd.descs[0].descs[0];
      if (specific && specific.data) return new Uint8Array(specific.data);
    }
    return null;
  }

  class CabinPlayer {
    constructor(canvas, hooks) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d", { alpha: false });
      this.hooks = hooks || {};
      this.audioCtx = null;
      this.gain = null;
      this.volume = 0.9;
      this.generation = 0;
      this.jsmpeg = null;
      this.raf = 0;
      this.fit();
    }

    fit() {
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      const rect = this.canvas.getBoundingClientRect();
      const w = Math.max(2, Math.round(rect.width * dpr));
      const h = Math.max(2, Math.round(rect.height * dpr));
      if (this.canvas.width !== w || this.canvas.height !== h) {
        this.canvas.width = w;
        this.canvas.height = h;
      }
    }

    setVolume(value) {
      this.volume = value;
      if (this.gain) this.gain.gain.value = value;
      if (this.jsmpeg && this.jsmpeg.volume !== undefined) this.jsmpeg.volume = value;
    }

    async play(opts) {
      this.stop();
      this.opts = opts;
      this.base = opts.start || 0;
      this.duration = opts.duration || 0;
      this.paused = false;
      this.ended = false;
      if (opts.compat || !webCodecsAvailable()) {
        this._playCompat(opts);
        return;
      }
      this.audioCtx = this.audioCtx || new AudioContext();
      this.gain = this.gain || this.audioCtx.createGain();
      this.gain.connect(this.audioCtx.destination);
      this.gain.gain.value = this.volume;
      await this.audioCtx.resume();
      await this._playWebCodecs(opts);
    }

    async seek(seconds) {
      if (!this.opts) return;
      const next = Math.max(0, seconds);
      await this.play({ ...this.opts, start: next });
    }

    async toggle() {
      if (this.jsmpeg) {
        if (this.jsmpeg.paused) this.jsmpeg.play();
        else this.jsmpeg.pause();
        this.paused = !!this.jsmpeg.paused;
        this._emitState();
        return;
      }
      if (!this.audioCtx) return;
      this.paused = !this.paused;
      if (this.paused) {
        this.pausedAt = this._mediaTime();
        await this.audioCtx.suspend();
      } else {
        this.wallTick = performance.now();
        await this.audioCtx.resume();
      }
      this._emitState();
    }

    stop() {
      this.generation += 1;
      if (this.ws) {
        const sock = this.ws;
        this.ws = null;
        try { sock.close(); } catch (err) { /* already gone */ }
      }
      if (this.abort) this.abort.abort();
      this.abort = null;
      if (this.raf) cancelAnimationFrame(this.raf);
      this.raf = 0;
      if (this.jsmpeg) {
        try { this.jsmpeg.destroy(); } catch (err) { /* already gone */ }
        this.jsmpeg = null;
      }
      this._closeDecoders();
      this.pendingVideo = [];
      this.pendingAudio = [];
      this.frames = [];
      this.fetchDone = false;
    }

    _closeDecoders() {
      for (const frame of this.frames || []) frame.frame.close();
      this.frames = [];
      if (this.vDecoder && this.vDecoder.state !== "closed") {
        try { this.vDecoder.close(); } catch (err) { /* ignore */ }
      }
      if (this.aDecoder && this.aDecoder.state !== "closed") {
        try { this.aDecoder.close(); } catch (err) { /* ignore */ }
      }
      this.vDecoder = null;
      this.aDecoder = null;
      this.mp4 = null;
    }

    _emitState() {
      if (this.hooks.onState) this.hooks.onState(this.paused ? "paused" : "playing");
    }

    _emitTime() {
      if (this.hooks.onTime) this.hooks.onTime(this.base + this._mediaTime(), this.duration);
    }

    _fail(error) {
      const message = error && error.message ? error.message : String(error);
      this._status(message);
      if (this.hooks.onError) this.hooks.onError(message);
    }

    _status(text) {
      if (this.hooks.onStatus) this.hooks.onStatus(text || "");
    }

    _mediaTime() {
      if (this.paused && this.pausedAt != null) return this.pausedAt;
      if (this.audioCtx && this.audioOrigin != null && this.audioCtx.state === "running") {
        return Math.max(0, this.audioCtx.currentTime - this.audioOrigin);
      }
      return Math.max(0, (performance.now() - (this.wallTick || performance.now())) / 1000 + (this.wallAccum || 0));
    }

    _playCompat(opts) {
      const url = opts.compatUrl;
      this.fit();
      this.jsmpeg = new JSMpeg.Player(url, {
        canvas: this.canvas,
        autoplay: true,
        audio: true,
        disableGl: false,
        videoBufferSize: 2 * 1024 * 1024,
        audioBufferSize: 256 * 1024,
        pauseWhenHidden: false,
      });
      this.setVolume(this.volume);
      this.wallTick = performance.now();
      this.wallAccum = 0;
      this._emitState();
      const tick = () => {
        if (!this.jsmpeg) return;
        this._emitTime();
        this.raf = requestAnimationFrame(tick);
      };
      this.raf = requestAnimationFrame(tick);
    }

    async _playWebCodecs(opts) {
      const gen = this.generation;
      this.abort = new AbortController();
      this.offset = 0;
      this.pendingVideo = [];
      this.pendingAudio = [];
      this.frames = [];
      this.audioOrigin = null;
      this.wallTick = performance.now();
      this.wallAccum = 0;
      this.pausedAt = null;
      this.fetchDone = false;
      this.sawMedia = false;
      this.pumpPaused = false;
      this.held = false;
      this.mp4 = MP4Box.createFile();
      this.mp4.onError = (err) => { if (gen === this.generation) this._fail(err); };
      this.mp4.onReady = (info) => { if (gen === this.generation) this._onReady(info, gen); };
      this.mp4.onSamples = (id, user, samples) => { if (gen === this.generation) this._onSamples(user, samples); };
      this._status("Starting the stream…");

      if (opts.wsUrl) {
        const opened = await this._openSocket(opts.wsUrl, gen);
        if (opened || gen !== this.generation) return;
      }
      await this._openFetch(opts, gen);
    }

    _openSocket(url, gen) {
      return new Promise((resolve) => {
        let sock;
        let settled = false;
        const finish = (opened) => {
          if (settled) return;
          settled = true;
          resolve(opened);
        };
        try {
          sock = new WebSocket(url);
        } catch (err) {
          finish(false);
          return;
        }
        this.ws = sock;
        sock.binaryType = "arraybuffer";
        sock.addEventListener("open", () => {
          this._status("Buffering…");
          this._emitState();
          this._loop(gen);
          finish(true);
        });
        sock.addEventListener("error", () => {
          if (!settled) {
            if (this.ws === sock) this.ws = null;
            finish(false);
          }
        });
        sock.addEventListener("close", () => {
          if (!settled) {
            if (this.ws === sock) this.ws = null;
            finish(false);
            return;
          }
          if (gen !== this.generation) return;
          this.fetchDone = true;
          if (!this.sawMedia) this._fail(new Error("Stream ended before playback started"));
          else this._finishDecoders();
        });
        sock.addEventListener("message", (event) => {
          if (gen !== this.generation) return;
          if (typeof event.data === "string") {
            let msg = {};
            try { msg = JSON.parse(event.data); } catch (err) { return; }
            if (msg.error) this._fail(new Error(msg.error));
            return;
          }
          this.sawMedia = true;
          this._append(new Uint8Array(event.data));
        });
      });
    }

    async _openFetch(opts, gen) {
      this._status("Buffering…");
      let response;
      try {
        response = await fetch(opts.url, { signal: this.abort.signal, credentials: "same-origin" });
      } catch (err) {
        if (err.name !== "AbortError") this._fail(err);
        return;
      }
      if (gen !== this.generation) return;
      if (!response.ok) {
        let detail = "Stream failed";
        try {
          const body = await response.json();
          detail = body.detail || detail;
        } catch (err) { /* not json */ }
        this._fail(new Error(typeof detail === "string" ? detail : "Stream failed"));
        return;
      }
      this._emitState();
      this._loop(gen);
      const reader = response.body.getReader();
      try {
        while (gen === this.generation) {
          while (this.pumpPaused && gen === this.generation) {
            await new Promise((resolve) => setTimeout(resolve, 80));
          }
          const { done, value } = await reader.read();
          if (done) break;
          this.sawMedia = true;
          this._append(value);
        }
      } catch (err) {
        if (err.name !== "AbortError" && gen === this.generation) this._fail(err);
        return;
      }
      if (gen !== this.generation) return;
      this.fetchDone = true;
      this._finishDecoders();
    }

    _finishDecoders() {
      try { this.mp4.flush(); } catch (err) { /* already flushed */ }
      if (this.vDecoder && this.vDecoder.state === "configured") this.vDecoder.flush().catch(() => {});
      if (this.aDecoder && this.aDecoder.state === "configured") this.aDecoder.flush().catch(() => {});
    }

    _append(value) {
      const copy = value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
      copy.fileStart = this.offset;
      this.offset += copy.byteLength;
      this.mp4.appendBuffer(copy);
    }

    _onReady(info, gen) {
      const video = info.videoTracks[0];
      const audio = info.audioTracks[0];
      if (!video) {
        this._fail(new Error("No video track in the stream"));
        return;
      }
      const vDesc = descriptionFor(this.mp4, video, "video");
      this.vDecoder = new VideoDecoder({
        output: (frame) => {
          if (gen !== this.generation) { frame.close(); return; }
          this.frames.push({ frame, ts: frame.timestamp / 1e6 });
          this._pumpDecode();
        },
        error: (err) => { if (gen === this.generation) this._fail(err); },
      });
      this.vDecoder.configure({
        codec: video.codec,
        codedWidth: video.video.width,
        codedHeight: video.video.height,
        description: vDesc || undefined,
        hardwareAcceleration: "prefer-hardware",
        optimizeForLatency: false,
      });
      this.mp4.setExtractionOptions(video.id, "video", { nbSamples: 60 });

      if (audio && typeof AudioDecoder === "function") {
        const aDesc = descriptionFor(this.mp4, audio, "audio");
        this.aDecoder = new AudioDecoder({
          output: (data) => {
            if (gen !== this.generation) { data.close(); return; }
            this._scheduleAudio(data);
          },
          error: (err) => { if (gen === this.generation) this._fail(err); },
        });
        try {
          this.aDecoder.configure({
            codec: audio.codec,
            sampleRate: audio.audio.sample_rate,
            numberOfChannels: audio.audio.channel_count,
            description: aDesc || undefined,
          });
          this.mp4.setExtractionOptions(audio.id, "audio", { nbSamples: 60 });
        } catch (err) {
          this.aDecoder = null;
        }
      }
      this.mp4.start();
    }

    _onSamples(user, samples) {
      const bucket = user === "video" ? this.pendingVideo : this.pendingAudio;
      for (const sample of samples) {
        const Ctor = user === "video" ? EncodedVideoChunk : EncodedAudioChunk;
        bucket.push(new Ctor({
          type: sample.is_sync ? "key" : "delta",
          timestamp: Math.round((sample.cts * 1e6) / sample.timescale),
          duration: Math.round((sample.duration * 1e6) / sample.timescale),
          data: sample.data.slice(),
        }));
      }
      this.pumpPaused = this.pendingVideo.length > 150;
      this._flowControl();
      this._pumpDecode();
    }

    _flowControl() {
      if (!this.ws || this.ws.readyState !== 1) return;
      const hold = this.pendingVideo.length > 150;
      const release = this.pendingVideo.length < 40;
      if (hold && !this.held) {
        this.held = true;
        try { this.ws.send("hold"); } catch (err) { /* socket closing */ }
      } else if (release && this.held) {
        this.held = false;
        try { this.ws.send("go"); } catch (err) { /* socket closing */ }
      }
    }

    _pumpDecode() {
      if (this.vDecoder && this.vDecoder.state === "configured") {
        while (this.frames.length < 6 && this.vDecoder.decodeQueueSize < 8 && this.pendingVideo.length) {
          this.vDecoder.decode(this.pendingVideo.shift());
        }
      }
      if (this.aDecoder && this.aDecoder.state === "configured") {
        while (this.aDecoder.decodeQueueSize < 12 && this.pendingAudio.length) {
          this.aDecoder.decode(this.pendingAudio.shift());
        }
      }
      if (this.pendingVideo.length < 40) this.pumpPaused = false;
      this._flowControl();
    }

    _scheduleAudio(audioData) {
      try {
        const frames = audioData.numberOfFrames;
        const channels = audioData.numberOfChannels;
        const buffer = this.audioCtx.createBuffer(channels, frames, audioData.sampleRate);
        for (let c = 0; c < channels; c++) {
          const plane = new Float32Array(frames);
          audioData.copyTo(plane, { planeIndex: c, format: "f32-planar" });
          buffer.copyToChannel(plane, c);
        }
        const ts = audioData.timestamp / 1e6;
        if (this.audioOrigin == null) this.audioOrigin = this.audioCtx.currentTime - ts + 0.15;
        const when = this.audioOrigin + ts;
        if (when >= this.audioCtx.currentTime - 0.05) {
          const src = this.audioCtx.createBufferSource();
          src.buffer = buffer;
          src.connect(this.gain);
          src.start(Math.max(when, this.audioCtx.currentTime));
        }
      } catch (err) {
        this._fail(err);
      } finally {
        audioData.close();
      }
    }

    _loop(gen) {
      const draw = () => {
        if (gen !== this.generation) return;
        this.fit();
        if (!this.paused) {
          const t = this._mediaTime();
          let chosen = null;
          while (this.frames.length && this.frames[0].ts <= t + 0.01) {
            if (chosen) chosen.frame.close();
            chosen = this.frames.shift();
          }
          if (chosen) {
            this._status("");
            const frame = chosen.frame;
            const cw = this.canvas.width;
            const ch = this.canvas.height;
            const scale = Math.min(cw / frame.displayWidth, ch / frame.displayHeight);
            const w = frame.displayWidth * scale;
            const h = frame.displayHeight * scale;
            this.ctx.fillStyle = "#000";
            this.ctx.fillRect(0, 0, cw, ch);
            this.ctx.drawImage(frame, (cw - w) / 2, (ch - h) / 2, w, h);
            frame.close();
          }
          this._pumpDecode();
          this._emitTime();
          if (this.fetchDone && !this.pendingVideo.length && !this.frames.length && this.vDecoder && this.vDecoder.decodeQueueSize === 0) {
            if (this.duration && this.base + t > this.duration - 0.4) {
              if (this.hooks.onEnded) this.hooks.onEnded();
            }
          }
        }
        this.raf = requestAnimationFrame(draw);
      };
      this.raf = requestAnimationFrame(draw);
    }
  }

  window.VelaPlayer = { CabinPlayer, webCodecsAvailable };
})();
