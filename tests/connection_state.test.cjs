const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const InputQueue = require("../web/input-queue.js");
const RemoteKeyboard = require("../web/keyboard-input.js");

function app(timers = { setTimeout, clearTimeout }) {
  const elements = {};
  let created = 0;
  function element(id) {
    if (elements[id]) return elements[id];
    const classes = new Set();
    const node = {
      id, style: {}, dataset: {}, value: "", handlers: {}, options: [], disabled: false,
      classList: {
        add: value => classes.add(value),
        remove: value => classes.delete(value),
        contains: value => classes.has(value),
        toggle: (value, enabled) => enabled ? classes.add(value) : classes.delete(value)
      },
      addEventListener(name, callback) { (this.handlers[name] = this.handlers[name] || []).push(callback); },
      setAttribute(name, value) { this[name] = value; },
      removeAttribute(name) { delete this[name]; },
      getAttribute(name) { return this[name]; },
      getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 600 }; },
      appendChild(child) {
        child.parentElement = this;
        if (id === "monitor") {
          this.options.push(child);
          if (this.options.length === 1 || child.selected) this.value = child.value;
        }
      },
      insertBefore(child) { child.parentElement = this; },
      contains(target) { return target === this || (target && this.contains(target.parentElement)); },
      focus() {}, blur() {}, pause() {}, play: async () => {}
    };
    if (id === "monitor") {
      Object.defineProperty(node, "innerHTML", {
        set() { this.options = []; this.value = ""; }
      });
      Object.defineProperty(node, "selectedOptions", {
        get() { return this.options.filter(option => option.value === this.value); }
      });
    }
    elements[id] = node;
    return node;
  }
  const document = {
    body: element("body"),
    getElementById: element,
    createElement: tag => element(`${tag}-${++created}`),
    querySelector: element,
    addEventListener() {}
  };
  const context = vm.createContext({
    document, InputQueue, RemoteKeyboard, console,
    window: { addEventListener() {} }, performance, ...timers,
    requestAnimationFrame: callback => callback(),
    HTMLInputElement: class {}, HTMLTextAreaElement: class {}, HTMLSelectElement: class {}
  });
  const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  vm.runInContext(`
    const realRequestJson = requestJson;
    tokenEl.value = "test-token";
    monitorsLoaded = true;
    monitorEl.value = "1";
    closeControlSession = async () => {};
    scheduleHeartbeat = () => {};
    const fakeRequestJson = async (url) => url === "/api/monitors"
      ? {ok:true,monitors:[{index:1,name:"Display one",width:1920,height:1080,current:true}]}
      : url === "/api/session" ? {ok:true,sessionId:"session"} : {ok:true};
    requestJson = fakeRequestJson;
    startWebRtcStream = async () => { showWebRtcViewer(); remoteAudioAvailable = true; };
    setMjpegStreamSource = () => showFallbackViewer();
  `, context);
  return { context, elements, evaluate: source => vm.runInContext(source, context) };
}

