// Drives src/static/api-keys.js (the /api-keys page) against a tiny fake DOM
// and a scripted fetch: create shows the key once and clears it, the list
// renders without secrets, revoke confirms then refreshes, and HTTP errors map
// to fixed friendly messages (401 -> re-login). Run via
// tests/test_frontend_api_keys_page.py, or directly with `node`.
"use strict";

const assert = require("assert");
const path = require("path");

const page = require(path.join(__dirname, "..", "..", "src", "static", "api-keys.js"));

const PAGE_IDS = [
  "api-keys-signed-in", "api-keys-error", "create-key-form", "key-name", "key-expiry",
  "create-key-btn", "create-key-error", "show-all-wrap", "show-all-keys", "list-keys-heading",
  "keys-empty", "keys-loading", "keys-table", "owner-col-header", "keys-tbody", "usage-snippet",
  "new-key-modal", "new-key-value", "copy-key-btn", "copy-key-status", "new-key-done-btn",
  "revoke-modal", "revoke-message", "revoke-error", "revoke-cancel-btn", "revoke-confirm-btn",
];
const INITIALLY_HIDDEN = [
  "api-keys-signed-in", "api-keys-error", "create-key-error", "show-all-wrap", "keys-empty",
  "keys-table", "owner-col-header", "new-key-modal", "revoke-modal", "revoke-error",
];

class FakeElement {
  constructor(tag, id) {
    this.tagName = tag;
    this.id = id || "";
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.className = "";
    this.dataset = {};
    this.children = [];
    this._hidden = new Set();
    this._listeners = {};
    const self = this;
    this.classList = {
      add: (c) => self._hidden.add(c),
      remove: (c) => self._hidden.delete(c),
      contains: (c) => self._hidden.has(c),
      toggle: (c, force) => (force ? self._hidden.add(c) : self._hidden.delete(c), Boolean(force)),
    };
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }
  fire(type, event = {}) {
    for (const fn of this._listeners[type] || []) fn({ preventDefault() {}, ...event });
  }
  get isHidden() {
    return this.classList.contains("hidden");
  }
  get allText() {
    return [this.textContent, ...this.children.map((c) => c.allText)].join(" ");
  }
  find(predicate) {
    if (predicate(this)) return this;
    for (const c of this.children) {
      const hit = c.find(predicate);
      if (hit) return hit;
    }
    return null;
  }
}

function makeEnv({ routes, role = "curator", authEnabled = true, authenticated = true }) {
  const byId = {};
  for (const id of PAGE_IDS) byId[id] = new FakeElement("div", id);
  for (const id of INITIALLY_HIDDEN) byId[id].classList.add("hidden");
  byId["show-all-keys"].checked = true;
  const document = new FakeElement("document");
  document.getElementById = (id) => byId[id] || null;
  document.createElement = (tag) => new FakeElement(tag);

  const calls = [];
  const clipboard = [];
  const window = new FakeElement("window");
  window.location = { href: "/api-keys" };
  window.navigator = { clipboard: { writeText: async (t) => clipboard.push(t) } };

  const me = {
    auth_enabled: authEnabled,
    authenticated,
    user: authenticated ? { email: "me@mskcc.org", role } : null,
  };
  const queues = { "GET /auth/me": [{ status: 200, body: me }], ...routes };

  async function fetch(url, init) {
    const key = `${init.method} ${url}`;
    calls.push({ key, init });
    const queue = queues[key];
    assert.ok(queue && queue.length, `unexpected request ${key}`);
    const next = queue.length > 1 ? queue.shift() : queue[0];
    if (next === "network") throw new TypeError("Failed to fetch");
    return {
      status: next.status,
      ok: next.status >= 200 && next.status < 300,
      json: async () => next.body,
    };
  }

  const app = page.createApiKeysPage({
    document,
    window,
    fetch,
    now: () => new Date("2026-10-07T12:00:00Z"),
    config: { publicBaseUrl: "https://acgc.example.org/", maxExpiresDays: 365 },
  });
  return { app, byId, calls, clipboard, window, document };
}

const KEY_A = {
  id: "11111111-aaaa", name: "nightly", owner_email: "me@mskcc.org",
  created_at: "2026-09-01T10:00:00Z", last_used_at: "2026-10-06T08:30:00Z",
  revoked_at: null, expires_at: "2026-11-30T10:00:00Z", active: true,
};
const KEY_REVOKED = { ...KEY_A, id: "22222222-bbbb", name: "old", revoked_at: "2026-09-15T00:00:00Z", active: false };
const KEY_EXPIRED = { ...KEY_A, id: "33333333-cccc", name: "stale", expires_at: "2026-10-01T00:00:00Z", active: false, last_used_at: null };
const SECRET = "acgc_" + "S".repeat(43);

