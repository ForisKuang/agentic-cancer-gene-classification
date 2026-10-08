// API keys page (/api-keys): create, list, and revoke personal ACGC API keys
// through the session-only /v1/api-keys endpoints.
//
// The plaintext key from POST /v1/api-keys is written straight into the
// one-time dialog's DOM node and nowhere else — not to state, storage, the
// URL, or the console — and that node is emptied when the dialog closes.
//
// Everything is built by createApiKeysPage(deps) so tests/frontend can drive
// the real code with a fake document/fetch; in the browser the bottom of this
// file wires it to the real ones.
(function (root) {
  "use strict";

  const DEFAULT_EXPIRES_DAYS = 90;
  const EXPIRY_PRESETS = [7, 30, 90, 180, 365];
  const NO_EXPIRY = "never";
  const PAGE_PATH = "/api-keys";

  class ApiError extends Error {
    constructor(status) {
      super(`HTTP ${status}`);
      this.status = status;
    }
  }

  function expiryOptions(maxDays) {
    const max = Number.isFinite(maxDays) && maxDays >= 1 ? Math.floor(maxDays) : 365;
    const days = EXPIRY_PRESETS.filter((d) => d <= max);
    if (!days.includes(max)) days.push(max);
    const defaultDays = days.includes(DEFAULT_EXPIRES_DAYS) ? DEFAULT_EXPIRES_DAYS : days[days.length - 1];
    const options = days.map((d) => ({
      value: String(d),
      label: d === 365 ? "1 year" : `${d} days`,
      selected: d === defaultDays,
    }));
    options.push({ value: NO_EXPIRY, label: "No expiration (not recommended)", selected: false });
    return options;
  }

  function keyStatus(key, now) {
    if (key.revoked_at) return "revoked";
    if (key.expires_at && new Date(key.expires_at).getTime() <= now.getTime()) return "expired";
    return key.active === false ? "inactive" : "active";
  }

  function formatDate(value) {
    if (!value) return "";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return "";
    return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
  }

  // Fixed, user-facing copy per status class; server `detail` text is never shown.
  function friendlyError(err, action) {
    const status = err && err.status;
    if (status === 401) return "Your session has expired. Redirecting you to sign in…";
    if (status === 403) {
      return `You don't have permission to ${action}. API keys can only be managed by an allowed user in a signed-in browser session.`;
    }
    if (status === 404) return "That API key no longer exists.";
    if (status === 422) return "Check the name and expiration and try again.";
    if (status === 400) return "API keys aren't available on this deployment.";
    if (status === 429) return "Too many requests. Wait a minute and try again.";
    if (status >= 500) return `The server couldn't ${action} right now. Please try again later.`;
    return `Couldn't ${action}. Check your connection and try again.`;
  }

  function usageSnippet(baseUrl) {
    const base = (baseUrl || "").replace(/\/+$/, "");
    return [
      "export ACGC_API_KEY=acgc_...   # paste your key; keep it out of git",
      "",
      `curl -X POST "${base}/v1/genes/query" \\`,
      '  -H "Authorization: Bearer $ACGC_API_KEY" \\',
      '  -H "Content-Type: application/json" \\',
      `  -d '{"genes": ["ALK", {"gene": "TP53", "tumor_type": "LUAD"}]}'`,
    ].join("\n");
  }

  function createApiKeysPage(deps) {
    const doc = deps.document;
    const fetchFn = deps.fetch;
    const win = deps.window;
    const now = deps.now || (() => new Date());
    const config = deps.config || {};
    const $ = (id) => doc.getElementById(id);

    const el = {
      signedIn: $("api-keys-signed-in"),
      pageError: $("api-keys-error"),
      form: $("create-key-form"),
      name: $("key-name"),
      expiry: $("key-expiry"),
      createBtn: $("create-key-btn"),
      createError: $("create-key-error"),
      showAllWrap: $("show-all-wrap"),
      showAll: $("show-all-keys"),
      listHeading: $("list-keys-heading"),
      empty: $("keys-empty"),
      loading: $("keys-loading"),
      table: $("keys-table"),
      ownerHeader: $("owner-col-header"),
      tbody: $("keys-tbody"),
      snippet: $("usage-snippet"),
      newKeyModal: $("new-key-modal"),
      newKeyValue: $("new-key-value"),
      copyBtn: $("copy-key-btn"),
      copyStatus: $("copy-key-status"),
      newKeyDone: $("new-key-done-btn"),
      revokeModal: $("revoke-modal"),
      revokeMessage: $("revoke-message"),
      revokeError: $("revoke-error"),
      revokeCancel: $("revoke-cancel-btn"),
      revokeConfirm: $("revoke-confirm-btn"),
    };

    const state = {
      isAdmin: false,
      showAll: false,
      keys: [],
      pendingRevoke: null,
      busy: false,
    };

    function show(node, visible) {
      if (node) node.classList.toggle("hidden", !visible);
    }

    function setMessage(node, message) {
      if (!node) return;
      node.textContent = message || "";
      show(node, Boolean(message));
    }

    function redirectToLogin() {
      win.location.href = `/login?redirect_to=${encodeURIComponent(PAGE_PATH)}`;
    }

    async function request(method, url, body) {
      const init = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
      if (body !== undefined) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
      let response;
      try {
        response = await fetchFn(url, init);
      } catch (_networkError) {
        throw new ApiError(0);
      }
      if (response.status === 401) {
        redirectToLogin();
        throw new ApiError(401);
      }
      if (!response.ok) throw new ApiError(response.status);
      return response.json();
    }

    function renderExpiryOptions() {
      if (!el.expiry) return;
      el.expiry.replaceChildren();
      for (const opt of expiryOptions(config.maxExpiresDays)) {
        const option = doc.createElement("option");
        option.value = opt.value;
        option.textContent = opt.label;
        if (opt.selected) {
          option.selected = true;
          el.expiry.value = opt.value;
        }
        el.expiry.appendChild(option);
      }
    }

    function cell(text, className) {
      const td = doc.createElement("td");
      if (className) td.className = className;
      td.textContent = text;
      return td;
    }

    function renderKeys() {
      const showOwner = state.showAll;
      show(el.ownerHeader, showOwner);
      if (el.listHeading) el.listHeading.textContent = showOwner ? "All API keys" : "Your keys";
      el.tbody.replaceChildren();
      show(el.loading, false);
      show(el.empty, state.keys.length === 0);
      show(el.table, state.keys.length > 0);
      const current = now();
      for (const key of state.keys) {
        // Only non-secret fields are rendered; list responses never contain the key.
        const status = keyStatus(key, current);
        const tr = doc.createElement("tr");
        tr.dataset.keyId = key.id;
        tr.appendChild(cell(key.name));
        if (showOwner) tr.appendChild(cell(key.owner_email || ""));
        tr.appendChild(cell(formatDate(key.created_at)));
        tr.appendChild(cell(key.expires_at ? formatDate(key.expires_at) : "Never"));
        tr.appendChild(cell(key.last_used_at ? formatDate(key.last_used_at) : "Never"));
        const statusCell = doc.createElement("td");
        const badge = doc.createElement("span");
        badge.className = `api-key-status status-${status}`;
        badge.textContent = status;
        statusCell.appendChild(badge);
        tr.appendChild(statusCell);
        tr.appendChild(cell(key.id, "api-key-id"));
        const actions = doc.createElement("td");
        if (status === "active" || status === "inactive") {
          const btn = doc.createElement("button");
          btn.type = "button";
          btn.className = "danger-link";
          btn.textContent = "Revoke";
          btn.addEventListener("click", () => openRevoke(key));
          actions.appendChild(btn);
        }
        tr.appendChild(actions);
        el.tbody.appendChild(tr);
      }
    }

    async function loadKeys(notice) {
      setMessage(el.pageError, notice || "");
      const url = state.showAll ? "/v1/api-keys?all=true" : "/v1/api-keys";
      try {
        const keys = await request("GET", url);
        state.keys = Array.isArray(keys) ? keys : [];
        renderKeys();
      } catch (err) {
        show(el.loading, false);
        if (err.status !== 401) setMessage(el.pageError, friendlyError(err, "load your API keys"));
      }
    }

    async function createKey() {
      if (state.busy) return;
      setMessage(el.createError, "");
      const name = (el.name.value || "").trim();
      if (!name) {
        setMessage(el.createError, "Enter a name for the key.");
        return;
      }
      const expiry = el.expiry.value;
      const body = { name, expires_in_days: expiry === NO_EXPIRY ? null : Number(expiry) };
      state.busy = true;
      el.createBtn.disabled = true;
      try {
        const created = await request("POST", "/v1/api-keys", body);
        el.name.value = "";
        showNewKey(created.key);
        await loadKeys();
      } catch (err) {
        if (err.status !== 401) setMessage(el.createError, friendlyError(err, "create the key"));
      } finally {
        state.busy = false;
        el.createBtn.disabled = false;
      }
    }

    function showNewKey(plaintext) {
      el.newKeyValue.textContent = plaintext;
      el.copyStatus.textContent = "";
      show(el.newKeyModal, true);
      if (el.copyBtn && el.copyBtn.focus) el.copyBtn.focus();
    }

    function closeNewKeyDialog() {
      el.newKeyValue.textContent = "";
      el.copyStatus.textContent = "";
      show(el.newKeyModal, false);
    }

    async function copyKey() {
      const text = el.newKeyValue.textContent;
      if (!text) return;
      try {
        await win.navigator.clipboard.writeText(text);
        el.copyStatus.textContent = "Copied to clipboard.";
      } catch (_err) {
        el.copyStatus.textContent = "Couldn't copy automatically — select the key and copy it manually.";
      }
    }

    function openRevoke(key) {
      state.pendingRevoke = { id: key.id, name: key.name };
      el.revokeMessage.textContent = `"${key.name}" will stop working immediately for any script using it. This can't be undone.`;
      setMessage(el.revokeError, "");
      el.revokeConfirm.disabled = false;
      show(el.revokeModal, true);
    }

    function closeRevoke() {
      state.pendingRevoke = null;
      show(el.revokeModal, false);
    }

    async function confirmRevoke() {
      const pending = state.pendingRevoke;
      if (!pending) return;
      el.revokeConfirm.disabled = true;
      try {
        await request("DELETE", `/v1/api-keys/${encodeURIComponent(pending.id)}`);
        closeRevoke();
        await loadKeys();
      } catch (err) {
        if (err.status === 401) return;
        if (err.status === 404) {
          closeRevoke();
          await loadKeys(friendlyError(err, "revoke the key"));
          return;
        }
        setMessage(el.revokeError, friendlyError(err, "revoke the key"));
        el.revokeConfirm.disabled = false;
      }
    }

    function bindEvents() {
      el.form.addEventListener("submit", (event) => {
        event.preventDefault();
        createKey();
      });
      el.copyBtn.addEventListener("click", copyKey);
      el.newKeyDone.addEventListener("click", closeNewKeyDialog);
      el.revokeCancel.addEventListener("click", closeRevoke);
      el.revokeConfirm.addEventListener("click", confirmRevoke);
      if (el.showAll) {
        el.showAll.addEventListener("change", () => {
          state.showAll = state.isAdmin && Boolean(el.showAll.checked);
          loadKeys();
        });
      }
      doc.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        if (!el.newKeyModal.classList.contains("hidden")) closeNewKeyDialog();
        if (!el.revokeModal.classList.contains("hidden")) closeRevoke();
      });
      // Drop the plaintext if the page is navigated away from / put in bfcache.
      win.addEventListener("pagehide", closeNewKeyDialog);
    }

    async function init() {
      renderExpiryOptions();
      if (el.snippet) el.snippet.textContent = usageSnippet(config.publicBaseUrl);
      bindEvents();
      let me;
      try {
        me = await request("GET", "/auth/me");
      } catch (err) {
        if (err.status !== 401) setMessage(el.pageError, friendlyError(err, "check your sign-in"));
        show(el.loading, false);
        return;
      }
      if (!me.auth_enabled) {
        setMessage(el.pageError, "Sign-in is disabled on this deployment, so API keys aren't needed or available.");
        el.createBtn.disabled = true;
        show(el.loading, false);
        return;
      }
      if (!me.authenticated || !me.user) {
        redirectToLogin();
        return;
      }
      if (el.signedIn) {
        el.signedIn.textContent = `Signed in as ${me.user.email}`;
        show(el.signedIn, true);
      }
      state.isAdmin = me.user.role === "admin";
      show(el.showAllWrap, state.isAdmin);
      state.showAll = state.isAdmin && Boolean(el.showAll && el.showAll.checked);
      await loadKeys();
    }

    return {
      state,
      init,
      loadKeys,
      createKey,
      copyKey,
      closeNewKeyDialog,
      openRevoke,
      closeRevoke,
      confirmRevoke,
    };
  }

  const api = { createApiKeysPage, expiryOptions, keyStatus, formatDate, friendlyError, usageSnippet };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else if (root.document && root.document.getElementById("api-keys-page")) {
    const body = root.document.body;
    const configuredBase = body.dataset.publicBaseUrl || "";
    const page = createApiKeysPage({
      document: root.document,
      fetch: root.fetch.bind(root),
      window: root,
      config: {
        publicBaseUrl: configuredBase.startsWith("http") ? configuredBase : root.location.origin,
        maxExpiresDays: Number(body.dataset.maxExpiresDays),
      },
    });
    page.init();
  }
})(typeof window !== "undefined" ? window : globalThis);