test("browser cursor is a centered translucent touch circle", () => {
  const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
  const cursor = html.match(/<svg id="remoteCursor"[\s\S]*?<\/svg>/)[0];
  assert.match(cursor, /viewBox="0 0 30 30"/);
  assert.equal((cursor.match(/<circle /g) || []).length, 3);
  assert.match(cursor, /fill-opacity="0\.19"/);
  assert.doesNotMatch(cursor, /<path /);
  const style = html.match(/#remoteCursor\s*\{([^}]+)\}/)[1];
  assert.match(style, /transform:\s*translate\(-50%, -50%\)/);
  assert.match(style, /pointer-events:\s*none/);
});

test("monitor control follows Connect and fullscreen menu stays anchored at every edge", () => {
  const html = fs.readFileSync(path.join(__dirname, "../web/index.html"), "utf8");
  const header = html.match(/<header id="controlsBar">([\s\S]*?)<\/header>/)[1];
  assert.deepEqual([...header.matchAll(/<button id="([^"]+)"/g)].slice(0, 4).map(match => match[1]),
    ["connect", "monitorToggle", "streamMode", "fullscreen"]);
  const { evaluate, elements } = app();
  for (const width of [1920, 1024, 568]) {
    evaluate(`
      stage.getBoundingClientRect = () => ({ left: 0, top: 0, width: ${width}, height: 768 });
      stageMenu.getBoundingClientRect = () => ({
        left: parseFloat(stageMenu.style.left) || 0,
        top: parseFloat(stageMenu.style.top) || 0,
        width: 48, height: 56
      });
      stageMenuPanel.scrollHeight = 220;
      stageMenu.classList.add("fullscreen-active");
    `);
    for (const [left, top] of [[width - 48, 10], [width / 2, 384], [0, 0], [width - 48, 712]]) {
      evaluate(`pinStageMenu(${left}, ${top});`);
      const before = { left: elements.stageMenu.style.left, top: elements.stageMenu.style.top };
      for (let count = 0; count < 3; count++) {
        evaluate("setStageMenuOpen(true); setMonitorPanelOpen(true);");
        assert.equal(elements.stageMenu.style.left, before.left);
        assert.equal(elements.stageMenu.style.top, before.top);
        const panelWidth = parseFloat(elements.stageMenuPanel.style.width);
        const panelLeft = left + parseFloat(elements.stageMenuPanel.style.left);
        assert.equal(panelWidth, Math.min(980, width - 20));
        assert.ok(panelLeft >= 0 && panelLeft + panelWidth <= width);
        evaluate("setStageMenuOpen(false);");
        assert.equal(elements.stageMenu.style.left, before.left);
        assert.equal(elements.stageMenu.style.top, before.top);
      }
    }
  }
});

function cursorApp() {
  let now = 0;
  let nextId = 0;
  const pending = new Map();
  const result = app({
    setTimeout(callback, delay) {
      const id = ++nextId;
      pending.set(id, { callback, at: now + delay });
      return id;
    },
    clearTimeout(id) { pending.delete(id); }
  });
  result.evaluate(`
    connected = true;
    connectionState = "connected";
    relativePos = () => ({ x: 0.5, y: 0.5 });
    streamContentRect = () => ({ left: 0, top: 0, width: 1000, height: 600 });
    sendEvent = () => {};
  `);
  return {
    ...result,
    advance(milliseconds) {
      now += milliseconds;
      for (const [id, timer] of pending) {
        if (timer.at <= now) {
          pending.delete(id);
          timer.callback();
        }
      }
    }
  };
}

test("touch circle hides exactly five seconds after the latest click, including legacy touch", () => {
  for (const event of ['{ pointerType: "touch" }', '{ identifier: 0 }']) {
    const { evaluate, elements, advance } = cursorApp();
    evaluate(`startRemoteDrag(${event})`);
    advance(4999);
    assert.equal(elements.remoteCursor.style.display, "block");
    evaluate(`startRemoteDrag(${event})`);
    advance(1);
    assert.equal(elements.remoteCursor.style.display, "block");
    advance(4999);
    assert.equal(elements.remoteCursor.style.display, "none");
    evaluate(`startRemoteDrag(${event})`);
    assert.equal(elements.remoteCursor.style.display, "block");
    advance(5000);
    assert.equal(elements.remoteCursor.style.display, "none");
  }
});

test("mouse and full mouse keep the circle visible and cancel an earlier touch timeout", () => {
  for (const action of [
    'startRemoteDrag({ pointerType: "mouse" })',
    'startRemoteDrag({ button: 0 })',
    'fullMouseEl.handlers.click[0](); startRemoteDrag({ pointerType: "touch" })'
  ]) {
    const { evaluate, elements, advance } = cursorApp();
    evaluate('startRemoteDrag({ pointerType: "touch" })');
    advance(1000);
    evaluate(action);
    advance(5000);
    assert.equal(elements.remoteCursor.style.display, "block");
  }
});

test("disconnect clears the pending touch hide timer", () => {
  const { evaluate, elements, advance } = cursorApp();
  evaluate('startRemoteDrag({ pointerType: "touch" }); disconnect();');
  assert.equal(elements.remoteCursor.style.display, "none");
  evaluate('connected = true; updateRemoteCursor({ pointerType: "mouse" });');
  advance(5000);
  assert.equal(elements.remoteCursor.style.display, "block");
});

test("connection phases consistently render controls and audio availability", async () => {
  const { elements, evaluate } = app();
  assert.equal(evaluate('connectionState === "disconnected" && muteAudioBtn.disabled'), true);
  const connect = elements.connect.handlers.click[0]();
  assert.equal(evaluate('connectionState === "connecting" && connectBtn.disabled && monitorEl.disabled'), true);
  await connect;
  assert.equal(evaluate('connectionState === "connected" && connected && !showKeyboardBtn.disabled && !muteAudioBtn.disabled'), true);
  await elements.streamMode.handlers.click[0]();
  assert.equal(evaluate('connectionState === "connected" && !usingWebRtc && muteAudioBtn.disabled'), true);
  elements.fallbackViewer.handlers.error[0]();
  assert.equal(evaluate('connectionState === "error" && !connected && connectBtn.textContent === "Connect" && showKeyboardBtn.disabled'), true);
});

test("failed connection returns controls to a usable error state", async () => {
  const { elements, evaluate } = app();
  evaluate('requestJson = async () => { throw new Error("Server unavailable"); };');
  await elements.connect.handlers.click[0]();
  assert.equal(evaluate('connectionState === "error" && !connected && !connectBtn.disabled && !tokenEl.disabled'), true);
});

test("late session creation after disconnect is revoked and cannot reconnect", async () => {
  const { elements, context, evaluate } = app();
  let resolveSession;
  context.pendingSession = new Promise(resolve => { resolveSession = resolve; });
  evaluate(`
    let closedLateSession = false;
    requestJson = async (url) => url === "/api/session" ? pendingSession : fakeRequestJson(url);
    closeControlSession = async () => { closedLateSession = true; };
  `);
  const pending = elements.connect.handlers.click[0]();
  await new Promise(resolve => setImmediate(resolve));
  evaluate("disconnect()");
  resolveSession({ ok: true, sessionId: "late-session" });
  await pending;
  assert.equal(evaluate('closedLateSession && !connected && connectionState === "disconnected" && !controlSessionId'), true);
});

test("late stream startup after disconnect cannot restore connected state", async () => {
  const { elements, context, evaluate } = app();
  let resolveStream;
  context.pendingStream = new Promise(resolve => { resolveStream = resolve; });
  evaluate("startWebRtcStream = () => pendingStream;");
  const pending = elements.connect.handlers.click[0]();
  await new Promise(resolve => setImmediate(resolve));
  evaluate("disconnect()");
  resolveStream();
  await pending;
  assert.equal(evaluate('!connected && connectionState === "disconnected" && !connectBtn.disabled'), true);
});

test("timeout and malformed JSON are surfaced rather than leaving controls busy", async () => {
  const { context, evaluate } = app();
  await assert.rejects(evaluate('withTimeout(new Promise(() => {}), 5, "Timed out")'), /Timed out/);
  context.fetch = async () => ({ ok: true, json: async () => null });
  await assert.rejects(evaluate('realRequestJson("/api/test", {})'), /Invalid server response/);
});

test("WebRTC fallback ends in connected MJPEG with audio disabled", async () => {
  const { elements, evaluate } = app();
  evaluate('startWebRtcStream = async () => { throw new Error("Unsupported WebRTC"); };');
  await elements.connect.handlers.click[0]();
  assert.equal(evaluate('connectionState === "connected" && !usingWebRtc && !connectBtn.disabled && muteAudioBtn.disabled'), true);
});

test("reconnect refreshes the monitor picker after local display topology changes", async () => {
  const { elements, evaluate } = app();
  await elements.connect.handlers.click[0]();
  assert.equal(evaluate('connectionState'), "connected");
  await elements.connect.handlers.click[0]();
  evaluate(`
    let monitorRefreshes = 0;
    requestJson = async (url) => {
      if (url !== "/api/monitors") return fakeRequestJson(url);
      monitorRefreshes++;
      return {ok:true,monitors:[
        {index:1,name:"Physical",width:1920,height:1080,current:true},
        {index:2,name:"Virtual",width:1280,height:720}
      ]};
    };
  `);
  await elements.connect.handlers.click[0]();
  assert.equal(evaluate('monitorRefreshes'), 1);
  assert.equal(evaluate('monitorButtons.length'), 2);
  assert.equal(evaluate('connectionState'), "connected");
  await elements.connect.handlers.click[0]();
  evaluate(`
    requestJson = async (url) => url === "/api/monitors"
      ? {ok:true,monitors:[{index:1,name:"Physical",width:1920,height:1080,current:true}]}
      : fakeRequestJson(url);
  `);
  await elements.connect.handlers.click[0]();
  assert.equal(evaluate('monitorButtons.length'), 1);
  assert.equal(evaluate('connectionState'), "connected");
});

test("monitor switching failure resets connection controls", async () => {
  const { elements, evaluate } = app();
  await elements.connect.handlers.click[0]();
  evaluate('requestJson = async () => { throw new Error("Monitor unavailable"); };');
  await evaluate("changeMonitor(2)");
  assert.equal(evaluate('connectionState === "error" && !connected && !connectBtn.disabled && !streamModeBtn.disabled && showKeyboardBtn.disabled'), true);
});

test("custom monitor picker loads names safely, highlights selection and switches streams", async () => {
  const { evaluate, elements } = app();
  evaluate(`
    token = "test-token";
    requestJson = async () => ({ monitors: [
      { index: 1, name: "<Display one>", width: 1920, height: 1080 },
      { index: 2, name: "Display two", width: 1280, height: 720, current: true }
    ] });
  `);
  await evaluate("loadMonitors()");
  evaluate('setConnectionState("connected", "Connected");');
  assert.equal(elements.monitorToggle.textContent, "Monitor\n2");
  assert.equal(evaluate('monitorButtons[0].textContent'), "MSS#1 - <Display one> (1920 x 1080)");
  assert.equal(evaluate('monitorButtons[1].getAttribute("aria-pressed")'), "true");
  elements.monitorToggle.handlers.click[0]();
  assert.equal(elements.monitorToggle.getAttribute("aria-expanded"), "true");
  evaluate('let chosenMonitor = 0; requestJson = async (url, options) => { chosenMonitor = JSON.parse(options.body).index; return {ok:true}; };');
  await evaluate('monitorButtons[0].handlers.click[0]()');
  assert.equal(evaluate('chosenMonitor'), 1);
  assert.equal(elements.monitor.value, "1");
  assert.equal(elements.monitorToggle.textContent, "Monitor\n1");
  assert.equal(evaluate('selectedMonitor().width'), 1920);
  assert.equal(elements.monitorToggle.getAttribute("aria-expanded"), "false");
  assert.equal(evaluate('monitorButtons[0].getAttribute("aria-pressed")'), "true");
});

test("monitor picker closes on Escape, disconnect and invalid credentials and disables while busy", () => {
  const { evaluate, elements } = app();
  evaluate('monitorsLoaded = true; renderConnectionControls();');
  elements.monitorToggle.handlers.click[0]();
  elements.controlsBar.handlers.keydown[0]({key:"Escape", preventDefault() {}, stopPropagation() {}});
  assert.equal(elements.monitorToggle.getAttribute("aria-expanded"), "false");
  elements.monitorToggle.handlers.click[0]();
  evaluate('setConnectionState("switching", "Changing monitor...");');
  assert.equal(elements.monitorToggle.disabled, true);
  assert.equal(elements.monitorToggle.getAttribute("aria-expanded"), "false");
  evaluate('setConnectionState("connected", "Connected");');
  elements.monitorToggle.handlers.click[0]();
  evaluate('disconnect();');
  assert.equal(elements.monitorToggle.getAttribute("aria-expanded"), "false");
  elements.token.handlers.input[0]();
  assert.equal(elements.monitorToggle.disabled, true);
  assert.equal(elements.monitorToggle.textContent, "Choose\nmonitor");
});
