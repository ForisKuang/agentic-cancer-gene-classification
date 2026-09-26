// Proves the frontend OpenEvidence sidecar gate (see review gap #1): when
// settings.openevidence_enabled is off, src/static/app.js must render no
// sidecar element and issue no GET /v1/genes/{gene}/openevidence request.
// Run via tests/test_frontend_openevidence_gate.py (invokes this with node).
"use strict";

const assert = require("assert");
const { loadApp, findById } = require("./openevidence_gate_harness");

const CANCER_ASSOCIATED_ANNOTATION = {
  gene: "ALK",
  cancer_associated: true,
  insufficient_evidence: false,
  fusions: [],
};

// Minimal-but-complete GeneAnnotation shape (see src/models/schema.py) for
// driving the full renderAnnotationResult()/applyResultsViewMode() pipeline
// — not just renderOpenEvidenceCard() in isolation — the way loadSharedRun()
// and runAnnotation() actually do.
function fakeAnnotation(gene) {
  return {
    gene,
    fusions: [],
    in_oncokb: null,
    cancer_associated: true,
    cancer_association_rationale: "Test rationale.",
    cancer_type_prevalence: null,
    gene_class: null,
    signaling_pathways: null,
    gene_summary: null,
    citations: [],
    supporting_quotes: [],
    evidence_cards: [],
    clinical_actionability: null,
    quality_flags: [],
    date_annotated: "1/1/26",
    retrieval_count: 0,
    retrieved_pmids: [],
    retrieval_ranking: [],
    insufficient_evidence: false,
    evidence_support_score: 0.5,
    evidence_support_explanation: "",
    error: null,
  };
}

function fakeResult(gene) {
  return {
    annotations: [fakeAnnotation(gene)],
    genes_annotated: 1,
    fusions_processed: 1,
    run_id: "run-123",
    fusion_evidence: [],
  };
}

async function withFetchLog(devStatusPayload) {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(String(url));
    if (String(url) === "/v1/dev/status") {
      return {
        ok: true,
        json: async () => devStatusPayload,
      };
    }
    return {
      ok: true,
      json: async () => ({ available: true, distilled: { consensus_role: "test" } }),
    };
  };
  const sandbox = loadApp({ fetchImpl });
  return { sandbox, calls };
}

async function test_default_state_is_disabled_before_dev_status_resolves() {
  const { sandbox } = await withFetchLog({ enabled: false, openevidence_enabled: true });
  assert.strictEqual(
    sandbox.state.openevidenceEnabled,
    false,
    "state.openevidenceEnabled must default to false (fail closed) before GET /v1/dev/status resolves"
  );
}

async function test_disabled_renders_no_card_and_issues_no_fetch() {
  const { sandbox, calls } = await withFetchLog({ enabled: false, openevidence_enabled: false });
  await sandbox.loadDevStatus();
  assert.strictEqual(sandbox.state.openevidenceEnabled, false);

  const createdBefore = sandbox.createdElementTags.length;
  const card = sandbox.renderOpenEvidenceCard(CANCER_ASSOCIATED_ANNOTATION);
  assert.strictEqual(card, null, "renderOpenEvidenceCard must render nothing when the flag is disabled");
  assert.strictEqual(
    sandbox.createdElementTags.length,
    createdBefore,
    "renderOpenEvidenceCard must not create any DOM element when the flag is disabled"
  );

  // Give any (incorrectly) queued fetch a turn to fire before asserting.
  await new Promise((resolve) => setTimeout(resolve, 0));

  const openEvidenceCalls = calls.filter((url) => url.includes("/openevidence"));
  assert.deepStrictEqual(
    openEvidenceCalls,
    [],
    `expected no GET /v1/genes/{gene}/openevidence request; saw: ${JSON.stringify(openEvidenceCalls)}`
  );
}

async function test_fetchGeneOpenEvidence_is_a_noop_when_disabled() {
  const { sandbox, calls } = await withFetchLog({ enabled: false, openevidence_enabled: false });
  await sandbox.loadDevStatus();
  assert.strictEqual(sandbox.state.openevidenceEnabled, false);

  // deepStrictEqual isn't usable here: the resolved object is an instance
  // of the vm sandbox's own Object constructor, a different realm from this
  // process's Object — structurally equal but never reference-equal.
  const response = await sandbox.fetchGeneOpenEvidence("ALK", null, {});
  assert.strictEqual(response.available, false);
  assert.deepStrictEqual(Object.keys(response), ["available"]);

  const openEvidenceCalls = calls.filter((url) => url.includes("/openevidence"));
  assert.deepStrictEqual(openEvidenceCalls, []);
}