const rows = (byId) => byId["keys-tbody"].children;
const rowText = (row) => row.children.map((c) => c.allText.trim());

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("pure helpers: expiry options, status, snippet", () => {
  const opts = page.expiryOptions(365);
  assert.deepStrictEqual(opts.map((o) => o.value), ["7", "30", "90", "180", "365", "never"]);
  assert.strictEqual(opts.find((o) => o.selected).value, "90");
  // Options never exceed the backend's API_KEY_MAX_EXPIRES_IN_DAYS.
  assert.deepStrictEqual(page.expiryOptions(60).map((o) => o.value), ["7", "30", "60", "never"]);
  assert.strictEqual(page.expiryOptions(60).find((o) => o.selected).value, "60");
  const now = new Date("2026-10-07T12:00:00Z");
  assert.strictEqual(page.keyStatus(KEY_A, now), "active");
  assert.strictEqual(page.keyStatus(KEY_REVOKED, now), "revoked");
  assert.strictEqual(page.keyStatus(KEY_EXPIRED, now), "expired");
  assert.strictEqual(page.keyStatus({ ...KEY_A, active: false }, now), "inactive");
  const snippet = page.usageSnippet("https://acgc.example.org/");
  assert.ok(snippet.includes('curl -X POST "https://acgc.example.org/v1/genes/query"'));
  assert.ok(snippet.includes("Authorization: Bearer $ACGC_API_KEY"));
});

test("list renders the user's keys with status and no secrets", async () => {
  const env = makeEnv({ routes: { "GET /v1/api-keys": [{ status: 200, body: [KEY_A, KEY_REVOKED, KEY_EXPIRED] }] } });
  await env.app.init();
  const { byId } = env;
  assert.ok(byId["keys-loading"].isHidden);
  assert.ok(!byId["keys-table"].isHidden);
  assert.ok(byId["owner-col-header"].isHidden, "non-admins see no owner column");
  assert.ok(byId["show-all-wrap"].isHidden);
  assert.strictEqual(rows(byId).length, 3);
  const [a, revoked, expired] = rows(byId).map(rowText);
  assert.deepStrictEqual(a.slice(0, 6), [
    "nightly", "2026-09-01 10:00 UTC", "2026-11-30 10:00 UTC", "2026-10-06 08:30 UTC", "active", KEY_A.id,
  ]);
  assert.strictEqual(a[6], "Revoke");
  assert.strictEqual(revoked[4], "revoked");
  assert.strictEqual(revoked[6], "", "revoked keys have no Revoke button");
  assert.strictEqual(expired[4], "expired");
  assert.strictEqual(expired[3], "Never");
  assert.ok(byId["usage-snippet"].textContent.includes("https://acgc.example.org/v1/genes/query"));
  assert.ok(!byId["keys-tbody"].allText.includes("acgc_"));
});

test("empty list shows the empty state", async () => {
  const env = makeEnv({ routes: { "GET /v1/api-keys": [{ status: 200, body: [] }] } });
  await env.app.init();
  assert.ok(!env.byId["keys-empty"].isHidden);
  assert.ok(env.byId["keys-table"].isHidden);
});

test("admins list all keys with the owner column", async () => {
  const other = { ...KEY_A, id: "44444444-dddd", owner_email: "other@mskcc.org" };
  const env = makeEnv({
    role: "admin",
    routes: {
      "GET /v1/api-keys?all=true": [{ status: 200, body: [KEY_A, other] }],
      "GET /v1/api-keys": [{ status: 200, body: [KEY_A] }],
    },
  });
  await env.app.init();
  const { byId } = env;
  assert.ok(!byId["show-all-wrap"].isHidden);
  assert.ok(!byId["owner-col-header"].isHidden);
  assert.strictEqual(rowText(rows(byId)[1])[1], "other@mskcc.org");
  // Unchecking "all users" falls back to the caller's own keys, without the owner column.
  byId["show-all-keys"].checked = false;
  byId["show-all-keys"].fire("change");
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(env.calls.at(-1).key, "GET /v1/api-keys");
  assert.ok(byId["owner-col-header"].isHidden);
  assert.strictEqual(rows(byId).length, 1);
});

