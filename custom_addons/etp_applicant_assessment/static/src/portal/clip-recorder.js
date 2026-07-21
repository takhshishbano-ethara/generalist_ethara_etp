const MIME_CANDIDATES = [
    "video/webm;codecs=vp8,opus",
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8",
    "video/webm",
];

export class ClipRecorder {
    constructor(stream, { seconds = 7 } = {}) {
        this.stream = stream;
        this.seconds = seconds;
        this._busy = false;
        this._active = null;
        this._stopTimer = null;
        this._mimeType = ClipRecorder._pickMime();
    }

    static isSupported() {
        return typeof window !== "undefined"
            && typeof window.MediaRecorder !== "undefined"
            && ClipRecorder._pickMime() !== null;
    }

    static _pickMime() {
        if (typeof window === "undefined" || !window.MediaRecorder) return null;
        for (const m of MIME_CANDIDATES) {
            if (window.MediaRecorder.isTypeSupported(m)) return m;
        }
        return null;
    }

    get mimeType() { return this._mimeType; }

    /* Finalize the in-flight recording immediately (partial clip) so the
       page can submit/navigate without losing the evidence entirely. */
    flush() {
        if (this._stopTimer) {
            clearTimeout(this._stopTimer);
            this._stopTimer = null;
        }
        const rec = this._active;
        if (rec && rec.state !== "inactive") {
            try { rec.stop(); } catch (e) {}
        }
    }

    captureClip() {
        if (this._busy) return Promise.resolve(null);
        if (!this._mimeType) return Promise.resolve(null);
        this._busy = true;
        return new Promise((resolve) => {
            let recorder;
            const chunks = [];
            const done = (blob) => {
                this._busy = false;
                this._active = null;
                if (this._stopTimer) {
                    clearTimeout(this._stopTimer);
                    this._stopTimer = null;
                }
                resolve(blob);
            };
            try {
                recorder = new MediaRecorder(this.stream, { mimeType: this._mimeType });
            } catch (e) {
                done(null);
                return;
            }
            this._active = recorder;
            recorder.ondataavailable = (evt) => {
                if (evt.data && evt.data.size > 0) chunks.push(evt.data);
            };
            recorder.onerror = () => done(null);
            recorder.onstop = () => {
                if (!chunks.length) { done(null); return; }
                const blob = new Blob(chunks, { type: this._mimeType });
                done(blob.size > 0 ? blob : null);
            };
            try {
                recorder.start();
                this._stopTimer = setTimeout(() => {
                    if (recorder.state !== "inactive") {
                        try { recorder.stop(); } catch (e) {}
                    }
                }, this.seconds * 1000);
            } catch (e) {
                done(null);
            }
        });
    }
}
