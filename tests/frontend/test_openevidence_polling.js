// Proves the OpenEvidence sidecar card's "pending + poll" handling in
// src/static/app.js (loadOpenEvidenceCard / fetchGeneOpenEvidence): a cache
// miss answers status "pending" (HTTP 503 + retry_after_seconds, see
// GET /v1/genes/{gene}/openevidence in main.py) and the card must keep
// polling until "ready" (render) or "failed"/timeout (explicit note), stop polling
// once the card leaves the page or the flag turns off, and never let pending
// cards hog the client-side fetch-concurrency queue.
// Run via tests/test_frontend_openevidence_gate.py (invokes this with node).
"use strict";

const assert = require("assert");
const { loadApp, findById } = require("./openevidence_gate_harness");

const READY = { available: true, distilled: { consensus_role: "Guideline-backed." }, error: null, status: "ready" };
const PENDING = { available: false, distilled: null, error: null, status: "pending", retry_after_seconds: 0.001 };
const FAILED = { available: false, distilled: null, error: "upstream timeout", status: "failed" };

function annotation(gene) {
  return {
    gene,
    fusions: [],
    cancer_associated: true,
    cancer_association_rationale: "Test rationale.",
    citations: [],
    supporting_quotes: [],
    evidence_cards: [],
    quality_flags: [],
    retrieved_pmids: [],
    retrieval_ranking: [],
    insufficient_evidence: false,
    evidence_support_score: 0.5,
    error: null,
  };
}

function result(...genes) {
  return {
    annotations: genes.map(annotation),
    genes_annotated: genes.length,
    fusions_processed: genes.length,
    run_id: "run-123",
    fusion_evidence: [],
  };
}

function sendsPollHeader(options) {
  const headers = options?.headers || {};
  return Object.keys(headers).some((name) => name.toLowerCase() === "x-openevidence-poll" && headers[name] === "1");
}

// Mirrors the server's negotiation (get_gene_openevidence in main.py): a
// pending answer is HTTP 202 for a client that sends X-OpenEvidence-Poll: 1
// and HTTP 503 for one that doesn't (old cached frontends). `legacyServer`
// models a server pod that predates the header and always sends 503.
function httpResponse(payload, options, { legacyServer = false } = {}) {
  const pending = payload?.status === "pending";
  const accepted = pending && !legacyServer && sendsPollHeader(options);
  const unavailable = pending && !accepted;
  return {
    ok: !unavailable,
    status: accepted ? 202 : unavailable ? 503 : 200,
    statusText: accepted ? "Accepted" : unavailable ? "Service Unavailable" : "OK",
    json: async () => payload,
  };
}

function geneOf(url) {
  const match = /^\/v1\/genes\/([^/]+)\/openevidence/.exec(url);
  return match ? decodeURIComponent(match[1]) : null;
}

// `answers` maps gene -> function(callIndex) returning the JSON payload the
// server answers with on that gene's Nth request (0-based).
async function setup(answers, { poll, routes = {}, legacyServer = false } = {}) {
  const calls = [];
  const statuses = [];
  const headerFlags = [];
  const aborted = [];
  const fetchImpl = async (url, options) => {
    url = String(url);
    if (url === "/v1/dev/status") {
      return { ok: true, status: 200, json: async () => ({ enabled: false, openevidence_enabled: true }) };
    }
    if (routes[url]) return routes[url]();
    calls.push(url);
    headerFlags.push(sendsPollHeader(options));
    options?.signal?.addEventListener("abort", () => aborted.push(url));
    const gene = geneOf(url);
    const index = calls.filter((u) => geneOf(u) === gene).length - 1;
    // An answer may be a promise (e.g. one that never settles, to model a
    // stalled request — which, like a real server, ignores the abort).
    const answer = httpResponse(await answers[gene](index), options, { legacyServer });
    statuses.push(answer.status);
    return answer;
  };
  const sandbox = loadApp({ fetchImpl });
  Object.assign(sandbox.OPENEVIDENCE_POLL, {
    defaultDelayMs: 1,
    minDelayMs: 0,
    maxDelayMs: 5,
    backoff: 1.5,
    totalCapMs: 60 * 1000,
    ...poll,
  });
  await sandbox.loadDevStatus();
  assert.strictEqual(sandbox.state.openevidenceEnabled, true);

  const rendered = [];
  const originalRenderBody = sandbox.renderOpenEvidenceCardBody;
  sandbox.renderOpenEvidenceCardBody = (card, body, response) => {
    rendered.push({ card, response });
    return originalRenderBody(card, body, response);
  };
  const notices = [];
  const originalRenderNotice = sandbox.renderOpenEvidenceCardNotice;
  sandbox.renderOpenEvidenceCardNotice = (body, message, kind) => {
    notices.push({ message, kind });
    return originalRenderNotice(body, message, kind);
  };
  const loadingMessages = [];
  const originalRenderLoading = sandbox.renderLoadingState;
  sandbox.renderLoadingState = (message) => {
    loadingMessages.push(message);
    return originalRenderLoading(message);
  };
  const callsFor = (gene) => calls.filter((url) => geneOf(url) === gene);
  return { sandbox, calls, callsFor, rendered, notices, loadingMessages, aborted, statuses, headerFlags };
}

