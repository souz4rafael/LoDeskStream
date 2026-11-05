(function (root) {
  const aliases = {
    Control: "ctrl", Shift: "shift", Alt: "alt", Meta: "win",
    ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down",
    Escape: "esc", " ": "space", Enter: "enter", Backspace: "backspace",
    Tab: "tab", Delete: "delete", Insert: "insert", Home: "home", End: "end",
    PageUp: "pageup", PageDown: "pagedown", CapsLock: "capslock",
    NumLock: "numlock", ScrollLock: "scrolllock", Pause: "pause", PrintScreen: "printscreen"
  };
  const modifiers = [
    ["ctrl", "ctrlKey"], ["shift", "shiftKey"], ["alt", "altKey"], ["win", "metaKey"]
  ];
  const physicalSymbols = {
    Backquote: "`", Minus: "-", Equal: "=", BracketLeft: "[", BracketRight: "]",
    Backslash: "\\", Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/"
  };

  class RemoteKeyboard {
    constructor(send) {
      this.send = send;
      this.held = new Set();
    }

    normalize(event) {
      if (event.isComposing || !event.key) return null;
      if (aliases[event.key]) return aliases[event.key];
      if (/^Digit[0-9]$/.test(event.code || "")) return event.code.slice(-1);
      if (physicalSymbols[event.code]) return physicalSymbols[event.code];
      if (/^F([1-9]|1[0-9]|2[0-4])$/.test(event.key)) return event.key.toLowerCase();
      if (/^[\x21-\x7e]$/.test(event.key)) return event.key.toLowerCase();
      return null;
    }

    down(event) {
      const key = this.normalize(event);
      if (!key) return false;
      for (const modifier of modifiers) {
        if (modifier[0] === key) continue;
        if (event[modifier[1]] && !this.held.has(modifier[0])) {
          this.send({ type: "key", key: modifier[0], action: "down" });
          this.held.add(modifier[0]);
        } else if (!event[modifier[1]] && this.held.has(modifier[0])) {
          this.send({ type: "key", key: modifier[0], action: "up" });
          this.held.delete(modifier[0]);
        }
      }
      if (!this.held.has(key) || event.repeat) {
        this.send({ type: "key", key, action: "down" });
        this.held.add(key);
      }
      return true;
    }

    up(event) {
      const key = this.normalize(event);
      if (!key || !this.held.has(key)) return false;
      this.send({ type: "key", key, action: "up" });
      this.held.delete(key);
      return true;
    }

    releaseAll() {
      for (const key of Array.from(this.held).reverse()) {
        this.send({ type: "key", key, action: "up" });
      }
      this.clear();
    }

    clear() {
      this.held.clear();
    }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = RemoteKeyboard;
  } else {
    root.RemoteKeyboard = RemoteKeyboard;
  }
})(typeof window !== "undefined" ? window : this);