async function test_enabled_still_renders_card_and_fetches() {
  const { sandbox, calls } = await withFetchLog({ enabled: false, openevidence_enabled: true });
  await sandbox.loadDevStatus();
  assert.strictEqual(sandbox.state.openevidenceEnabled, true);

  const card = sandbox.renderOpenEvidenceCard(CANCER_ASSOCIATED_ANNOTATION);
  assert.notStrictEqual(card, null, "renderOpenEvidenceCard must still render when the flag is enabled");

  await new Promise((resolve) => setTimeout(resolve, 0));

  const openEvidenceCalls = calls.filter((url) => url.includes("/openevidence"));
  assert.strictEqual(
    openEvidenceCalls.length,
    1,
    `expected exactly one GET .../openevidence request when enabled; saw: ${JSON.stringify(openEvidenceCalls)}`
  );
}

async function test_sidecar_appears_after_late_flag_resolution_for_already_rendered_genes() {
  // Regression test for the startup race: loadDevStatus() and
  // loadSharedRun() fire concurrently (see bottom of app.js). If a shared
  // run's results render BEFORE loadDevStatus() resolves, they render with
  // the fail-closed default (openevidenceEnabled: false) and previously got
  // no OpenEvidence card at all — with nothing to ever re-trigger rendering
  // for those genes once the flag resolved to true.
  const { sandbox, calls } = await withFetchLog({ enabled: false, openevidence_enabled: true });
  assert.strictEqual(sandbox.state.openevidenceEnabled, false, "must still be fail-closed pre-bootstrap");

  // Simulate loadSharedRun() completing and rendering results FIRST, before
  // loadDevStatus() has resolved — the exact ordering that used to leave
  // the sidecar permanently missing.
  const result = fakeResult("ALK");
  sandbox.state.currentResult = result;
  sandbox.renderAnnotationResult(result);

  assert.strictEqual(
    findById(sandbox.elements.resultsWindow, "openevidence-ALK"),
    null,
    "no OpenEvidence card should exist yet — the flag hadn't resolved when this render happened"
  );
  assert.deepStrictEqual(
    calls.filter((url) => url.includes("/openevidence")),
    [],
    "no OpenEvidence fetch should have fired yet either"
  );

  // GET /v1/dev/status now resolves with openevidence_enabled:true.
  await sandbox.loadDevStatus();
  assert.strictEqual(sandbox.state.openevidenceEnabled, true);

  // The already-rendered gene must now actually have a sidecar card, and it
  // must have actually fetched — not just the boolean flag flipping.
  const card = findById(sandbox.elements.resultsWindow, "openevidence-ALK");
  assert.notStrictEqual(
    card,
    null,
    "the OpenEvidence card must appear for the already-rendered gene once the flag resolves to true"
  );

  await new Promise((resolve) => setTimeout(resolve, 0));

  const openEvidenceCalls = calls.filter((url) => url.includes("/openevidence"));
  assert.strictEqual(
    openEvidenceCalls.length,
    1,
    `expected the reconciled card to actually fetch; saw: ${JSON.stringify(openEvidenceCalls)}`
  );
}

function openEvidenceCallsOf(calls) {
  return calls.filter((url) => url.includes("/openevidence"));
}

function fusionParamOf(url) {
  const query = url.includes("?") ? url.slice(url.indexOf("?") + 1) : "";
  return new URLSearchParams(query).get("fusion");
}