// Attaches a card to the (document-owned) results window, the way
// applyResultsViewMode does, so card.isConnected is true.
function mountCard(sandbox, gene) {
  const card = sandbox.renderOpenEvidenceCard(annotation(gene));
  assert.notStrictEqual(card, null);
  sandbox.elements.resultsWindow.appendChild(card);
  return card;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(predicate, message, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${message}`);
    await sleep(1);
  }
}

// Replaces the sandbox's clock (Date.now) and timers with a manual one, so
// deadline tests assert exact virtual times instead of racing the wall
// clock on a loaded runner. advance(ms) fires due timers in order, letting
// promise callbacks settle after each.
function installFakeClock(sandbox) {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  sandbox.Date = class extends Date {
    static now() {
      return now;
    }
  };
  sandbox.setTimeout = (fn, ms = 0) => {
    const id = nextId++;
    timers.set(id, { fn, at: now + Math.max(0, Number(ms) || 0) });
    return id;
  };
  sandbox.clearTimeout = (id) => timers.delete(id);
  const settle = async () => {
    for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
  };
  return {
    now: () => now,
    async advance(ms) {
      const target = now + ms;
      await settle();
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= target).sort((a, b) => a[1].at - b[1].at || a[0] - b[0]);
        if (due.length === 0) break;
        const [id, timer] = due[0];
        timers.delete(id);
        now = timer.at;
        timer.fn();
        await settle();
      }
      now = target;
      await settle();
    },
  };
}

async function stopEverything(sandbox) {
  sandbox.state.openevidenceEnabled = false; // every scheduled poll bails out on its next tick
  await sleep(sandbox.OPENEVIDENCE_POLL.maxDelayMs + 20);
}

async function test_pending_polls_until_ready_then_renders() {
  const { sandbox, callsFor, rendered } = await setup({ ALK: (i) => (i < 3 ? PENDING : READY) });
  const card = mountCard(sandbox, "ALK");

  await waitFor(() => rendered.length === 1, "the ready answer to render");
  assert.strictEqual(callsFor("ALK").length, 4, "3 pending answers then 1 ready answer");
  assert.strictEqual(rendered[0].response.status, "ready");
  assert.strictEqual(card._removed, undefined, "a ready card must stay on the page");
  // Every poll re-requests the same URL (pending is never memoized).
  assert.strictEqual(new Set(callsFor("ALK")).size, 1);
  assert.deepStrictEqual(Object.keys(sandbox.state.openEvidenceByGene), ["ALK||"], "ready is memoized");

  await sleep(20);
  assert.strictEqual(callsFor("ALK").length, 4, "polling must stop once ready");
}

async function test_pending_shows_still_checking_state_once() {
  const { sandbox, callsFor, rendered, loadingMessages } = await setup({ ALK: (i) => (i < 3 ? PENDING : READY) });
  mountCard(sandbox, "ALK");

  await waitFor(() => rendered.length === 1, "the ready answer to render");
  assert.strictEqual(callsFor("ALK").length, 4);
  // The initial "Checking…" spinner, then the pending note exactly once (not per poll).
  assert.deepStrictEqual(loadingMessages, ["Checking OpenEvidence…", sandbox.OPENEVIDENCE_MESSAGES.pending]);
}

async function test_failed_after_pending_shows_failed_state_and_stops() {
  const { sandbox, callsFor, rendered, notices } = await setup({ ALK: (i) => (i < 2 ? PENDING : FAILED) });
  const card = mountCard(sandbox, "ALK");

  await waitFor(() => notices.length === 1, "the failed state to render");
  assert.deepStrictEqual(notices, [{ message: sandbox.OPENEVIDENCE_MESSAGES.failed, kind: "failed" }]);
  assert.strictEqual(card._removed, undefined, "a failed card stays on the page with its failed note");
  assert.strictEqual(rendered.length, 0);
  assert.strictEqual(callsFor("ALK").length, 3);
  await sleep(20);
  assert.strictEqual(callsFor("ALK").length, 3, "polling must stop once failed");
}

async function test_polling_times_out_and_shows_timeout_state() {
  const { sandbox, callsFor, notices } = await setup({ ALK: () => PENDING }, { poll: { totalCapMs: 40 } });
  const card = mountCard(sandbox, "ALK");

  await waitFor(() => notices.length === 1, "the timed-out state to render at the total polling cap");
  assert.deepStrictEqual(notices, [{ message: sandbox.OPENEVIDENCE_MESSAGES.timeout, kind: "timeout" }]);
  assert.strictEqual(card._removed, undefined, "a timed-out card stays on the page with its note");
  const pollsAtTimeout = callsFor("ALK").length;
  assert.ok(pollsAtTimeout >= 2, `expected several polls before the cap; saw ${pollsAtTimeout}`);
  await sleep(30);
  assert.strictEqual(callsFor("ALK").length, pollsAtTimeout, "no polls after the cap");
}

async function test_backoff_starts_at_retry_after_and_is_capped() {
  const { sandbox } = await setup({ ALK: () => PENDING });
  Object.assign(sandbox.OPENEVIDENCE_POLL, { defaultDelayMs: 10000, minDelayMs: 1000, maxDelayMs: 20000, backoff: 1.5 });
  const next = sandbox.nextOpenEvidencePollDelayMs;
  let delay = next({ retry_after_seconds: 10 }, null);
  assert.strictEqual(delay, 10000, "first poll waits the server's retry_after hint");
  delay = next({ retry_after_seconds: 10 }, delay);
  assert.strictEqual(delay, 15000);
  delay = next({ retry_after_seconds: 10 }, delay);
  assert.strictEqual(delay, 20000, "capped at maxDelayMs");
  assert.strictEqual(next({ retry_after_seconds: 10 }, delay), 20000);
  assert.strictEqual(next({}, null), 10000, "no hint falls back to defaultDelayMs");
}

async function test_polling_stops_when_card_is_removed() {
  const { sandbox, callsFor } = await setup({ ALK: () => PENDING });
  const card = mountCard(sandbox, "ALK");

  await waitFor(() => callsFor("ALK").length >= 2, "polling to start");
  card.remove();
  await sleep(20);
  const pollsAfterRemoval = callsFor("ALK").length;
  await sleep(30);
  assert.strictEqual(callsFor("ALK").length, pollsAfterRemoval, "a removed card must stop polling");
}

async function test_new_annotation_run_stops_polling_for_replaced_cards() {
  const { sandbox, callsFor, rendered } = await setup({ ALK: () => PENDING, TP53: () => READY });
  const first = result("ALK");
  sandbox.state.currentResult = first;
  sandbox.renderAnnotationResult(first);
  assert.notStrictEqual(findById(sandbox.elements.resultsWindow, "openevidence-ALK"), null);
  await waitFor(() => callsFor("ALK").length >= 2, "the ALK card to start polling");

  // A new annotation run replaces the whole results list.
  const second = result("TP53");
  sandbox.state.currentResult = second;
  sandbox.renderAnnotationResult(second);
  await waitFor(() => rendered.some((r) => r.response.status === "ready"), "the new run's card to render");
  await sleep(20);
  const alkPolls = callsFor("ALK").length;
  await sleep(30);
  assert.strictEqual(callsFor("ALK").length, alkPolls, "the replaced ALK card must stop polling");
}

async function test_flag_turning_off_stops_polling_with_no_more_requests() {
  const { sandbox, calls, callsFor } = await setup({ ALK: () => PENDING });
  const card = mountCard(sandbox, "ALK");
  await waitFor(() => callsFor("ALK").length >= 2, "polling to start");

  sandbox.state.openevidenceEnabled = false;
  await sleep(20);
  assert.strictEqual(card._removed, true, "the card is dropped when the flag turns off");
  const total = calls.length;
  await sleep(30);
  assert.strictEqual(calls.length, total, "no OpenEvidence requests once the flag is off");
}

async function test_flag_off_pending_capable_client_makes_zero_requests() {
  const calls = [];
  const sandbox = loadApp({
    fetchImpl: async (url) => {
      calls.push(String(url));
      if (String(url) === "/v1/dev/status") {
        return { ok: true, status: 200, json: async () => ({ enabled: false, openevidence_enabled: false }) };
      }
      return httpResponse(PENDING);
    },
  });
  await sandbox.loadDevStatus();
  assert.strictEqual(sandbox.renderOpenEvidenceCard(annotation("ALK")), null);
  sandbox.renderAnnotationResult(result("ALK", "BRAF"));
  await sleep(30);
  assert.deepStrictEqual(calls.filter((url) => url.includes("/openevidence")), []);
}

async function test_pending_cards_do_not_occupy_the_fetch_queue() {
  // Slow polls (50ms apart) for three permanently pending cards — as many
  // as OPENEVIDENCE_MAX_CONCURRENT_FETCHES — must not block a fourth card.
  const { sandbox, callsFor, rendered } = await setup(
    { ALK: () => PENDING, BRAF: () => PENDING, EGFR: () => PENDING, TP53: () => READY },
    { poll: { minDelayMs: 50, maxDelayMs: 50 } }
  );
  ["ALK", "BRAF", "EGFR"].forEach((gene) => mountCard(sandbox, gene));
  await waitFor(
    () => ["ALK", "BRAF", "EGFR"].every((gene) => callsFor(gene).length === 1),
    "the three pending cards' first requests"
  );

  mountCard(sandbox, "TP53");
  await waitFor(() => rendered.some((r) => r.response.status === "ready"), "the fourth card to render", 1000);
  assert.strictEqual(callsFor("TP53").length, 1);
  await stopEverything(sandbox);
}

async function test_non_pending_503_is_an_error_and_not_memoized() {
  const calls = [];
  const sandbox = loadApp({
    fetchImpl: async (url) => {
      calls.push(String(url));
      if (String(url) === "/v1/dev/status") {
        return { ok: true, status: 200, json: async () => ({ enabled: false, openevidence_enabled: true }) };
      }
      return { ok: false, status: 503, statusText: "Service Unavailable", json: async () => ({ message: "no healthy upstream" }) };
    },
  });
  await sandbox.loadDevStatus();
  await assert.rejects(sandbox.fetchGeneOpenEvidence("ALK", null, {}));
  assert.deepStrictEqual(Object.keys(sandbox.state.openEvidenceByGene), []);
}

async function test_failed_answer_is_kept_for_the_run_but_a_new_run_recovers() {
  const { sandbox, callsFor, rendered, notices } = await setup({ ALK: (i) => (i === 0 ? FAILED : READY) });
  const run = result("ALK");
  renderRun(sandbox, run);
  await waitFor(() => notices.length === 1, "the failed state to render");
  assert.deepStrictEqual(Object.keys(sandbox.state.openEvidenceByGene), [], "failed must not be memoized");

  // Progress re-renders of the same run replay the failed note, no requests.
  renderRun(sandbox, run);
  renderRun(sandbox, run);
  await sleep(10);
  assert.strictEqual(callsFor("ALK").length, 1);
  assert.deepStrictEqual(notices.map((notice) => notice.kind), ["failed", "failed", "failed"]);

  // A new run (after the server's failure record expired) asks again and recovers.
  renderRun(sandbox, { ...run, run_id: "run-2" });
  await waitFor(() => rendered.length === 1, "the new run's ready answer to render");
  assert.strictEqual(callsFor("ALK").length, 2, "the new run must make a fresh request");
  assert.strictEqual(rendered[0].response.status, "ready");
}

async function test_leaving_the_results_view_stops_polling_and_aborts_in_flight_requests() {
  const gates = [];
  const { sandbox, callsFor, aborted } = await setup({
    ALK: () => PENDING,
    BRAF: () => new Promise((resolve) => gates.push(() => resolve(PENDING))),
  });
  const run = result("ALK", "BRAF");
  renderRun(sandbox, run);
  await waitFor(() => callsFor("ALK").length >= 2 && callsFor("BRAF").length === 1, "polling to start");

  sandbox.switchView("benchmark");
  assert.deepStrictEqual(aborted, [callsFor("BRAF")[0]], "the in-flight request is aborted on navigation");
  const callsAtSwitch = callsFor("ALK").length + callsFor("BRAF").length;
  gates.forEach((open) => open()); // BRAF's answer arrives after navigation
  await sleep(sandbox.OPENEVIDENCE_POLL.maxDelayMs * 6 + 30);
  assert.strictEqual(callsFor("ALK").length + callsFor("BRAF").length, callsAtSwitch, "no polls after leaving the results view");

  // An annotation job's progress tick re-renders the hidden results panel
  // while the user is still on Benchmark: it must not restart polling.
  renderRun(sandbox, run);
  await sleep(sandbox.OPENEVIDENCE_POLL.maxDelayMs * 6 + 30);
  assert.strictEqual(sandbox.state.currentView, "benchmark");
  assert.strictEqual(
    callsFor("ALK").length + callsFor("BRAF").length,
    callsAtSwitch,
    "a progress render while away from Results makes no sidecar request"
  );

  // Back on Results, the normal render path resumes the cards.
  sandbox.switchView("annotate");
  await waitFor(
    () => callsFor("ALK").length + callsFor("BRAF").length >= callsAtSwitch + 3 && callsFor("BRAF").length === 2,
    "polling to resume on returning to Results"
  );
  sandbox.switchView("benchmark");
}

async function test_rerendering_results_reuses_an_in_flight_request() {
  const gates = [];
  const { sandbox, callsFor, rendered, aborted } = await setup({
    ALK: () => new Promise((resolve) => gates.push(() => resolve(READY))),
  });
  const run = result("ALK");
  sandbox.state.currentResult = run;
  sandbox.renderAnnotationResult(run);
  await waitFor(() => callsFor("ALK").length === 1, "the ALK request to start");

  // A job-progress poll re-renders the same results while ALK is in flight.
  sandbox.renderAnnotationResult(run);
  sandbox.renderAnnotationResult(run);
  await sleep(10);
  assert.deepStrictEqual(aborted, [], "a re-render must not abort the in-flight request");
  assert.strictEqual(callsFor("ALK").length, 1, "the re-rendered card reuses the in-flight request");

  gates.forEach((open) => open());
  await waitFor(() => rendered.length >= 1, "the re-rendered card to render the answer");
  assert.ok(findById(sandbox.elements.resultsWindow, "openevidence-ALK"), "the current card is on the page");
}

function renderRun(sandbox, run) {
  sandbox.state.currentResult = run;
  sandbox.renderAnnotationResult(run);
}

async function setupThreeStalled(poll) {
  const gates = [];
  const stalled = () => new Promise((resolve) => gates.push(() => resolve(READY)));
  const env = await setup({ ALK: stalled, BRAF: stalled, EGFR: stalled, KRAS: () => READY }, { poll });
  const run = result("ALK", "BRAF", "EGFR");
  renderRun(env.sandbox, run);
  await waitFor(
    () => ["ALK", "BRAF", "EGFR"].every((gene) => env.callsFor(gene).length === 1),
    "three stalled requests to fill every fetch slot"
  );
  renderRun(env.sandbox, run); // a job-progress re-render while they're in flight
  return { ...env, gates, run };
}

async function test_rerendered_requests_still_time_out_abort_and_free_their_slots() {
  const { sandbox, callsFor, notices, aborted, rendered, gates } = await setupThreeStalled({ totalCapMs: 60 });

  await waitFor(() => notices.length === 3, "the re-rendered cards to show the timeout state");
  assert.ok(notices.every((notice) => notice.kind === "timeout"));
  assert.strictEqual(aborted.length, 3, "the original (kept) requests are aborted at the deadline");
  assert.ok(["ALK", "BRAF", "EGFR"].every((gene) => callsFor(gene).length === 1), "the re-render re-requested nothing");

  sandbox.OPENEVIDENCE_POLL.totalCapMs = 60 * 1000;
  renderRun(sandbox, result("KRAS"));
  await waitFor(() => rendered.length === 1, "KRAS to get a freed slot and render");
  assert.strictEqual(callsFor("KRAS").length, 1);
  gates.forEach((open) => open());
  await sleep(10);
  assert.strictEqual(rendered.length, 1, "late answers are ignored");
}

async function test_navigation_after_a_rerender_aborts_kept_requests_and_frees_their_slots() {
  const { sandbox, callsFor, aborted, rendered } = await setupThreeStalled();

  sandbox.switchView("benchmark");
  assert.strictEqual(aborted.length, 3, "navigation aborts the requests the re-render kept");

  sandbox.state.currentResult = result("KRAS");
  sandbox.switchView("annotate");
  await waitFor(() => rendered.length === 1, "coming back, KRAS gets a freed slot and renders");
  assert.strictEqual(callsFor("KRAS").length, 1);
}

async function test_repeated_rerenders_do_not_extend_or_restart_the_deadline() {
  const gates = [];
  const { sandbox, callsFor, notices } = await setup(
    { ALK: () => new Promise((resolve) => gates.push(() => resolve(READY))) },
    { poll: { totalCapMs: 30 } }
  );
  const clock = installFakeClock(sandbox);
  const run = result("ALK");
  while (notices.length === 0 && clock.now() < 300) {
    renderRun(sandbox, run); // re-render every 10ms, well inside the 30ms cap
    await clock.advance(10);
  }
  assert.strictEqual(notices.length, 1, "the deadline must expire despite continuous re-renders");
  assert.strictEqual(notices[0].kind, "timeout");
  assert.strictEqual(clock.now(), 30, "the first lifecycle's 30ms deadline was neither extended nor restarted");
  assert.strictEqual(callsFor("ALK").length, 1, "re-renders reused the one request");

  // Keep re-rendering the same run past the timeout: no new lifecycle, no
  // fresh deadline, no new requests — the timed-out note just persists.
  for (let i = 0; i < 8; i += 1) {
    renderRun(sandbox, run);
    await clock.advance(5);
  }
  await clock.advance(40);
  assert.strictEqual(callsFor("ALK").length, 1, "no requests after the timeout within the same run");
  assert.ok(notices.length > 1 && notices.every((notice) => notice.kind === "timeout"), "the timeout note persists");
}

async function test_a_new_run_does_not_inherit_an_old_runs_deadline() {
  const { sandbox, callsFor, notices, aborted } = await setup(
    { ALK: () => new Promise(() => {}) },
    { poll: { totalCapMs: 80 } }
  );
  const clock = installFakeClock(sandbox);
  const runA = result("ALK");
  renderRun(sandbox, runA);
  await clock.advance(50); // 50ms into run A's 80ms deadline
  assert.strictEqual(callsFor("ALK").length, 1, "run A made its request");

  renderRun(sandbox, { ...runA, run_id: "run-B" }); // run B starts at t=50
  assert.strictEqual(aborted.length, 1, "run A's request is cancelled");
  await clock.advance(0);
  assert.strictEqual(callsFor("ALK").length, 2, "run B makes its own request");
  await clock.advance(45); // t=95: past run A's deadline, well inside run B's
  assert.strictEqual(notices.length, 0, "run B must not time out on run A's deadline");

  await clock.advance(34); // t=129: 1ms before run B's own deadline
  assert.strictEqual(notices.length, 0, "run B's deadline runs a full 80ms from its own start");
  await clock.advance(1); // t=130
  assert.strictEqual(notices.length, 1, "run B's own deadline expires");
  assert.strictEqual(notices[0].kind, "timeout");
}

async function test_job_completion_switching_to_the_final_run_id_is_the_same_run() {
  // Drives the real pollAnnotationJob: one "running" status (rendered with
  // run_id = job_id) during which ALK times out, then "complete" with the
  // final run_id. The completed render must not count as a new run.
  const statuses = [
    { status: "running", job_id: "job-1", annotations: [annotation("ALK")], genes_total: 1, genes_completed: 0 },
    { status: "complete", job_id: "job-1", result: { ...result("ALK"), run_id: "run-final" } },
  ];
  const { sandbox, callsFor, notices } = await setup(
    { ALK: () => new Promise(() => {}) },
    {
      poll: { totalCapMs: 30 },
      routes: { "/v1/jobs/job-1": async () => ({ ok: true, status: 200, json: async () => statuses.shift() }) },
    }
  );
  sandbox.sleep = () => sleep(80); // the job's poll interval: ALK's 30ms deadline expires meanwhile

  const final = await sandbox.pollAnnotationJob("/v1/jobs/job-1");
  assert.strictEqual(final.run_id, "run-final");
  await sleep(20);
  assert.strictEqual(callsFor("ALK").length, 1, "completing the same job is not a new run");
  assert.ok(notices.length >= 2, "the completed render replays the timed-out note");
  assert.ok(notices.every((notice) => notice.kind === "timeout"));
}

async function test_a_new_run_retries_a_key_that_timed_out() {
  const gates = [];
  const { sandbox, callsFor, notices } = await setup(
    { ALK: () => new Promise((resolve) => gates.push(() => resolve(READY))) },
    { poll: { totalCapMs: 30 } }
  );
  const run = result("ALK");
  renderRun(sandbox, run);
  await waitFor(() => notices.length === 1, "the first run to time out");

  renderRun(sandbox, { ...run, run_id: "run-2" });
  await waitFor(() => callsFor("ALK").length === 2, "a new run to request again");
  await waitFor(() => notices.length === 2, "the new run gets its own deadline");
}

async function test_queued_card_removed_before_its_turn_makes_no_request() {
  const gates = [];
  const stalled = () => new Promise((resolve) => gates.push(() => resolve(READY)));
  const { sandbox, callsFor } = await setup({ ALK: stalled, BRAF: stalled, EGFR: stalled, KRAS: () => READY });
  ["ALK", "BRAF", "EGFR"].forEach((gene) => mountCard(sandbox, gene)); // fill every fetch slot
  const queued = mountCard(sandbox, "KRAS");
  await sleep(5);
  assert.strictEqual(callsFor("KRAS").length, 0, "KRAS waits for a free slot");

  queued.remove();
  gates.forEach((open) => open()); // free the slots
  await sleep(30);
  assert.strictEqual(callsFor("KRAS").length, 0, "a removed card's queued request must never be sent");
}

async function test_stalled_request_times_out_frees_its_slot_and_ignores_late_answers() {
  const gates = [];
  const stalled = () => new Promise((resolve) => gates.push(() => resolve(READY)));
  const { sandbox, callsFor, rendered, notices, aborted } = await setup(
    { ALK: stalled, BRAF: stalled, EGFR: stalled, KRAS: () => READY },
    { poll: { totalCapMs: 40 } }
  );
  const cards = ["ALK", "BRAF", "EGFR"].map((gene) => mountCard(sandbox, gene)); // fill every fetch slot

  await waitFor(() => notices.length === 3, "every stalled card to show the timeout state");
  assert.ok(notices.every((notice) => notice.kind === "timeout"));
  assert.ok(cards.every((card) => card._removed === undefined), "timed-out cards stay with their note");
  assert.strictEqual(aborted.length, 3, "each stalled request is aborted at the deadline");

  // Their slots were freed even though the requests never settled.
  sandbox.OPENEVIDENCE_POLL.totalCapMs = 60 * 1000;
  mountCard(sandbox, "KRAS");
  await waitFor(() => rendered.length === 1, "a new card to get a slot and render");
  assert.strictEqual(callsFor("KRAS").length, 1);

  gates.forEach((open) => open()); // the stalled answers finally arrive
  await sleep(20);
  assert.strictEqual(rendered.length, 1, "late answers after the deadline are ignored");
  assert.strictEqual(notices.length, 3);
}

async function test_every_sidecar_request_sends_the_poll_header_and_gets_202_pending() {
  const { sandbox, callsFor, rendered, statuses, headerFlags } = await setup({ ALK: (i) => (i < 2 ? PENDING : READY) });
  mountCard(sandbox, "ALK");
  await waitFor(() => rendered.length === 1, "the ready answer to render");

  assert.strictEqual(callsFor("ALK").length, 3);
  assert.deepStrictEqual(headerFlags, [true, true, true], "every request (first and polls) must send X-OpenEvidence-Poll: 1");
  assert.deepStrictEqual(statuses, [202, 202, 200], "pending is answered 202 to a client that sends the header");
  assert.strictEqual(rendered[0].response.status, "ready");
}

async function test_503_pending_from_a_server_without_header_support_still_polls() {
  // A backend pod that predates the header (e.g. mid rolling deploy)
  // answers pending with 503; the client must keep polling, not drop the card.
  const { sandbox, callsFor, rendered, statuses } = await setup(
    { ALK: (i) => (i < 2 ? PENDING : READY) },
    { legacyServer: true }
  );
  const card = mountCard(sandbox, "ALK");
  await waitFor(() => rendered.length === 1, "the ready answer to render");
  assert.deepStrictEqual(statuses, [503, 503, 200]);
  assert.strictEqual(callsFor("ALK").length, 3);
  assert.strictEqual(card._removed, undefined);
}

async function test_polling_deadline_outlasts_the_backend_lookup_budget() {
  // The backend's default per-lookup budget is 900s
  // (OPENEVIDENCE_SIDECAR_LOOKUP_TIMEOUT_SECONDS) plus queue time; the card
  // must not give up on a lookup the server can still finish.
  const sandbox = loadApp({ fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({}) }) });
  const BACKEND_DEFAULT_LOOKUP_BUDGET_MS = 900 * 1000;
  assert.ok(
    sandbox.OPENEVIDENCE_POLL.totalCapMs >= BACKEND_DEFAULT_LOOKUP_BUDGET_MS + 5 * 60 * 1000,
    `totalCapMs (${sandbox.OPENEVIDENCE_POLL.totalCapMs}) must exceed the 900s backend budget with queue-time headroom`
  );
}

const TESTS = [
  test_every_sidecar_request_sends_the_poll_header_and_gets_202_pending,
  test_503_pending_from_a_server_without_header_support_still_polls,
  test_polling_deadline_outlasts_the_backend_lookup_budget,
  test_pending_polls_until_ready_then_renders,
  test_pending_shows_still_checking_state_once,
  test_failed_after_pending_shows_failed_state_and_stops,
  test_polling_times_out_and_shows_timeout_state,
  test_backoff_starts_at_retry_after_and_is_capped,
  test_polling_stops_when_card_is_removed,
  test_new_annotation_run_stops_polling_for_replaced_cards,
  test_flag_turning_off_stops_polling_with_no_more_requests,
  test_flag_off_pending_capable_client_makes_zero_requests,
  test_pending_cards_do_not_occupy_the_fetch_queue,
  test_non_pending_503_is_an_error_and_not_memoized,
  test_failed_answer_is_kept_for_the_run_but_a_new_run_recovers,
  test_leaving_the_results_view_stops_polling_and_aborts_in_flight_requests,
  test_rerendering_results_reuses_an_in_flight_request,
  test_rerendered_requests_still_time_out_abort_and_free_their_slots,
  test_navigation_after_a_rerender_aborts_kept_requests_and_frees_their_slots,
  test_repeated_rerenders_do_not_extend_or_restart_the_deadline,
  test_a_new_run_retries_a_key_that_timed_out,
  test_job_completion_switching_to_the_final_run_id_is_the_same_run,
  test_a_new_run_does_not_inherit_an_old_runs_deadline,
  test_queued_card_removed_before_its_turn_makes_no_request,
  test_stalled_request_times_out_frees_its_slot_and_ignores_late_answers,
];

async function main() {
  let failures = 0;
  for (const test of TESTS) {
    try {
      await test();
      console.log(`PASS ${test.name}`);
    } catch (err) {
      failures += 1;
      console.error(`FAIL ${test.name}`);
      console.error(err);
    }
  }
  if (failures > 0) {
    console.error(`${failures}/${TESTS.length} frontend polling tests failed`);
    process.exit(1);
  }
  console.log(`${TESTS.length}/${TESTS.length} frontend polling tests passed`);
  process.exit(0);
}

main();
