const test = require("node:test");
const assert = require("node:assert/strict");
const InputQueue = require("../web/input-queue.js");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

test("serializes requests and coalesces only adjacent moves", async () => {
  const first = deferred();
  const sent = [];
  let inFlight = 0;
  let maxInFlight = 0;
  const queue = new InputQueue(async (payload) => {
    sent.push(payload);
    maxInFlight = Math.max(maxInFlight, ++inFlight);
    if (sent.length === 1) await first.promise;
    inFlight--;
  }, assert.fail);
  const results = [
    queue.enqueue({ type: "mouse_down", monitorIndex: 1 }),
    queue.enqueue({ type: "mouse_move", x: 0.1, monitorIndex: 1 }),
    queue.enqueue({ type: "mouse_move", x: 0.2, monitorIndex: 1 }),
    queue.enqueue({ type: "mouse_up", monitorIndex: 1 }),
    queue.enqueue({ type: "mouse_move", x: 0.3, monitorIndex: 1 })
  ];
  assert.equal(sent.length, 1);
  first.resolve();
  assert.deepEqual(await Promise.all(results), [true, false, true, true, true]);
  assert.equal(maxInFlight, 1);
  assert.deepEqual(sent.map(p => p.type), ["mouse_down", "mouse_move", "mouse_up", "mouse_move"]);
  assert.deepEqual(sent.map(p => p.sequence), [1, 2, 3, 4]);
  assert.equal(sent[1].x, 0.2);
});

test("does not coalesce across monitor changes or key events", async () => {
  const first = deferred();
  const sent = [];
  const queue = new InputQueue(async (p) => {
    sent.push(p);
    if (sent.length === 1) await first.promise;
  }, assert.fail);
  const results = [
    queue.enqueue({ type: "text", text: "a" }),
    queue.enqueue({ type: "mouse_move", monitorIndex: 1 }),
    queue.enqueue({ type: "mouse_move", monitorIndex: 2 }),
    queue.enqueue({ type: "key", key: "enter" }),
    queue.enqueue({ type: "text", text: "b" })
  ];
  first.resolve();
  await Promise.all(results);
  assert.equal(sent.length, 5);
  assert.equal(sent[3].key, "enter");
  assert.equal(sent[4].text, "b");
});

test("a failed request stops the queue instead of skipping a press", async () => {
  const first = deferred();
  const sent = [];
  const errors = [];
  const queue = new InputQueue(async (p) => {
    sent.push(p);
    await first.promise;
    throw new Error("Request failed");
  }, error => errors.push(error));
  const down = queue.enqueue({ type: "mouse_down" });
  const up = queue.enqueue({ type: "mouse_up" });
  first.resolve();
  assert.deepEqual(await Promise.all([down, up]), [false, false]);
  assert.equal(sent.length, 1);
  assert.equal(errors.length, 1);
  assert.equal(await queue.enqueue({ type: "key" }), false);
});

test("timeout stops the queue and reports the failure", async () => {
  const errors = [];
  const queue = new InputQueue(() => new Promise(() => {}), error => errors.push(error), 10);
  const first = queue.enqueue({ type: "mouse_down" });
  const second = queue.enqueue({ type: "mouse_up" });
  assert.deepEqual(await Promise.all([first, second]), [false, false]);
  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /timed out/);
});

test("stopping an old session discards pending input without affecting a new queue", async () => {
  const gate = deferred();
  const oldSent = [];
  const oldQueue = new InputQueue(async p => { oldSent.push(p); await gate.promise; }, assert.fail);
  const oldDown = oldQueue.enqueue({ type: "mouse_down" });
  const oldUp = oldQueue.enqueue({ type: "mouse_up" });
  oldQueue.stop();
  const newSent = [];
  const next = new InputQueue(async p => { newSent.push(p); }, assert.fail);
  assert.equal(await next.enqueue({ type: "mouse_click" }), true);
  gate.resolve();
  assert.deepEqual(await Promise.all([oldDown, oldUp]), [false, false]);
  assert.equal(oldSent.length, 1);
  assert.equal(newSent[0].sequence, 1);
});

test("overflow reports failure and stops all pending input", async () => {
  const gate = deferred();
  const errors = [];
  const queue = new InputQueue(() => gate.promise, error => errors.push(error));
  const results = [];
  for (let i = 0; i < 258; i++) results.push(queue.enqueue({ type: "key", key: "a" }));
  gate.resolve();
  assert.equal((await Promise.all(results)).every(value => value === false), true);
  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /overflow/);
});
