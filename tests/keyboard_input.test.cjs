const test = require("node:test");
const assert = require("node:assert/strict");
const RemoteKeyboard = require("../web/keyboard-input.js");

function setup() {
  const sent = [];
  return { sent, keyboard: new RemoteKeyboard(p => sent.push([p.key, p.action])) };
}

test("Ctrl+C keeps Control down until its keyup", () => {
  const { sent, keyboard } = setup();
  keyboard.down({ key: "Control", ctrlKey: true });
  keyboard.down({ key: "c", ctrlKey: true });
  keyboard.up({ key: "c", ctrlKey: true });
  keyboard.up({ key: "Control" });
  assert.deepEqual(sent, [["ctrl", "down"], ["c", "down"], ["c", "up"], ["ctrl", "up"]]);
});

test("synchronizes modifiers when the shortcut starts outside the remote surface", () => {
  const { sent, keyboard } = setup();
  keyboard.down({ key: "C", ctrlKey: true, shiftKey: true });
  keyboard.releaseAll();
  assert.deepEqual(sent, [
    ["ctrl", "down"], ["shift", "down"], ["c", "down"],
    ["c", "up"], ["shift", "up"], ["ctrl", "up"]
  ]);
});

test("forwards physical repeats but ignores duplicate releases", () => {
  const { sent, keyboard } = setup();
  keyboard.down({ key: "a" });
  keyboard.down({ key: "a", repeat: true });
  keyboard.up({ key: "a" });
  assert.equal(keyboard.up({ key: "a" }), false);
  assert.deepEqual(sent, [["a", "down"], ["a", "down"], ["a", "up"]]);
});

test("blur releases held keys once and disconnect clears without more input", () => {
  const { sent, keyboard } = setup();
  keyboard.down({ key: "Alt", altKey: true });
  keyboard.down({ key: "Tab", altKey: true });
  keyboard.releaseAll();
  keyboard.releaseAll();
  assert.deepEqual(sent, [["alt", "down"], ["tab", "down"], ["tab", "up"], ["alt", "up"]]);
  keyboard.down({ key: "Shift", shiftKey: true });
  keyboard.clear();
  assert.equal(keyboard.up({ key: "Shift" }), false);
});

test("leaves composition and unsupported browser keys untouched", () => {
  const { sent, keyboard } = setup();
  for (const event of [{ key: "Dead" }, { key: "Process" }, { key: "é" }, { key: "a", isComposing: true }]) {
    assert.equal(keyboard.down(event), false);
  }
  assert.equal(sent.length, 0);
});

test("releases a missed modifier before sending the next unmodified key", () => {
  const { sent, keyboard } = setup();
  keyboard.down({ key: "Control", ctrlKey: true });
  keyboard.down({ key: "x", ctrlKey: false });
  assert.deepEqual(sent, [["ctrl", "down"], ["ctrl", "up"], ["x", "down"]]);
});

test("shifted symbols use their base key without releasing Shift implicitly on the host", () => {
  const { sent, keyboard } = setup();
  keyboard.down({ key: "!", code: "Digit1", shiftKey: true });
  keyboard.up({ key: "1", code: "Digit1" });
  keyboard.releaseAll();
  assert.deepEqual(sent, [["shift", "down"], ["1", "down"], ["1", "up"], ["shift", "up"]]);
});
