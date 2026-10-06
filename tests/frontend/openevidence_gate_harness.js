// Minimal DOM-free harness for exercising src/static/app.js's OpenEvidence
// sidecar gating (renderOpenEvidenceCard / fetchGeneOpenEvidence /
// loadDevStatus) without a browser or a full jsdom dependency.
//
// app.js has no module boundary — its bottom six lines
// (initSidebarResize(); bindEvents(); renderGrid(); updateExportState();
// loadDevStatus(); loadSharedRun();) run immediately on load and drive real
// page wiring this harness has no DOM for. We load everything ABOVE that
// block (all function declarations plus the top-level `state`/`elements`
// consts) inside a vm context with a tiny fake `document`, then drive
// renderOpenEvidenceCard/fetchGeneOpenEvidence/loadDevStatus ourselves —
// this runs the actual production code for the flag gate, not a
// reimplementation of it.
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const APP_JS_PATH = path.join(__dirname, "..", "..", "src", "static", "app.js");
const TRAILING_INVOCATIONS =
  "initSidebarResize();\nbindEvents();\nrenderGrid();\nupdateExportState();\nloadDevStatus();\nloadSharedRun();\n";

function loadedSource() {
  const source = fs.readFileSync(APP_JS_PATH, "utf8");
  const idx = source.lastIndexOf(TRAILING_INVOCATIONS);
  assert.notStrictEqual(
    idx,
    -1,
    "app.js's trailing top-level invocation block has moved/changed — update TRAILING_INVOCATIONS in this harness to match."
  );
  // Keep only the function/const declarations above the auto-run block, so
  // loading the script wires up `state`/`elements`/every function without
  // also firing bindEvents()/renderGrid()/loadDevStatus()/loadSharedRun()
  // against our fake DOM.
  const declarations = source.slice(0, idx);
  // Top-level `const`/`let` bindings in a vm script live in the script's
  // lexical scope, not as properties of the sandbox/global object (unlike
  // `function` declarations, which do attach) — so `state` isn't reachable
  // from outside as `sandbox.state` without this explicit re-export.
  return `${declarations}\nglobalThis.state = state;\nglobalThis.elements = elements;\nglobalThis.OPENEVIDENCE_POLL = OPENEVIDENCE_POLL;\nglobalThis.OPENEVIDENCE_MESSAGES = OPENEVIDENCE_MESSAGES;\n`;
}

// Depth-first search through the FakeElement tree (as built by
// appendChild/replaceChildren) for an element with the given id, mirroring
// how a test would use document.getElementById against a real DOM.
function findById(root, id) {
  if (!root) return null;
  if (root.id === id) return root;
  for (const child of root.children || []) {
    const found = findById(child, id);
    if (found) return found;
  }
  return null;
}

class FakeClassList {
  constructor() {
    this._set = new Set();
  }
  add(...names) {
    names.forEach((n) => this._set.add(n));
  }
  remove(...names) {
    names.forEach((n) => this._set.delete(n));
  }
  toggle(name, force) {
    const shouldAdd = force === undefined ? !this._set.has(name) : Boolean(force);
    if (shouldAdd) this._set.add(name);
    else this._set.delete(name);
    return shouldAdd;
  }
  contains(name) {
    return this._set.has(name);
  }
}

class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag;
    this.className = "";
    this.id = "";
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.disabled = false;
    this.checked = false;
    this.hidden = false;
    this.dataset = {};
    this.children = [];
    this.classList = new FakeClassList();
    this._attrs = new Map();
    this._listeners = new Map();
    this.style = {
      _props: new Map(),
      getPropertyValue(name) {
        return this._props.get(name) || "";
      },
      setProperty(name, value) {
        this._props.set(name, value);
      },
      removeProperty(name) {
        this._props.delete(name);
      },
    };
  }
  setAttribute(name, value) {
    this._attrs.set(name, String(value));
  }
  getAttribute(name) {
    return this._attrs.has(name) ? this._attrs.get(name) : null;
  }
  removeAttribute(name) {
    this._attrs.delete(name);
  }
  addEventListener(type, handler) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(handler);
  }
  removeEventListener() {}
  dispatchEvent() {
    return true;
  }
  // Mirrors Node.isConnected closely enough for the sidecar's polling to
  // notice a card being removed or its results list being replaced: true
  // only when an unbroken _parent chain reaches a document-owned root (see
  // buildSandbox) and nothing on the way was remove()d.
  get isConnected() {
    for (let node = this; node; node = node._parent) {
      if (node._removed) return false;
      if (node._isRoot) return true;
    }
    return false;
  }
  appendChild(child) {
    this.children.push(child);
    child._parent = this;
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    if (child._parent === this) child._parent = null;
    return child;
  }
  replaceChildren(...nodes) {
    this.children.forEach((child) => {
      if (child._parent === this) child._parent = null;
    });
    this.children = nodes;
    nodes.forEach((child) => {
      child._parent = this;
    });
  }
  remove() {
    this._removed = true;
  }
  closest() {
    return null;
  }
  contains() {
    return false;
  }
  querySelector(selector) {
    return new FakeElement(selector);
  }
  querySelectorAll() {
    return [];
  }
  focus() {}
  click() {}
  select() {}
  scrollIntoView() {}
  getBoundingClientRect() {
    return { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 };
  }
}

function buildSandbox({ fetchImpl }) {
  // Every document.createElement call app.js makes gets recorded here so
  // tests can assert nothing was created at all (not just "nothing
  // fetched") for a given code path — see createdElementTags below.
  const createdElements = [];
  const rootElement = (tag) => {
    const el = new FakeElement(tag);
    el._isRoot = true;
    return el;
  };
  const fakeDocument = {
    querySelector: (selector) => rootElement(selector),
    querySelectorAll: () => [],
    createElement: (tag) => {
      const el = new FakeElement(tag);
      createdElements.push(el);
      return el;
    },
    getElementById: (id) => rootElement(`#${id}`),
    addEventListener: () => {},
    removeEventListener: () => {},
    body: rootElement("body"),
  };
  const fakeWindow = {
    addEventListener: () => {},
    removeEventListener: () => {},
    location: { search: "", href: "http://localhost/" },
  };

  const sandbox = {
    document: fakeDocument,
    window: fakeWindow,
    navigator: { clipboard: { writeText: async () => {} } },
    fetch: fetchImpl,
    URLSearchParams,
    console,
    setTimeout,
    clearTimeout,
    localStorage: {
      _store: new Map(),
      getItem(key) {
        return this._store.has(key) ? this._store.get(key) : null;
      },
      setItem(key, value) {
        this._store.set(key, String(value));
      },
      removeItem(key) {
        this._store.delete(key);
      },
    },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  return { sandbox, createdElements };
}

// Loads app.js's declarations into a fresh sandbox and returns it, with the
// module's own functions/state directly reachable as sandbox properties
// (e.g. sandbox.state, sandbox.renderOpenEvidenceCard), plus
// `createdElementTags` — every tag name passed to document.createElement
// during this sandbox's lifetime, for asserting nothing was created at all.
function loadApp({ fetchImpl }) {
  const { sandbox, createdElements } = buildSandbox({ fetchImpl });
  const script = new vm.Script(loadedSource(), { filename: "app.js" });
  script.runInContext(sandbox);
  Object.defineProperty(sandbox, "createdElementTags", {
    get: () => createdElements.map((el) => el.tagName),
  });
  return sandbox;
}

module.exports = { loadApp, FakeElement, findById };
