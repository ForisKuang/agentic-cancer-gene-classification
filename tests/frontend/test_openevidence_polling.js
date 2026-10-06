// Proves the OpenEvidence sidecar card's "pending + poll" handling in
// src/static/app.js (loadOpenEvidenceCard / fetchGeneOpenEvidence): a cache
// miss answers status "pending" (HTTP 503 + retry_after_seconds, see
// GET /v1/genes/{gene}/openevidence in main.py) and the card must keep
// polling until "ready" (render) or "failed"/timeout (remove), stop polling
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

function httpResponse(payload) {
  const pending = payload?.status === "pending";
  return {
    ok: !pending,
    status: pending ? 503 : 200,
    statusText: pending ? "Service Unavailable" : "OK",
    json: async () => payload,
  };
}

function geneOf(url) {
  const match = /^\/v1\/genes\/([^/]+)\/openevidence/.exec(url);
  return match ? decodeURIComponent(match[1]) : null;
}

// `answers` maps gene -> function(callIndex) returning the JSON payload the
// server answers with on that gene's Nth request (0-based).
async function setup(answers, { poll } = {}) {
  const calls = [];
  const fetchImpl = async (url) => {
    url = String(url);
    if (url === "/v1/dev/status") {
      return { ok: true, status: 200, json: async () => ({ enabled: false, openevidence_enabled: true }) };
    }
    calls.push(url);
    const gene = geneOf(url);
    const index = calls.filter((u) => geneOf(u) === gene).length - 1;
    return httpResponse(answers[gene](index));
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
  const callsFor = (gene) => calls.filter((url) => geneOf(url) === gene);
  return { sandbox, calls, callsFor, rendered };
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

async function test_failed_after_pending_removes_card_and_stops() {
  const { sandbox, callsFor } = await setup({ ALK: (i) => (i < 2 ? PENDING : FAILED) });
  const card = mountCard(sandbox, "ALK");

  await waitFor(() => card._removed === true, "the failed card to be removed");
  assert.strictEqual(callsFor("ALK").length, 3);
  await sleep(20);
  assert.strictEqual(callsFor("ALK").length, 3, "polling must stop once failed");
}

async function test_polling_times_out_and_removes_card() {
  const { sandbox, callsFor } = await setup({ ALK: () => PENDING }, { poll: { totalCapMs: 40 } });
  const card = mountCard(sandbox, "ALK");

  await waitFor(() => card._removed === true, "the card to be removed at the total polling cap");
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

const TESTS = [
  test_pending_polls_until_ready_then_renders,
  test_failed_after_pending_removes_card_and_stops,
  test_polling_times_out_and_removes_card,
  test_backoff_starts_at_retry_after_and_is_capped,
  test_polling_stops_when_card_is_removed,
  test_new_annotation_run_stops_polling_for_replaced_cards,
  test_flag_turning_off_stops_polling_with_no_more_requests,
  test_flag_off_pending_capable_client_makes_zero_requests,
  test_pending_cards_do_not_occupy_the_fetch_queue,
  test_non_pending_503_is_an_error_and_not_memoized,
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