async function flushFetchQueue() {
  for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

// The backend asks (and caches) a fusion-specific question when `fusion` is
// sent — see _cache_key in src/pipeline/openevidence.py — and the offline
// warmup fills that fusion-specific slot with annotation.fusions[0]'s value.
// The rendered card must send the same fusion so it hits that slot.
async function test_fusion_gene_sends_fusion_param_and_plain_gene_does_not() {
  const { sandbox, calls } = await withFetchLog({ enabled: false, openevidence_enabled: true });
  await sandbox.loadDevStatus();

  const fusionAnnotation = { ...fakeAnnotation("ALK"), fusions: ["EML4::ALK"] };
  const plainAnnotation = fakeAnnotation("TP53");
  assert.notStrictEqual(sandbox.renderOpenEvidenceCard(fusionAnnotation), null);
  assert.notStrictEqual(sandbox.renderOpenEvidenceCard(plainAnnotation), null);
  await flushFetchQueue();

  const openEvidenceCalls = openEvidenceCallsOf(calls);
  const alkCall = openEvidenceCalls.find((url) => url.startsWith("/v1/genes/ALK/openevidence"));
  const tp53Call = openEvidenceCalls.find((url) => url.startsWith("/v1/genes/TP53/openevidence"));
  assert.ok(alkCall, `expected an ALK OpenEvidence request; saw: ${JSON.stringify(openEvidenceCalls)}`);
  assert.ok(tp53Call, `expected a TP53 OpenEvidence request; saw: ${JSON.stringify(openEvidenceCalls)}`);

  assert.ok(
    alkCall.includes("fusion=EML4%3A%3AALK"),
    `fusion gene must send URL-encoded fusion=EML4::ALK; saw: ${alkCall}`
  );
  assert.strictEqual(fusionParamOf(alkCall), "EML4::ALK");
  assert.strictEqual(
    fusionParamOf(tp53Call),
    null,
    `plain gene must not send a fusion param; saw: ${tp53Call}`
  );
}

async function test_fusion_and_plain_requests_for_same_gene_do_not_share_cache() {
  const { sandbox, calls } = await withFetchLog({ enabled: false, openevidence_enabled: true });
  await sandbox.loadDevStatus();

  await sandbox.fetchGeneOpenEvidence("ALK", "NSCLC", { fusion: "EML4::ALK" });
  await sandbox.fetchGeneOpenEvidence("ALK", "NSCLC", {});
  let openEvidenceCalls = openEvidenceCallsOf(calls);
  assert.strictEqual(
    openEvidenceCalls.length,
    2,
    `fusion and plain lookups for the same gene must each fetch; saw: ${JSON.stringify(openEvidenceCalls)}`
  );
  assert.strictEqual(fusionParamOf(openEvidenceCalls[0]), "EML4::ALK");
  assert.strictEqual(fusionParamOf(openEvidenceCalls[1]), null);

  // Each variant is still cached under its own key — repeats don't re-fetch.
  await sandbox.fetchGeneOpenEvidence("ALK", "NSCLC", { fusion: "EML4::ALK" });
  await sandbox.fetchGeneOpenEvidence("ALK", "NSCLC", {});
  openEvidenceCalls = openEvidenceCallsOf(calls);
  assert.strictEqual(openEvidenceCalls.length, 2, `repeat lookups must hit the client cache; saw: ${JSON.stringify(openEvidenceCalls)}`);
  assert.strictEqual(Object.keys(sandbox.state.openEvidenceByGene).length, 2);
}

async function test_fusion_gene_disabled_issues_no_fetch() {
  const { sandbox, calls } = await withFetchLog({ enabled: false, openevidence_enabled: false });
  await sandbox.loadDevStatus();
  assert.strictEqual(sandbox.state.openevidenceEnabled, false);

  const fusionAnnotation = { ...fakeAnnotation("ALK"), fusions: ["EML4::ALK"] };
  assert.strictEqual(sandbox.renderOpenEvidenceCard(fusionAnnotation), null);
  const response = await sandbox.fetchGeneOpenEvidence("ALK", null, { fusion: "EML4::ALK" });
  assert.strictEqual(response.available, false);
  await flushFetchQueue();

  assert.deepStrictEqual(openEvidenceCallsOf(calls), []);
  assert.strictEqual(Object.keys(sandbox.state.openEvidenceByGene).length, 0);
}

const TESTS = [
  test_default_state_is_disabled_before_dev_status_resolves,
  test_disabled_renders_no_card_and_issues_no_fetch,
  test_fetchGeneOpenEvidence_is_a_noop_when_disabled,
  test_enabled_still_renders_card_and_fetches,
  test_sidecar_appears_after_late_flag_resolution_for_already_rendered_genes,
  test_fusion_gene_sends_fusion_param_and_plain_gene_does_not,
  test_fusion_and_plain_requests_for_same_gene_do_not_share_cache,
  test_fusion_gene_disabled_issues_no_fetch,
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
    console.error(`${failures}/${TESTS.length} frontend gate tests failed`);
    process.exit(1);
  }
  console.log(`${TESTS.length}/${TESTS.length} frontend gate tests passed`);
}

main();
