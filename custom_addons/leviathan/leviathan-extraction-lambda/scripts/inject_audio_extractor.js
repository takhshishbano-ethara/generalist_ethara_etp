// Audio Extractor - Early Hook
// Must be injected via page.addInitScript() BEFORE navigation.
// Patches Web Audio API and HTML Audio to capture all audio file usage.
(() => {
  const captured = {
    audioFiles: [],           // {url, format, trigger, scrollPosition, timestamp}
    audioElements: [],        // DOM <audio> elements found
    playEvents: [],           // {src, scrollPercent, timestamp} - tracked at actual play time
    webAudioUsage: false,
    audioLibrary: null,       // 'howler' | 'tone' | 'web-audio' | null
    spatialAudio: false,
    audioContexts: [],        // {sampleRate, state, baseLatency}
    bufferSources: [],        // {startTime, offset, duration, loop, playbackRate, scrollPercent}
    howlerSounds: [],         // Howler.js specific
  };

  const audioUrlSet = new Set();
  let currentScrollY = 0;

  // Track scroll position for audio-to-scroll mapping
  if (typeof window !== 'undefined') {
    window.addEventListener('scroll', () => {
      currentScrollY = window.scrollY || document.documentElement.scrollTop;
    }, { passive: true });
  }

  function recordAudioUrl(url, trigger, extra) {
    if (!url || audioUrlSet.has(url)) return;
    audioUrlSet.add(url);
    const entry = {
      url,
      format: guessFormat(url),
      trigger: trigger || 'unknown',
      scrollPosition: currentScrollY,
      scrollPercent: getScrollPercent(),
      timestamp: Date.now(),
      ...extra,
    };
    captured.audioFiles.push(entry);
  }

  function guessFormat(url) {
    if (!url) return 'unknown';
    const ext = url.split('?')[0].split('.').pop().toLowerCase();
    const formats = { mp3: 'mp3', ogg: 'ogg', wav: 'wav', m4a: 'm4a', webm: 'webm', aac: 'aac', flac: 'flac' };
    return formats[ext] || 'unknown';
  }

  function getScrollPercent() {
    const docHeight = Math.max(
      document.body.scrollHeight || 0,
      document.documentElement.scrollHeight || 0
    );
    const viewHeight = window.innerHeight || 0;
    const scrollable = docHeight - viewHeight;
    if (scrollable <= 0) return 0;
    return Math.round((currentScrollY / scrollable) * 100);
  }

  // Patch AudioContext constructor
  const OrigAudioContext = window.AudioContext || window.webkitAudioContext;
  if (OrigAudioContext) {
    const PatchedAudioContext = function(...args) {
      const ctx = new OrigAudioContext(...args);
      captured.webAudioUsage = true;
      captured.audioContexts.push({
        sampleRate: ctx.sampleRate,
        state: ctx.state,
        baseLatency: ctx.baseLatency,
      });
      return ctx;
    };
    PatchedAudioContext.prototype = OrigAudioContext.prototype;
    window.AudioContext = PatchedAudioContext;
    if (window.webkitAudioContext) window.webkitAudioContext = PatchedAudioContext;
  }

  // Patch decodeAudioData to capture audio buffer loads
  if (OrigAudioContext) {
    const origDecode = OrigAudioContext.prototype.decodeAudioData;
    OrigAudioContext.prototype.decodeAudioData = function(arrayBuffer, ...args) {
      captured.webAudioUsage = true;
      return origDecode.call(this, arrayBuffer, ...args);
    };
  }

  // Patch createBufferSource to track playback
  if (OrigAudioContext) {
    const origCreateBufferSource = OrigAudioContext.prototype.createBufferSource;
    OrigAudioContext.prototype.createBufferSource = function() {
      const source = origCreateBufferSource.call(this);
      const origStart = source.start.bind(source);

      source.start = function(...args) {
        captured.bufferSources.push({
          startTime: args[0] || 0,
          offset: args[1] || 0,
          duration: args[2] || null,
          loop: source.loop,
          playbackRate: source.playbackRate?.value || 1,
          scrollPosition: currentScrollY,
          scrollPercent: getScrollPercent(),
          timestamp: Date.now(),
        });
        return origStart(...args);
      };
      return source;
    };
  }

  // Patch HTML Audio element
  const OrigAudio = window.Audio;
  if (OrigAudio) {
    window.Audio = function(src) {
      const audio = new OrigAudio(src);
      if (src) recordAudioUrl(src, 'Audio_constructor');

      const origPlay = audio.play.bind(audio);
      audio.play = function() {
        recordAudioUrl(audio.src || audio.currentSrc, 'Audio.play');
        return origPlay();
      };
      return audio;
    };
    window.Audio.prototype = OrigAudio.prototype;
  }

  // Patch HTMLMediaElement.prototype.play for <audio> elements in DOM
  const origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function() {
    if (this.tagName === 'AUDIO' || this.tagName === 'VIDEO') {
      recordAudioUrl(this.src || this.currentSrc, 'element.play', {
        loop: this.loop,
        volume: this.volume,
        muted: this.muted,
      });
    }
    const scrollPercent = getScrollPercent();
    captured.playEvents.push({
      src: this.src || this.currentSrc || '',
      scrollPercent,
      scrollY: currentScrollY,
      timestamp: Date.now(),
      loop: this.loop || false,
      volume: this.volume,
      tagName: this.tagName,
    });
    return origPlay.call(this);
  };

  // Detect Howler.js
  Object.defineProperty(window, 'Howl', {
    configurable: true,
    set(val) {
      captured.audioLibrary = 'howler';
      const OrigHowl = val;
      const PatchedHowl = function(options) {
        if (options && options.src) {
          const srcs = Array.isArray(options.src) ? options.src : [options.src];
          srcs.forEach(s => recordAudioUrl(s, 'Howler', {
            loop: options.loop || false,
            volume: options.volume || 1,
            sprite: options.sprite ? Object.keys(options.sprite) : null,
          }));
          captured.howlerSounds.push({
            src: srcs,
            loop: options.loop || false,
            volume: options.volume || 1,
            rate: options.rate || 1,
            sprite: options.sprite || null,
            spatial: options.pannerAttr ? true : false,
          });
          if (options.pannerAttr) captured.spatialAudio = true;
        }
        return new OrigHowl(options);
      };
      PatchedHowl.prototype = OrigHowl.prototype;
      Object.defineProperty(window, 'Howl', { value: PatchedHowl, configurable: true, writable: true });
    },
    get() { return undefined; }
  });

  // Detect Tone.js
  Object.defineProperty(window, 'Tone', {
    configurable: true,
    set(val) {
      captured.audioLibrary = 'tone';
      Object.defineProperty(window, 'Tone', { value: val, configurable: true, writable: true });
    },
    get() { return undefined; }
  });

  // Expose captured data
  window.__audioCapture = captured;

  // Post-load scan for <audio> elements and performance entries
  window.addEventListener('load', () => {
    // Scan DOM for audio elements
    document.querySelectorAll('audio, audio source, [data-audio], [data-sound], [data-src*=".mp3"], [data-src*=".ogg"], [data-src*=".wav"]').forEach(el => {
      const src = el.src || el.dataset?.audio || el.dataset?.sound || el.dataset?.src || '';
      if (src) recordAudioUrl(src, 'dom_element');
      captured.audioElements.push({
        tag: el.tagName,
        src: src,
        loop: el.loop || false,
        autoplay: el.autoplay || false,
        preload: el.preload || 'auto',
      });
    });

    // Scan performance entries for audio files
    const audioExts = /\.(mp3|ogg|wav|m4a|webm|aac|flac)(\?|$)/i;
    const entries = performance.getEntriesByType('resource') || [];
    entries.forEach(entry => {
      if (audioExts.test(entry.name)) {
        recordAudioUrl(entry.name, 'performance_entry', {
          transferSize: entry.transferSize,
          duration: entry.duration,
        });
      }
    });

    // Detect library if not already detected
    if (!captured.audioLibrary) {
      if (window.Howl || window.Howler) captured.audioLibrary = 'howler';
      else if (window.Tone) captured.audioLibrary = 'tone';
      else if (captured.webAudioUsage) captured.audioLibrary = 'web-audio';
    }

    // Check for Three.js positional audio
    try {
      const canvas = document.querySelector('canvas');
      if (canvas && canvas.__r3f) {
        const state = canvas.__r3f.getState ? canvas.__r3f.getState() : canvas.__r3f;
        if (state && state.scene) {
          state.scene.traverse(obj => {
            if (obj.type === 'Audio' || obj.type === 'PositionalAudio') {
              captured.spatialAudio = true;
              if (obj.buffer) {
                captured.bufferSources.push({
                  type: obj.type,
                  loop: obj.loop,
                  volume: obj.volume?.gain?.value || 1,
                  isPlaying: obj.isPlaying,
                });
              }
            }
          });
        }
      }
    } catch(e) {}
  });
})();
