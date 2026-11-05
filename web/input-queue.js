(function (root) {
  class InputQueue {
    constructor(transmit, onError, timeoutMs = 5000) {
      this.transmit = transmit;
      this.onError = onError;
      this.timeoutMs = timeoutMs;
      this.pending = [];
      this.running = false;
      this.stopped = false;
      this.sequence = 0;
    }

    enqueue(payload) {
      if (this.stopped) return Promise.resolve(false);
      return new Promise((resolve) => {
        const last = this.pending[this.pending.length - 1];
        // Only adjacent moves may be replaced; presses and releases are barriers.
        if (payload.type === "mouse_move" && last && last.payload.type === "mouse_move" &&
            last.payload.monitorIndex === payload.monitorIndex) {
          last.resolve(false);
          this.pending[this.pending.length - 1] = { payload, resolve };
        } else {
          this.pending.push({ payload, resolve });
        }
        if (this.pending.length > 256) {
          this.fail(new Error("Input queue overflow. Reconnect."));
          return;
        }
        this.drain();
      });
    }

    stop() {
      this.stopped = true;
      for (const item of this.pending) item.resolve(false);
      this.pending = [];
    }

    fail(error) {
      if (this.stopped) return;
      this.stop();
      this.onError(error);
    }

    async drain() {
      if (this.running || this.stopped) return;
      this.running = true;
      try {
        while (!this.stopped && this.pending.length) {
          const item = this.pending.shift();
          let timeoutId = 0;
          try {
            const payload = Object.assign({}, item.payload, { sequence: ++this.sequence });
            await Promise.race([
              this.transmit(payload),
              new Promise((resolve, reject) => {
                timeoutId = setTimeout(() => reject(new Error("Input request timed out. Reconnect.")), this.timeoutMs);
              })
            ]);
            item.resolve(!this.stopped);
          } catch (error) {
            item.resolve(false);
            this.fail(error);
          } finally {
            clearTimeout(timeoutId);
          }
        }
      } finally {
        this.running = false;
      }
    }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = InputQueue;
  } else {
    root.InputQueue = InputQueue;
  }
})(typeof window !== "undefined" ? window : this);
