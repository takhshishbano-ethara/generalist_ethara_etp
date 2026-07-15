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

    captureClip() {
        if (this._busy) return Promise.resolve(null);
        if (!this._mimeType) return Promise.resolve(null);
        this._busy = true;
        return new Promise((resolve) => {
            let recorder;
            const chunks = [];
            try {
                recorder = new MediaRecorder(this.stream, { mimeType: this._mimeType });
            } catch (e) {
                this._busy = false;
                resolve(null);
                return;
            }
            recorder.ondataavailable = (evt) => {
                if (evt.data && evt.data.size > 0) chunks.push(evt.data);
            };
            recorder.onerror = () => {
                this._busy = false;
                resolve(null);
            };
            recorder.onstop = () => {
                this._busy = false;
                if (!chunks.length) { resolve(null); return; }
                const blob = new Blob(chunks, { type: this._mimeType });
                resolve(blob.size > 0 ? blob : null);
            };
            try {
                recorder.start();
                setTimeout(() => {
                    if (recorder.state !== "inactive") {
                        try { recorder.stop(); } catch (e) {}
                    }
                }, this.seconds * 1000);
            } catch (e) {
                this._busy = false;
                resolve(null);
            }
        });
    }
}