test("create shows the key once, copies it, and clears it on dismiss", async () => {
  const created = { ...KEY_A, id: "55555555-eeee", name: "ci", key: SECRET };
  const env = makeEnv({
    routes: {
      "GET /v1/api-keys": [{ status: 200, body: [] }, { status: 200, body: [{ ...created, key: undefined }] }],
      "POST /v1/api-keys": [{ status: 201, body: created }],
    },
  });
  await env.app.init();
  const { byId } = env;
  assert.strictEqual(byId["key-expiry"].value, "90");
  byId["key-name"].value = "  ci  ";
  await env.app.createKey();

  const post = env.calls.find((c) => c.key === "POST /v1/api-keys");
  assert.deepStrictEqual(JSON.parse(post.init.body), { name: "ci", expires_in_days: 90 });
  assert.strictEqual(post.init.credentials, "same-origin");
  assert.ok(!byId["new-key-modal"].isHidden);
  assert.strictEqual(byId["new-key-value"].textContent, SECRET);
  assert.strictEqual(byId["key-name"].value, "");
  assert.strictEqual(rows(byId).length, 1, "list refreshed after create");
  assert.ok(!byId["keys-tbody"].allText.includes(SECRET));
  // The secret isn't kept in page state, the URL, or anywhere but the dialog node.
  assert.ok(!JSON.stringify(env.app.state).includes(SECRET));
  assert.ok(!env.window.location.href.includes(SECRET));

  await env.app.copyKey();
  assert.deepStrictEqual(env.clipboard, [SECRET]);
  assert.strictEqual(byId["copy-key-status"].textContent, "Copied to clipboard.");

  byId["new-key-done-btn"].fire("click");
  assert.ok(byId["new-key-modal"].isHidden);
  assert.strictEqual(byId["new-key-value"].textContent, "");
  assert.strictEqual(byId["copy-key-status"].textContent, "");
});

test("create sends null expiry for 'never' and requires a name", async () => {
  const env = makeEnv({
    routes: {
      "GET /v1/api-keys": [{ status: 200, body: [] }],
      "POST /v1/api-keys": [{ status: 201, body: { ...KEY_A, key: SECRET } }],
    },
  });
  await env.app.init();
  env.byId["key-name"].value = "   ";
  await env.app.createKey();
  assert.strictEqual(env.byId["create-key-error"].textContent, "Enter a name for the key.");
  assert.ok(!env.calls.some((c) => c.key === "POST /v1/api-keys"));

  env.byId["key-name"].value = "forever";
  env.byId["key-expiry"].value = "never";
  await env.app.createKey();
  const post = env.calls.find((c) => c.key === "POST /v1/api-keys");
  assert.deepStrictEqual(JSON.parse(post.init.body), { name: "forever", expires_in_days: null });
  assert.ok(env.byId["create-key-error"].isHidden);
});

test("pagehide clears a displayed key", async () => {
  const env = makeEnv({
    routes: {
      "GET /v1/api-keys": [{ status: 200, body: [] }],
      "POST /v1/api-keys": [{ status: 201, body: { ...KEY_A, key: SECRET } }],
    },
  });
  await env.app.init();
  env.byId["key-name"].value = "x";
  await env.app.createKey();
  assert.strictEqual(env.byId["new-key-value"].textContent, SECRET);
  env.window.fire("pagehide");
  assert.strictEqual(env.byId["new-key-value"].textContent, "");
});

test("revoke asks for confirmation, then deletes and refreshes", async () => {
  const env = makeEnv({
    routes: {
      "GET /v1/api-keys": [{ status: 200, body: [KEY_A] }, { status: 200, body: [{ ...KEY_A, revoked_at: "2026-10-07T12:00:00Z", active: false }] }],
      [`DELETE /v1/api-keys/${KEY_A.id}`]: [{ status: 200, body: { ...KEY_A, revoked_at: "2026-10-07T12:00:00Z" } }],
    },
  });
  await env.app.init();
  const { byId } = env;
  const revokeBtn = rows(byId)[0].find((n) => n.textContent === "Revoke");
  revokeBtn.fire("click");
  assert.ok(!byId["revoke-modal"].isHidden);
  assert.ok(byId["revoke-message"].textContent.includes('"nightly"'));
  assert.ok(!env.calls.some((c) => c.key.startsWith("DELETE")), "nothing revoked before confirming");

  // Cancel closes without a request.
  byId["revoke-cancel-btn"].fire("click");
  assert.ok(byId["revoke-modal"].isHidden);
  assert.ok(!env.calls.some((c) => c.key.startsWith("DELETE")));

  env.app.openRevoke(KEY_A);
  await env.app.confirmRevoke();
  assert.ok(env.calls.some((c) => c.key === `DELETE /v1/api-keys/${KEY_A.id}`));
  assert.ok(byId["revoke-modal"].isHidden);
  assert.strictEqual(env.calls.at(-1).key, "GET /v1/api-keys");
  assert.strictEqual(rowText(rows(byId)[0])[4], "revoked");
});

test("revoke errors stay in the dialog; 404 refreshes the list", async () => {
  const env = makeEnv({
    routes: {
      "GET /v1/api-keys": [{ status: 200, body: [KEY_A] }, { status: 200, body: [] }],
      [`DELETE /v1/api-keys/${KEY_A.id}`]: [
        { status: 500, body: { detail: "Traceback: db exploded at 10.0.0.5" } },
        { status: 404, body: { detail: "API key not found." } },
      ],
    },
  });
  await env.app.init();
  env.app.openRevoke(KEY_A);
  await env.app.confirmRevoke();
  assert.ok(!env.byId["revoke-modal"].isHidden);
  assert.strictEqual(env.byId["revoke-error"].textContent, "The server couldn't revoke the key right now. Please try again later.");
  assert.ok(!env.byId["revoke-confirm-btn"].disabled);

  await env.app.confirmRevoke();
  assert.ok(env.byId["revoke-modal"].isHidden);
  assert.strictEqual(env.byId["api-keys-error"].textContent, "That API key no longer exists.");
  assert.ok(!env.byId["keys-empty"].isHidden);
});

test("HTTP errors map to friendly messages without server details", async () => {
  const cases = [
    [403, "You don't have permission to create the key."],
    [429, "Too many requests. Wait a minute and try again."],
    [500, "The server couldn't create the key right now. Please try again later."],
    [503, "The server couldn't create the key right now. Please try again later."],
    [422, "Check the name and expiration and try again."],
  ];
  for (const [status, prefix] of cases) {
    const env = makeEnv({
      routes: {
        "GET /v1/api-keys": [{ status: 200, body: [] }],
        "POST /v1/api-keys": [{ status, body: { detail: "internal: secret stack trace" } }],
      },
    });
    await env.app.init();
    env.byId["key-name"].value = "k";
    await env.app.createKey();
    const msg = env.byId["create-key-error"].textContent;
    assert.ok(msg.startsWith(prefix), `${status}: ${msg}`);
    assert.ok(!msg.includes("internal"), `${status} leaked server detail`);
    assert.ok(env.byId["new-key-modal"].isHidden);
    assert.ok(!env.byId["create-key-btn"].disabled);
  }

  const listFail = makeEnv({ routes: { "GET /v1/api-keys": [{ status: 502, body: {} }] } });
  await listFail.app.init();
  assert.strictEqual(listFail.byId["api-keys-error"].textContent, "The server couldn't load your API keys right now. Please try again later.");
  assert.ok(listFail.byId["keys-loading"].isHidden);

  const offline = makeEnv({ routes: { "GET /v1/api-keys": ["network"] } });
  await offline.app.init();
  assert.strictEqual(offline.byId["api-keys-error"].textContent, "Couldn't load your API keys. Check your connection and try again.");
});

test("401 sends the user back through login to /api-keys", async () => {
  const env = makeEnv({ routes: { "GET /v1/api-keys": [{ status: 401, body: {} }] } });
  await env.app.init();
  assert.strictEqual(env.window.location.href, "/login?redirect_to=%2Fapi-keys");
  assert.ok(env.byId["api-keys-error"].isHidden);

  const signedOut = makeEnv({ authenticated: false, routes: {} });
  await signedOut.app.init();
  assert.strictEqual(signedOut.window.location.href, "/login?redirect_to=%2Fapi-keys");
  assert.ok(!signedOut.calls.some((c) => c.key.includes("/v1/api-keys")));
});

test("auth disabled: explains and disables creation", async () => {
  const env = makeEnv({ authEnabled: false, authenticated: false, routes: {} });
  await env.app.init();
  assert.ok(env.byId["create-key-btn"].disabled);
  assert.ok(env.byId["api-keys-error"].textContent.includes("Sign-in is disabled"));
  assert.ok(!env.calls.some((c) => c.key.includes("/v1/api-keys")));
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      failed += 1;
      console.log(`not ok - ${name}\n${err.stack}`);
    }
  }
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
