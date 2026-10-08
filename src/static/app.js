const SIDEBAR_DEFAULT_WIDTH = 360;
const SIDEBAR_MIN_WIDTH = 280;
const SIDEBAR_MAX_WIDTH = 920;

const GRID_COLUMNS = [
  { key: "fusion",           label: "Gene/Fusion",   required: true,  type: "text",   width: 140 },
  { key: "tumor_type",       label: "Tumor Type",    required: false, type: "text",   width: 110 },
  { key: "five_exon",        label: "5′ Exon",       required: false, type: "number", width: 62 },
  { key: "three_exon",       label: "3′ Exon",       required: false, type: "number", width: 62 },
  { key: "five_genomic",     label: "5′ Genomic",    required: false, type: "text",   width: 100 },
  { key: "three_genomic",    label: "3′ Genomic",    required: false, type: "text",   width: 100 },
  { key: "five_transcript",  label: "5′ Transcript", required: false, type: "text",   width: 105 },
  { key: "three_transcript", label: "3′ Transcript", required: false, type: "text",   width: 105 },
];

const INPUT_EXAMPLES = {
  "alk-gene": {
    fusion: "ALK",
    tumor_type: "LUAD",
  },
  "braf-gene": {
    fusion: "BRAF",
    tumor_type: "melanoma",
  },
  "eml4-alk-fusion": {
    fusion: "EML4::ALK",
    tumor_type: "LUAD",
    five_exon: "13",
    three_exon: "20",
  },
  "bcr-abl1-fusion": {
    fusion: "BCR::ABL1",
    tumor_type: "CML",
  },
};

function emptyRow() {
  return GRID_COLUMNS.reduce((acc, col) => ({ ...acc, [col.key]: "" }), {});
}

const state = {
  isRunning: false,
  isBenchmarkRunning: false,
  currentResult: null,
  currentBenchmark: null,
  currentView: "annotate",
  resultsViewMode: "results",
  fusionEvidenceStatus: null,
  inputMode: "single",
  queue: [],
  batchRows: Array.from({ length: 5 }, emptyRow),
  auth: {
    enabled: false,
    authenticated: false,
    user: null,
    allowedDomains: [],
  },
  enrichmentByGene: new Map(),
  // Breakpoint fields from the most recent submission, keyed by the exact
  // fusion string as submitted (matches GeneAnnotation.fusions entries) —
  // needed by the on-demand /v1/fusion-context lookup, since breakpoint
  // context isn't persisted on the annotation result itself.
  lastInputsByFusion: {},
  // In-flight/completed /v1/fusion-context lookups, keyed by fusion string,
  // so re-expanding a disclosure (or expanding the other partner gene's
  // card for the same fusion) never re-fetches.
  fusionContextByFusion: {},
  // In-flight/completed /v1/fusion-partner-evidence lookups, keyed by
  // gene|tumorType|agnostic, so re-expanding a "Check fusion partner
  // precedent" disclosure never re-fetches.
  fusionPartnerEvidenceByKey: {},
  // In-flight/completed GET /v1/genes/{gene}/openevidence lookups, keyed by
  // gene|tumorType|fusion, so re-rendering the results list never re-fetches. This
  // sidecar is fetched independently of the core annotation result and
  // never blocks rendering the rest of the page (see renderOpenEvidenceCard).
  openEvidenceByGene: {},
  // Server-side settings.openevidence_enabled, learned from GET
  // /v1/dev/status on page load. Starts false (fail closed, matching the
  // server default) so no card renders and no fetch fires before that call
  // resolves — see loadDevStatus and renderOpenEvidenceCard.
  openevidenceEnabled: false,
};

const elements = {
  annotatePanel: document.querySelector("#annotate-panel"),
  benchmarkJudge: document.querySelector("#benchmark-judge"),
  benchmarkLocalBackend: document.querySelector("#benchmark-local-backend"),
  benchmarkMaxGenes: document.querySelector("#benchmark-max-genes"),
  benchmarkPanel: document.querySelector("#benchmark-panel"),
  clearResults: document.querySelector("#clear-results"),
  exportButton: document.querySelector("#export-button"),
  exportFormat: document.querySelector("#export-format"),
  shareRun: document.querySelector("#share-run"),
  appShell: document.querySelector(".app-shell"),
  geneIndex: document.querySelector("#gene-index"),
  openFeedback: document.querySelector("#open-feedback"),
  closeFeedback: document.querySelector("#close-feedback"),
  feedbackModal: document.querySelector("#feedback-modal"),
  feedbackRunContext: document.querySelector("#feedback-run-context"),
  feedbackCategory: document.querySelector("#feedback-category"),
  feedbackGeneField: document.querySelector("#feedback-gene-field"),
  feedbackGene: document.querySelector("#feedback-gene"),
  feedbackMessage: document.querySelector("#feedback-message"),
  feedbackEmail: document.querySelector("#feedback-email"),
  feedbackOutput: document.querySelector("#feedback-output"),
  submitFeedback: document.querySelector("#submit-feedback"),
  messageBox: document.querySelector("#message-box"),
  navAnnotate: document.querySelector("#nav-annotate"),
  navBenchmark: document.querySelector("#nav-benchmark"),
  resultsWindow: document.querySelector("#results-window"),
  resultsViewTabs: document.querySelector("#results-view-tabs"),
  tabResultsView: document.querySelector("#tab-results-view"),
  tabFusionEvidenceView: document.querySelector("#tab-fusion-evidence-view"),
  tabNoResultView: document.querySelector("#tab-noresult-view"),
  runButton: document.querySelector("#run-button"),
  runBenchmarkButton: document.querySelector("#run-benchmark-button"),
  runSummary: document.querySelector("#run-summary"),
  sidebarResizer: document.querySelector("#sidebar-resizer"),
  workspaceTitle: document.querySelector("#workspace-title"),
  // auth elements
  userAuthBar: document.querySelector("#user-auth-bar"),
  userProfile: document.querySelector("#user-profile"),
  userAvatar: document.querySelector("#user-avatar"),
  userName: document.querySelector("#user-name"),
  userRole: document.querySelector("#user-role"),
  userEmail: document.querySelector("#user-email"),
  signoutBtn: document.querySelector("#signout-btn"),
  userSignin: document.querySelector("#user-signin"),
  headerSigninBtn: document.querySelector("#header-signin-btn"),
  authGateModal: document.querySelector("#auth-gate-modal"),
  modalGoogleSigninBtn: document.querySelector("#modal-google-signin-btn"),
  modalSamlSigninBtn: document.querySelector("#modal-saml-signin-btn"),
  // mode tabs
  tabSingle: document.querySelector("#tab-single"),
  tabBatch: document.querySelector("#tab-batch"),
  singleMode: document.querySelector("#single-mode"),
  batchMode: document.querySelector("#batch-mode"),
  // single form
  singleFusion: document.querySelector("#single-fusion"),
  singleTumorType: document.querySelector("#single-tumor-type"),
  singleFiveExon: document.querySelector("#single-five-exon"),
  singleThreeExon: document.querySelector("#single-three-exon"),
  singleFiveGenomic: document.querySelector("#single-five-genomic"),
  singleThreeGenomic: document.querySelector("#single-three-genomic"),
  singleFiveTranscript: document.querySelector("#single-five-transcript"),
  singleThreeTranscript: document.querySelector("#single-three-transcript"),
  addFusionBtn: document.querySelector("#add-fusion-btn"),
  fusionQueue: document.querySelector("#fusion-queue"),
  queueCount: document.querySelector("#queue-count"),
  clearQueueBtn: document.querySelector("#clear-queue-btn"),
  queueList: document.querySelector("#queue-list"),
  // batch grid
  batchGridBody: document.querySelector("#batch-grid-body"),
  addRowBtn: document.querySelector("#add-row-btn"),
  batchHint: document.querySelector("#batch-hint"),
  batchUploadBtn: document.querySelector("#batch-upload-btn"),
  batchUploadInput: document.querySelector("#batch-upload-input"),
  // dev-mode annotation backend
  annotateBackendField: document.querySelector("#annotate-backend-field"),
  annotateLocalBackend: document.querySelector("#annotate-local-backend"),
  annotateMode: document.querySelector("#annotate-mode"),
  skipLiteratureOncokb: document.querySelector("#skip-literature-oncokb"),
};

const statusFields = [
  ["cancer_associated", "Cancer associated"],
  ["in_oncokb", "In OncoKB"],
];

const lazyAnnotationFields = new Set([
  "cancer_type_prevalence",
  "gene_class",
  "signaling_pathways",
]);

const editableFields = [
  ["cancer_association_rationale", "Rationale", "long"],
  ["gene_summary", "Gene summary", "long"],
  ["cancer_type_prevalence", "Cancer type prevalence", "long"],
  ["gene_class", "Gene class", "text"],
  ["signaling_pathways", "Signaling pathways", "long"],
  ["error", "Error", "text"],
];

// ---------------------------------------------------------------------------
// Sidebar resizing
// ---------------------------------------------------------------------------

function sidebarMaxWidth() {
  if (window.innerWidth <= 900) return SIDEBAR_DEFAULT_WIDTH;
  return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, window.innerWidth - 360));
}

function clampSidebarWidth(width) {
  return Math.min(sidebarMaxWidth(), Math.max(SIDEBAR_MIN_WIDTH, Math.round(width)));
}

function setSidebarWidth(width) {
  elements.appShell.style.setProperty("--sidebar-width", `${clampSidebarWidth(width)}px`);
}

function resetSidebarWidth() {
  elements.appShell.style.removeProperty("--sidebar-width");
}

function initSidebarResize() {
  const handle = elements.sidebarResizer;
  if (!handle) return;

  let startX = 0;
  let startWidth = SIDEBAR_DEFAULT_WIDTH;

  handle.addEventListener("pointerdown", (event) => {
    if (window.innerWidth <= 900) return;
    event.preventDefault();
    startX = event.clientX;
    startWidth = document.querySelector(".sidebar").getBoundingClientRect().width;
    document.body.classList.add("sidebar-resizing");
    handle.setPointerCapture(event.pointerId);
  });

  handle.addEventListener("pointermove", (event) => {
    if (!document.body.classList.contains("sidebar-resizing")) return;
    setSidebarWidth(startWidth + event.clientX - startX);
  });

  function stopResize(event) {
    if (!document.body.classList.contains("sidebar-resizing")) return;
    document.body.classList.remove("sidebar-resizing");
    if (handle.hasPointerCapture(event.pointerId)) {
      handle.releasePointerCapture(event.pointerId);
    }
  }

  handle.addEventListener("pointerup", stopResize);
  handle.addEventListener("pointercancel", stopResize);
  handle.addEventListener("dblclick", resetSidebarWidth);
  handle.addEventListener("keydown", (event) => {
    if (event.key === "Home") {
      event.preventDefault();
      resetSidebarWidth();
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const delta = event.key === "ArrowRight" ? 24 : -24;
    const currentWidth = document.querySelector(".sidebar").getBoundingClientRect().width;
    setSidebarWidth(currentWidth + delta);
  });

  window.addEventListener("resize", () => {
    const inlineWidth = elements.appShell.style.getPropertyValue("--sidebar-width");
    if (!inlineWidth) return;
    setSidebarWidth(parseFloat(inlineWidth));
  });
}

// ---------------------------------------------------------------------------
// Authentication & SSO status
// ---------------------------------------------------------------------------

async function checkAuth() {
  try {
    const response = await fetch("/auth/me");
    if (!response.ok) return;
    const payload = await response.json();
    state.auth = payload;

    if (!payload.auth_enabled) {
      if (elements.userAuthBar) elements.userAuthBar.classList.add("hidden");
      if (elements.authGateModal) elements.authGateModal.classList.add("hidden");
      return;
    }

    if (elements.userAuthBar) elements.userAuthBar.classList.remove("hidden");

    const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
    const loginPageHref = `/login?redirect_to=${returnUrl}`;
    if (elements.headerSigninBtn) elements.headerSigninBtn.href = loginPageHref;

    if (payload.authenticated && payload.user) {
      if (elements.userProfile) elements.userProfile.classList.remove("hidden");
      if (elements.userSignin) elements.userSignin.classList.add("hidden");
      if (elements.authGateModal) elements.authGateModal.classList.add("hidden");

      if (elements.userName) elements.userName.textContent = payload.user.name || payload.user.email;
      if (elements.userRole) {
        const role = payload.user.role || "curator";
        elements.userRole.textContent = role;
        elements.userRole.className = `user-role-badge role-${role.toLowerCase()}`;
      }
      if (elements.userEmail) elements.userEmail.textContent = payload.user.email;

      if (elements.userAvatar) {
        if (payload.user.picture) {
          elements.userAvatar.innerHTML = `<img src="${payload.user.picture}" alt="" />`;
        } else {
          const initial = (payload.user.name || payload.user.email || "?").charAt(0).toUpperCase();
          elements.userAvatar.textContent = initial;
        }
      }

      if (elements.signoutBtn) {
        elements.signoutBtn.href = "/auth/logout";
      }
    } else {
      if (elements.userProfile) elements.userProfile.classList.add("hidden");
      if (elements.userSignin) elements.userSignin.classList.remove("hidden");
      if (elements.authGateModal) elements.authGateModal.classList.add("hidden");
      window.location.href = loginPageHref;
    }
  } catch (err) {
    console.warn("Auth status check failed", err);
  }
}

// ---------------------------------------------------------------------------
// App section switching
// ---------------------------------------------------------------------------

async function loadDevStatus() {
  try {
    const response = await fetch("/v1/dev/status");
    if (!response.ok) {
      elements.navBenchmark.classList.add("hidden");
      elements.annotateBackendField.classList.add("hidden");
      state.openevidenceEnabled = false;
      if (state.currentView === "benchmark") switchView("annotate");
      return;
    }
    const payload = await response.json();
    elements.navBenchmark.classList.toggle("hidden", !payload.enabled);
    elements.annotateBackendField.classList.toggle("hidden", !payload.enabled);
    const openevidenceEnabled = Boolean(payload.openevidence_enabled);
    state.openevidenceEnabled = openevidenceEnabled;
    // Startup fires loadDevStatus() and loadSharedRun() concurrently (see
    // bottom of file). If a shared run's results render first, they render
    // with the fail-closed default (openevidenceEnabled: false) and get no
    // OpenEvidence cards at all — nothing else re-triggers rendering for
    // those genes afterward. Re-render once here so a flag that resolves to
    // true after the fact still reconciles against whatever's already on
    // the page, instead of the sidecar silently never appearing for that
    // page load.
    if (openevidenceEnabled && state.currentResult) {
      renderAnnotationResult(state.currentResult);
    }
    if (!payload.enabled && state.currentView === "benchmark") switchView("annotate");
  } catch {
    elements.navBenchmark.classList.add("hidden");
    elements.annotateBackendField.classList.add("hidden");
    state.openevidenceEnabled = false;
    if (state.currentView === "benchmark") switchView("annotate");
  }
}

function switchView(view) {
  state.currentView = view;
  const isBenchmark = view === "benchmark";

  elements.navAnnotate.classList.toggle("active", !isBenchmark);
  elements.navBenchmark.classList.toggle("active", isBenchmark);
  elements.annotatePanel.classList.toggle("hidden", isBenchmark);
  elements.benchmarkPanel.classList.toggle("hidden", !isBenchmark);
  elements.workspaceTitle.textContent = isBenchmark ? "Benchmark" : "Results";
  elements.clearResults.classList.toggle("hidden", isBenchmark);
  elements.shareRun.classList.toggle("hidden", isBenchmark);
  elements.clearResults.disabled = isBenchmark || !state.currentResult;
  elements.shareRun.disabled = isBenchmark || !state.currentResult?.run_id;
  updateExportState();
  // The results panel is only hidden, so its cards stay connected — stop
  // their OpenEvidence polling explicitly. Coming back re-renders them.
  if (isBenchmark) cancelOpenEvidenceCardLoads();

  if (isBenchmark) {
    renderBenchmarkResult(state.currentBenchmark);
  } else if (state.currentResult) {
    renderAnnotationResult(state.currentResult);
  } else {
    renderEmptyState("No results yet", "Enter genes or fusions, then click Run to annotate.");
    elements.runSummary.textContent = "Run annotations to populate this review area.";
  }
}

// ---------------------------------------------------------------------------
// Input mode switching
// ---------------------------------------------------------------------------

function switchMode(mode) {
  state.inputMode = mode;
  elements.tabSingle.classList.toggle("active", mode === "single");
  elements.tabBatch.classList.toggle("active", mode === "batch");
  elements.singleMode.classList.toggle("hidden", mode !== "single");
  elements.batchMode.classList.toggle("hidden", mode !== "batch");
  document.body.classList.toggle("batch-active", mode === "batch");
}

// ---------------------------------------------------------------------------
// Single entry queue
// ---------------------------------------------------------------------------

function buildSingleItem() {
  const fusion = elements.singleFusion.value.trim();
  if (!fusion) return null;
  const item = { fusion };
  const tumorType = elements.singleTumorType.value.trim();
  if (tumorType) item.tumor_type = tumorType;
  const fiveExon = parseInt(elements.singleFiveExon.value, 10);
  if (!isNaN(fiveExon)) item.five_exon = fiveExon;
  const threeExon = parseInt(elements.singleThreeExon.value, 10);
  if (!isNaN(threeExon)) item.three_exon = threeExon;
  const fiveGenomic = elements.singleFiveGenomic.value.trim();
  if (fiveGenomic) item.five_genomic = fiveGenomic;
  const threeGenomic = elements.singleThreeGenomic.value.trim();
  if (threeGenomic) item.three_genomic = threeGenomic;
  const fiveTranscript = elements.singleFiveTranscript.value.trim();
  if (fiveTranscript) item.five_transcript = fiveTranscript;
  const threeTranscript = elements.singleThreeTranscript.value.trim();
  if (threeTranscript) item.three_transcript = threeTranscript;
  return item;
}

function addToQueue() {
  const item = buildSingleItem();
  if (!item) {
    setMessage("Enter a gene or fusion before adding to queue.", "error");
    return;
  }
  clearMessage();
  state.queue.push(item);
  clearSingleForm();
  renderQueue();
}

function clearSingleForm() {
  elements.singleFusion.value = "";
  elements.singleTumorType.value = "";
  elements.singleFiveExon.value = "";
  elements.singleThreeExon.value = "";
  elements.singleFiveGenomic.value = "";
  elements.singleThreeGenomic.value = "";
  elements.singleFiveTranscript.value = "";
  elements.singleThreeTranscript.value = "";
  elements.singleFusion.focus();
}

function removeFromQueue(index) {
  state.queue.splice(index, 1);
  renderQueue();
}

function clearQueue() {
  state.queue = [];
  renderQueue();
}

function clearResults() {
  state.currentResult = null;
  state.resultsViewMode = "results";
  state.fusionEvidenceStatus = null;
  updateExportState();
  elements.clearResults.disabled = true;
  elements.shareRun.disabled = true;
  elements.resultsViewTabs.classList.add("hidden");
  elements.tabFusionEvidenceView.classList.add("hidden");
  rebuildGeneIndex([]);
  renderEmptyState("No results yet", "Enter genes or fusions, then click Run to annotate.");
  clearMessage();
}

function populateSingleForm(example) {
  elements.singleFusion.value = example.fusion || "";
  elements.singleTumorType.value = example.tumor_type || "";
  elements.singleFiveExon.value = example.five_exon || "";
  elements.singleThreeExon.value = example.three_exon || "";
  elements.singleFiveGenomic.value = example.five_genomic || "";
  elements.singleThreeGenomic.value = example.three_genomic || "";
  elements.singleFiveTranscript.value = example.five_transcript || "";
  elements.singleThreeTranscript.value = example.three_transcript || "";
  elements.singleFusion.focus();
}

function nextAvailableBatchRowIndex() {
  const emptyIndex = state.batchRows.findIndex((row) =>
    GRID_COLUMNS.every((col) => !String(row[col.key] || "").trim())
  );
  if (emptyIndex !== -1) return emptyIndex;
  state.batchRows.push(emptyRow());
  return state.batchRows.length - 1;
}

function populateBatchRow(example) {
  const rowIndex = nextAvailableBatchRowIndex();
  const nextRow = emptyRow();
  GRID_COLUMNS.forEach((col) => {
    if (example[col.key] !== undefined) {
      nextRow[col.key] = String(example[col.key]);
    }
  });
  state.batchRows[rowIndex] = nextRow;
  renderGrid({ row: rowIndex, col: 0 });
}

function applyInputExample(exampleId, mode) {
  const example = INPUT_EXAMPLES[exampleId];
  if (!example) return;
  if (mode === "batch") {
    populateBatchRow(example);
    return;
  }
  populateSingleForm(example);
}

function renderQueue() {
  const count = state.queue.length;
  elements.fusionQueue.classList.toggle("hidden", count === 0);
  elements.queueCount.textContent = `${count} input${count === 1 ? "" : "s"} queued`;
  elements.queueList.replaceChildren();
  state.queue.forEach((item, i) => {
    const meta = [
      item.tumor_type || null,
      (item.five_exon !== undefined || item.three_exon !== undefined) ? "exon" : null,
      (item.five_genomic || item.three_genomic) ? "genomic" : null,
      (item.five_transcript || item.three_transcript) ? "transcript" : null,
    ].filter(Boolean);
    const li = document.createElement("li");
    li.className = "queue-item";
    li.innerHTML = `
      <span class="queue-fusion">${escapeHtml(item.fusion)}</span>
      ${meta.length ? `<span class="queue-meta">${escapeHtml(meta.join(" · "))}</span>` : ""}
      <button class="queue-remove" type="button" aria-label="Remove">×</button>
    `;
    li.querySelector(".queue-remove").addEventListener("click", () => removeFromQueue(i));
    elements.queueList.appendChild(li);
  });
}

// ---------------------------------------------------------------------------
// Batch spreadsheet grid
// ---------------------------------------------------------------------------

function renderGrid(focusAfter = null) {
  const focused = focusAfter ? null : document.activeElement;
  const focusRow = focusAfter ? String(focusAfter.row) : focused?.dataset?.row;
  const focusCol = focusAfter ? String(focusAfter.col) : focused?.dataset?.col;

  elements.batchGridBody.innerHTML = state.batchRows.map((row, rIdx) => `
    <tr>
      <td class="grid-rn">${rIdx + 1}</td>
      ${GRID_COLUMNS.map((col, cIdx) => `
        <td class="grid-cell${col.required ? " grid-cell-required" : ""}">
          <input
            type="${col.type}"
            value="${escapeAttr(row[col.key])}"
            data-row="${rIdx}"
            data-col="${cIdx}"
            data-key="${col.key}"
            ${col.type === "number" ? 'min="1"' : ""}
            placeholder="${col.required ? col.label : ""}"
          />
        </td>
      `).join("")}
      <td class="grid-del-cell">
        <button class="grid-del-btn" data-row="${rIdx}" type="button" aria-label="Remove row">×</button>
      </td>
    </tr>
  `).join("");

  if (focusRow !== undefined && focusCol !== undefined) {
    const target = elements.batchGridBody.querySelector(
      `[data-row="${focusRow}"][data-col="${focusCol}"]`
    );
    if (target) target.focus();
  }

  updateBatchHint();
}

function updateBatchHint() {
  const filled = state.batchRows.filter(r => r.fusion.trim()).length;
  elements.batchHint.textContent = filled
    ? `${filled} input${filled === 1 ? "" : "s"} ready.`
    : "";
}

function handleGridInput(event) {
  const { row, key } = event.target.dataset;
  if (row === undefined || key === undefined) return;
  state.batchRows[parseInt(row, 10)][key] = event.target.value;
  updateBatchHint();
}

function handleGridKeydown(event) {
  const { row, col } = event.target.dataset;
  if (row === undefined) return;
  const rIdx = parseInt(row, 10);
  const cIdx = parseInt(col, 10);

  if (event.key === "Enter") {
    event.preventDefault();
    const nextRow = rIdx + 1;
    if (nextRow >= state.batchRows.length) {
      state.batchRows.push(emptyRow());
      renderGrid({ row: nextRow, col: cIdx });
    } else {
      const target = elements.batchGridBody.querySelector(
        `[data-row="${nextRow}"][data-col="${cIdx}"]`
      );
      if (target) target.focus();
    }
    return;
  }

  if (event.key === "ArrowDown") {
    event.preventDefault();
    const target = elements.batchGridBody.querySelector(
      `[data-row="${rIdx + 1}"][data-col="${cIdx}"]`
    );
    if (target) target.focus();
    return;
  }

  if (event.key === "ArrowUp") {
    event.preventDefault();
    const target = elements.batchGridBody.querySelector(
      `[data-row="${rIdx - 1}"][data-col="${cIdx}"]`
    );
    if (target) target.focus();
  }
}

function handleGridPaste(event) {
  const text = (event.clipboardData || window.clipboardData).getData("text");
  if (!text) return;

  const lines = text.split(/\r?\n/).filter(Boolean);
  if (!lines.length) return;

  const hasTabs = lines[0].includes("\t");
  const isMultiCell = hasTabs || lines.length > 1;
  if (!isMultiCell) return; // let single-cell paste happen normally

  event.preventDefault();

  const sep = hasTabs ? "\t" : ",";
  const firstLineParts = lines[0].split(sep).map(v => v.trim().toLowerCase());

  // Detect header row: any part matches a known column key
  const isHeader = firstLineParts.some(v => GRID_COLUMNS.some(c => c.key === v));

  let colMap;   // paste-column index → GRID_COLUMNS key (or null)
  let dataFrom; // first line index of actual data

  if (isHeader) {
    colMap = firstLineParts.map(v => {
      const col = GRID_COLUMNS.find(c => c.key === v);
      return col ? col.key : null;
    });
    dataFrom = 1;
  } else {
    const focused = event.target;
    const startCol = focused?.dataset?.col !== undefined
      ? parseInt(focused.dataset.col, 10)
      : 0;
    colMap = GRID_COLUMNS.slice(startCol).map(c => c.key);
    dataFrom = 0;
  }

  const focused = event.target;
  const startRow = focused?.dataset?.row !== undefined
    ? parseInt(focused.dataset.row, 10)
    : 0;

  const dataLines = lines.slice(dataFrom);
  while (state.batchRows.length < startRow + dataLines.length) {
    state.batchRows.push(emptyRow());
  }

  dataLines.forEach((line, lineIdx) => {
    const cols = line.split(sep);
    const rowIdx = startRow + lineIdx;
    colMap.forEach((key, cIdx) => {
      if (!key) return;
      state.batchRows[rowIdx][key] = (cols[cIdx] || "").trim();
    });
  });

  renderGrid({ row: startRow, col: 0 });
}

function handleGridClick(event) {
  if (!event.target.classList.contains("grid-del-btn")) return;
  const rIdx = parseInt(event.target.dataset.row, 10);
  if (state.batchRows.length <= 1) {
    state.batchRows[0] = emptyRow();
    renderGrid();
    return;
  }
  state.batchRows.splice(rIdx, 1);
  renderGrid();
}

// A cell counts as a match for a GRID_COLUMNS entry if it equals that
// column's machine key (e.g. "tumor_type") OR its on-screen label (e.g.
// "Tumor Type"), letter/digit comparison only — so "5' Exon", "5′ Exon", and
// "5 Exon" all match "5′ Exon" the same way.
function normalizeHeaderCell(value) {
  return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]/g, "");
}

const HEADER_ALIASES = new Map();
GRID_COLUMNS.forEach((col) => {
  HEADER_ALIASES.set(normalizeHeaderCell(col.key), col.key);
  HEADER_ALIASES.set(normalizeHeaderCell(col.label), col.key);
});

// Real gene/fusion identifiers (ALK, EML4::ALK, HLA-A, ...) never contain
// whitespace and are never this long. A parsed value that fails this is
// almost certainly not one — most likely an unrecognized header cell that
// fell through to positional parsing (see parseBatchFileText), or a wrong
// file (binary, wrong delimiter) landing in this column. Caught here so it
// doesn't get silently queued as a "gene" to annotate.
const PLAUSIBLE_FUSION_PATTERN = /^\S{1,120}$/;

// Parses uploaded TSV/CSV file text into grid rows. Tab-delimited when the
// first line contains a tab, else comma. A header row is recognized when at
// least one of its cells matches a known column (see HEADER_ALIASES); its
// unrecognized cells are reported back as ignoredColumns so a superset of
// fields degrades to "we only used the ones we needed" instead of silent
// misalignment. With no recognized header, columns are read positionally in
// the same order as the visible grid — the same fallback handleGridPaste
// uses, so a genuinely headerless file still loads.
function parseBatchFileText(text) {
  const lines = text.split(/\r?\n/).filter((line) => line.trim().length);
  if (!lines.length) {
    return { rows: [], headerDetected: false, ignoredColumns: [], missingRequiredColumn: false };
  }

  const hasTabs = lines[0].includes("\t");
  const sep = hasTabs ? "\t" : ",";
  const firstLineCells = lines[0].split(sep).map((v) => v.trim());
  const firstLineKeys = firstLineCells.map((v) => HEADER_ALIASES.get(normalizeHeaderCell(v)) || null);
  const headerDetected = firstLineKeys.some(Boolean);

  let colMap;
  let dataFrom;
  let ignoredColumns = [];
  if (headerDetected) {
    colMap = firstLineKeys;
    ignoredColumns = firstLineCells.filter((_, cIdx) => !firstLineKeys[cIdx]);
    dataFrom = 1;
  } else {
    colMap = GRID_COLUMNS.map((c) => c.key);
    dataFrom = 0;
  }

  const requiredKey = GRID_COLUMNS.find((c) => c.required).key;
  const missingRequiredColumn = headerDetected && !colMap.includes(requiredKey);

  const rows = lines.slice(dataFrom).map((line) => {
    const cols = line.split(sep);
    const row = emptyRow();
    colMap.forEach((key, cIdx) => {
      if (!key) return;
      row[key] = (cols[cIdx] || "").trim();
    });
    if (row.fusion && !PLAUSIBLE_FUSION_PATTERN.test(row.fusion)) row.fusion = "";
    return row;
  });

  return { rows, headerDetected, ignoredColumns, missingRequiredColumn };
}

function handleBatchFileUpload(event) {
  const file = event.target.files?.[0];
  event.target.value = ""; // allow re-selecting the same file later
  if (!file) return;

  const reader = new FileReader();
  reader.onload = () => {
    const { rows, headerDetected, ignoredColumns, missingRequiredColumn } =
      parseBatchFileText(String(reader.result || ""));
    const requiredLabel = GRID_COLUMNS.find((c) => c.required).label;

    if (missingRequiredColumn) {
      elements.batchHint.textContent =
        `${file.name}: no "${requiredLabel}" column found in the header — nothing loaded. ` +
        `Rename that column to "${requiredLabel}" (or "fusion") and re-upload.`;
      return;
    }

    const withFusion = rows.filter((row) => row.fusion.trim());
    if (!withFusion.length) {
      elements.batchHint.textContent = headerDetected
        ? `${file.name}: header recognized, but no row had a "${requiredLabel}" value.`
        : `No valid rows found in ${file.name}. If your columns aren't in the form's ` +
          `default order, add a header row naming them (e.g. "fusion, tumor_type").`;
      return;
    }

    state.batchRows = rows;
    renderGrid();

    const notes = [`Loaded ${withFusion.length} input${withFusion.length === 1 ? "" : "s"} from ${file.name}.`];
    if (!headerDetected) {
      notes.push(`No header row recognized — columns were read positionally, matching the grid's column order.`);
    }
    if (ignoredColumns.length) {
      notes.push(`Ignored unrecognized column${ignoredColumns.length === 1 ? "" : "s"}: ${ignoredColumns.join(", ")}.`);
    }
    const droppedCount = rows.length - withFusion.length;
    if (droppedCount > 0) {
      notes.push(`Skipped ${droppedCount} row${droppedCount === 1 ? "" : "s"} missing a valid "${requiredLabel}" value.`);
    }
    elements.batchHint.textContent = notes.join(" ");
  };
  reader.onerror = () => {
    elements.batchHint.textContent = `Could not read ${file.name}.`;
  };
  reader.readAsText(file);
}

function getGridData() {
  return state.batchRows
    .map(row => {
      const obj = {};
      GRID_COLUMNS.forEach(col => {
        const val = (row[col.key] || "").trim();
        if (!val) return;
        if (col.type === "number") {
          const n = parseInt(val, 10);
          if (!isNaN(n)) obj[col.key] = n;
        } else {
          obj[col.key] = val;
        }
      });
      return obj;
    })
    .filter(obj => Boolean(obj.fusion));
}

// ---------------------------------------------------------------------------
// Unified input parsing
// ---------------------------------------------------------------------------

function parseInputs() {
  if (state.inputMode === "batch") {
    return getGridData();
  }
  const pendingItem = buildSingleItem();
  if (pendingItem) {
    state.queue.push(pendingItem);
    clearSingleForm();
    renderQueue();
  }
  return [...state.queue];
}

// ---------------------------------------------------------------------------
// UI state helpers
// ---------------------------------------------------------------------------

function setMessage(message, type = "info") {
  elements.messageBox.textContent = message;
  elements.messageBox.className = `status-box ${type}`;
}

function clearMessage() {
  elements.messageBox.textContent = "";
  elements.messageBox.className = "status-box hidden";
}

function setRunning(isRunning) {
  state.isRunning = isRunning;
  elements.runButton.textContent = isRunning ? "Running..." : "Run";
  elements.runButton.disabled = isRunning;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function updateExportState({ annotationExportDisabled = false } = {}) {
  const isBenchmark = state.currentView === "benchmark";
  const hasAnnotationExport = Boolean(state.currentResult?.annotations?.length);
  const hasBenchmarkExport = Boolean(state.currentBenchmark);
  const csvOption = elements.exportFormat.querySelector('option[value="csv"]');

  csvOption.disabled = isBenchmark;
  if (isBenchmark && elements.exportFormat.value === "csv") {
    elements.exportFormat.value = "json";
  }

  elements.exportButton.disabled = isBenchmark
    ? !hasBenchmarkExport
    : annotationExportDisabled || !hasAnnotationExport;
}

function renderLoadingState(message) {
  const wrap = document.createElement("div");
  wrap.className = "inline-loading";
  wrap.setAttribute("role", "status");

  const spinner = document.createElement("span");
  spinner.className = "inline-spinner";
  spinner.setAttribute("aria-hidden", "true");
  wrap.appendChild(spinner);

  const text = document.createElement("span");
  text.textContent = message;
  wrap.appendChild(text);

  return wrap;
}

function setBenchmarkRunning(isRunning) {
  state.isBenchmarkRunning = isRunning;
  elements.runBenchmarkButton.textContent = isRunning ? "Running..." : "Run Benchmark";
  elements.runBenchmarkButton.disabled = isRunning;
}

// ---------------------------------------------------------------------------
// Annotation run
// ---------------------------------------------------------------------------

async function runAnnotation() {
  const annotationInputs = parseInputs();
  if (!annotationInputs.length) {
    setMessage(
      state.inputMode === "single"
        ? "Add at least one gene or fusion to the queue before running."
        : "Enter at least one gene or fusion in the Gene/Fusion column before running.",
      "error"
    );
    return;
  }

  // Collapse the expanded batch sidebar so results have room
  document.body.classList.remove("batch-active");

  annotationInputs.forEach((item) => {
    if (item.fusion) state.lastInputsByFusion[item.fusion] = item;
  });

  setRunning(true);
  updateExportState({ annotationExportDisabled: true });
  clearMessage();
  setMessage(`Submitting ${annotationInputs.length} input${annotationInputs.length === 1 ? "" : "s"} for annotation...`, "info");

  try {
    const localBackend = elements.annotateLocalBackend.value || undefined;
    const body = { fusions: annotationInputs, mode: elements.annotateMode.value || "full" };
    if (localBackend) body.local_backend = localBackend;
    if (elements.skipLiteratureOncokb.checked) {
      body.skip_literature_for_oncokb = true;
    }
    const response = await fetch("/v1/annotate/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      let detail = response.statusText;
      const text = await response.text();
      try {
        const payload = JSON.parse(text);
        detail = payload.detail || detail;
      } catch {
        detail = text || detail;
      }
      throw new Error(detail || "Request failed");
    }

    const job = await response.json();
    await pollAnnotationJob(job.status_url);
    startFusionEvidenceJob(annotationInputs, state.currentResult?.run_id).catch((error) => {
      state.fusionEvidenceStatus = "failed";
      setMessage(formatRunError(error.message), "error");
      if (state.currentResult) renderAnnotationResult(state.currentResult);
    });
    setMessage("Annotation complete. Review fields before exporting.", "info");
  } catch (error) {
    setMessage(formatRunError(error.message), "error");
  } finally {
    setRunning(false);
  }
}

async function pollAnnotationJob(statusUrl) {
  while (true) {
    const response = await fetch(statusUrl);
    if (!response.ok) {
      throw new Error(response.statusText || "Unable to load annotation job status");
    }
    const status = await response.json();
    if (status.status === "failed") {
      throw new Error(status.error || "Annotation job failed");
    }
    // Progress renders carry the job_id as run_id; the completed result has
    // the real run_id. Adopt it as the same OpenEvidence run before the
    // completed result is first rendered, so its timed-out/failed keys stay.
    if (status.result?.run_id) adoptOpenEvidenceRunId(status.job_id, status.result.run_id);

    const partial = status.result || {
      run_id: status.job_id,
      timestamp: "",
      fusions_processed: status.fusions_processed,
      genes_annotated: status.genes_total || status.genes_completed,
      annotations: status.annotations || [],
      fusion_evidence: state.currentResult?.fusion_evidence || [],
      timings_ms: status.timings_ms || {},
    };
    state.currentResult = partial;
    renderAnnotationResult(partial);
    updateExportState({ annotationExportDisabled: status.status !== "complete" });
    elements.shareRun.disabled = status.status !== "complete";

    if (status.status === "complete") {
      state.currentResult = status.result;
      renderAnnotationResult(status.result);
      return status.result;
    }

    const total = status.genes_total || "?";
    setMessage(`Annotation running: ${status.genes_completed}/${total} genes complete.`, "info");
    await sleep(1500);
  }
}

function isFusionInputValue(value) {
  return /::|--|\//.test(String(value || ""));
}

function fusionEvidenceInputs(inputs) {
  const seen = new Set();
  const fusionInputs = [];
  (inputs || []).forEach((item) => {
    if (!item?.fusion || !isFusionInputValue(item.fusion)) return;
    const tumorType = String(item.tumor_type || "").trim().toLowerCase();
    const key = `${item.fusion}||${tumorType}`;
    if (seen.has(key)) return;
    seen.add(key);
    fusionInputs.push(item);
  });
  return fusionInputs;
}

async function startFusionEvidenceJob(inputs, runId) {
  const fusions = fusionEvidenceInputs(inputs);
  if (!fusions.length || !state.currentResult) return;

  state.fusionEvidenceStatus = "running";
  state.currentResult = {
    ...state.currentResult,
    fusion_evidence: state.currentResult.fusion_evidence || [],
  };
  renderAnnotationResult(state.currentResult);

  const response = await fetch("/v1/fusion-evidence/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      fusions,
      mode: elements.annotateMode.value || "full",
      run_id: runId || null,
    }),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText || "Fusion evidence job failed");
  }

  const job = await response.json();
  await pollFusionEvidenceJob(job.status_url);
}

async function pollFusionEvidenceJob(statusUrl) {
  while (true) {
    const response = await fetch(statusUrl);
    if (!response.ok) {
      throw new Error(response.statusText || "Unable to load fusion evidence job status");
    }
    const status = await response.json();
    if (status.status === "failed") {
      throw new Error(status.error || "Fusion evidence job failed");
    }

    state.currentResult = {
      ...state.currentResult,
      fusion_evidence: status.fusion_evidence || [],
    };
    renderAnnotationResult(state.currentResult);

    if (status.status === "complete") {
      state.fusionEvidenceStatus = "complete";
      renderAnnotationResult(state.currentResult);
      setMessage("Fusion evidence retrieval complete.", "info");
      return status.fusion_evidence || [];
    }

    const total = status.fusions_total || "?";
    setMessage(`Fusion evidence running: ${status.fusions_completed}/${total} fusions complete.`, "info");
    await sleep(1500);
  }
}

function mergeAnnotations(existing, enriched) {
  const byGene = new Map((existing || []).map((annotation) => [annotation.gene, annotation]));
  (enriched || []).forEach((annotation) => {
    byGene.set(annotation.gene, { ...(byGene.get(annotation.gene) || {}), ...annotation });
  });
  return Array.from(byGene.values()).sort((a, b) => a.gene.localeCompare(b.gene));
}

function canEnrichAnnotation(annotation) {
  return annotation && !annotation.insufficient_evidence && annotation.gene;
}

async function enrichAnnotationOnDemand(annotation, label = "details") {
  if (!canEnrichAnnotation(annotation)) return null;
  if (state.enrichmentByGene.has(annotation.gene)) {
    return state.enrichmentByGene.get(annotation.gene);
  }

  const promise = submitAnnotationEnrichment(annotation, label).finally(() => {
    state.enrichmentByGene.delete(annotation.gene);
  });
  state.enrichmentByGene.set(annotation.gene, promise);
  return promise;
}

async function submitAnnotationEnrichment(annotation, label) {
  const localBackend = elements.annotateLocalBackend.value || undefined;
  const body = { annotations: [annotation] };
  if (localBackend) body.local_backend = localBackend;

  const response = await fetch("/v1/annotate/enrichment/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = response.statusText;
    const text = await response.text();
    try {
      const payload = JSON.parse(text);
      detail = payload.detail || detail;
    } catch {
      detail = text || detail;
    }
    throw new Error(detail || "Enrichment request failed");
  }

  const job = await response.json();
  const enriched = await pollAnnotationEnrichmentJob(job.status_url, annotation.gene);
  setMessage(`${annotation.gene} ${label} enriched.`, "info");
  return enriched;
}

async function pollAnnotationEnrichmentJob(statusUrl, gene) {
  while (true) {
    const response = await fetch(statusUrl);
    if (!response.ok) {
      throw new Error(response.statusText || "Unable to load enrichment job status");
    }
    const status = await response.json();
    if (status.status === "failed") {
      throw new Error(status.error || "Enrichment job failed");
    }

    const annotations = status.annotations || [];
    if (annotations.length) {
      state.currentResult = {
        ...state.currentResult,
        annotations: mergeAnnotations(state.currentResult?.annotations || [], annotations),
      };
    }

    if (status.status === "complete") {
      renderAnnotationResult(state.currentResult);
      return annotations.find((annotation) => annotation.gene === gene) || annotations[0] || null;
    }

    setMessage(`Loading ${gene} enrichment...`, "info");
    await sleep(1500);
  }
}

function formatRunError(message) {
  if (String(message).includes("ONCOKB_API_TOKEN")) {
    return "OncoKB API token is required. Open Settings and configure your OncoKB token.";
  }
  return message;
}

// ---------------------------------------------------------------------------
// Sharing a run by link
// ---------------------------------------------------------------------------

async function copyShareLink() {
  const runId = state.currentResult?.run_id;
  if (!runId) return;

  const url = `${location.origin}${location.pathname}?run=${encodeURIComponent(runId)}`;
  try {
    await navigator.clipboard.writeText(url);
    setMessage(
      "Share link copied. It only works for peers who can reach the same Backend URL as you.",
      "info",
    );
  } catch {
    setMessage(`Couldn't copy automatically — copy this link: ${url}`, "error");
  }
}

// Every page load starts fresh (no auto-restore of a previous run) except
// for the one explicit case a curator asked for: opening a share link.
// Without this, a shared production URL would show whichever run last ran
// on that browser — possibly someone else's, possibly days old — instead
// of a clean landing page.
async function loadSharedRun() {
  const sharedRunId = new URLSearchParams(location.search).get("run");
  if (!sharedRunId) return;

  try {
    const response = await fetch(`/v1/annotate/${encodeURIComponent(sharedRunId)}`);
    if (!response.ok) {
      throw new Error(
        response.status === 404
          ? "This shared run wasn't found on the configured Backend URL."
          : response.statusText,
      );
    }
    const result = await response.json();
    // A run's fusion evidence job runs after the core annotation completes, so
    // it's normally persisted onto the run by the time anyone opens a share
    // link. Mark it complete up front so the tab shows immediately instead of
    // staying hidden (fusionEvidenceStatus otherwise defaults to not-running).
    if (result.fusion_evidence?.length) {
      state.fusionEvidenceStatus = "complete";
    }
    state.currentResult = result;
    renderAnnotationResult(result);
    setMessage("Shared run restored.", "info");
  } catch (error) {
    setMessage(formatRunError(error.message), "error");
  }
}

// ---------------------------------------------------------------------------
// Benchmark run
// ---------------------------------------------------------------------------

async function runBenchmark() {
  const maxGenes = parseInt(elements.benchmarkMaxGenes.value, 10);
  const localBackend = elements.benchmarkLocalBackend.value;
  const payload = {
    no_judge: elements.benchmarkJudge.value !== "run",
  };
  if (!isNaN(maxGenes)) payload.max_genes = maxGenes;
  if (localBackend) payload.local_backend = localBackend;

  setBenchmarkRunning(true);
  clearMessage();
  setMessage("Running benchmark...", "info");

  try {
    const response = await fetch("/v1/dev/benchmark", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      let detail = response.statusText;
      const text = await response.text();
      try {
        const errorPayload = JSON.parse(text);
        detail = errorPayload.detail || detail;
      } catch {
        detail = text || detail;
      }
      throw new Error(detail || "Benchmark failed");
    }

    const result = await response.json();
    state.currentBenchmark = result;
    renderBenchmarkResult(result);
    setMessage("Benchmark complete.", "info");
  } catch (error) {
    setMessage(error.message, "error");
  } finally {
    setBenchmarkRunning(false);
  }
}

// ---------------------------------------------------------------------------
// Gene index sidebar
// ---------------------------------------------------------------------------

let _geneObserver = null;

function rebuildGeneIndex(annotations) {
  elements.geneIndex.replaceChildren();
  if (!annotations || !annotations.length) return;

  annotations.forEach((annotation) => {
    const btn = document.createElement("button");
    btn.className = "gene-index-item";
    btn.textContent = annotation.gene;
    btn.type = "button";
    btn.addEventListener("click", () => {
      const card = document.getElementById(`gene-${annotation.gene}`);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    elements.geneIndex.appendChild(btn);
  });

  observeGeneCards();
}

function observeGeneCards() {
  if (_geneObserver) _geneObserver.disconnect();
  const cards = elements.resultsWindow.querySelectorAll(".annotation-card[id]");
  if (!cards.length) return;

  _geneObserver = new IntersectionObserver(
    (entries) => {
      const visible = entries.filter((e) => e.isIntersecting);
      if (!visible.length) return;
      const topEntry = visible.reduce((a, b) =>
        a.boundingClientRect.top < b.boundingClientRect.top ? a : b,
      );
      const gene = topEntry.target.id.replace("gene-", "");
      elements.geneIndex.querySelectorAll(".gene-index-item").forEach((btn) => {
        btn.classList.toggle("active", btn.textContent === gene);
      });
    },
    { root: elements.resultsWindow, threshold: 0.15 },
  );

  cards.forEach((card) => _geneObserver.observe(card));
}

// ---------------------------------------------------------------------------
// Results rendering
// ---------------------------------------------------------------------------

function renderEmptyState(title, body) {
  cancelOpenEvidenceCardLoads({ abort: false });
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <p>${escapeHtml(body)}</p>
  `;
  elements.resultsWindow.replaceChildren(empty);
  rebuildGeneIndex([]);
}

// ---------------------------------------------------------------------------
// Feedback
// ---------------------------------------------------------------------------

function setInstallOutput(title, body, type = "info") {
  elements.feedbackOutput.className = "install-output";
  elements.feedbackOutput.innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <p>${escapeHtml(body)}</p>
  `;
  elements.feedbackOutput.classList.toggle("error", type === "error");
  elements.feedbackOutput.classList.remove("hidden");
}

function showFeedbackModal() {
  elements.feedbackOutput.classList.add("hidden");
  elements.feedbackMessage.value = "";
  elements.feedbackCategory.value = "bug";

  const result = state.currentResult;
  const genes = (result?.annotations || []).map((a) => a.gene).filter(Boolean);

  if (result?.run_id) {
    elements.feedbackRunContext.textContent =
      `Attached: run ${result.run_id.slice(0, 8)} (${genes.length} gene${genes.length === 1 ? "" : "s"})`;
    elements.feedbackRunContext.classList.remove("hidden");
  } else {
    elements.feedbackRunContext.classList.add("hidden");
  }

  if (genes.length) {
    elements.feedbackGene.innerHTML = '<option value="">Not specific to a gene</option>';
    genes.forEach((gene) => {
      const option = document.createElement("option");
      option.value = gene;
      option.textContent = gene;
      elements.feedbackGene.appendChild(option);
    });
    elements.feedbackGeneField.classList.remove("hidden");
  } else {
    elements.feedbackGeneField.classList.add("hidden");
  }

  elements.feedbackModal.classList.remove("hidden");
}

function hideFeedbackModal() {
  elements.feedbackModal.classList.add("hidden");
}

function githubIssueUrl(title, body) {
  const url = new URL("https://github.com/oncokb/agentic-cancer-gene-classification/issues/new");
  url.searchParams.set("title", title);
  url.searchParams.set("body", body);
  return url.toString();
}

async function submitFeedbackForm() {
  const message = elements.feedbackMessage.value.trim();
  if (!message) {
    setInstallOutput("Message required", "Describe what's going on before submitting.", "error");
    return;
  }

  elements.submitFeedback.disabled = true;
  try {
    const response = await fetch("/v1/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        category: elements.feedbackCategory.value,
        message,
        contact_email: elements.feedbackEmail.value.trim() || null,
        run_id: state.currentResult?.run_id || null,
        gene: elements.feedbackGene?.value || null,
        page_url: window.location.href,
      }),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || "Failed to submit feedback");
    }
    const payload = await response.json();
    if (payload.issue_url) {
      setInstallOutput("Thanks!", `Your feedback was filed as a GitHub issue: ${payload.issue_url}`);
    } else if (payload.issue_title && payload.issue_body) {
      window.open(githubIssueUrl(payload.issue_title, payload.issue_body), "_blank", "noopener");
      setInstallOutput("Thanks!", "Your feedback was stored and a GitHub issue draft was opened.");
    } else {
      setInstallOutput("Thanks!", "Your feedback was submitted.");
    }
    elements.feedbackMessage.value = "";
    elements.feedbackEmail.value = "";
  } catch (error) {
    setInstallOutput("Couldn't submit feedback", error.message, "error");
  } finally {
    elements.submitFeedback.disabled = false;
  }
}

function switchResultsView(mode) {
  if (state.resultsViewMode === mode || !state.currentResult) return;
  state.resultsViewMode = mode;
  elements.tabResultsView.classList.toggle("active", mode === "results");
  elements.tabFusionEvidenceView.classList.toggle("active", mode === "fusion");
  elements.tabNoResultView.classList.toggle("active", mode === "noresult");

  const allAnnotations = state.currentResult.annotations || [];
  const visibleAnnotations = allAnnotations.filter((a) => !a.insufficient_evidence);
  const hiddenAnnotations = allAnnotations.filter((a) => a.insufficient_evidence);
  applyResultsViewMode(visibleAnnotations, hiddenAnnotations, state.currentResult.fusion_evidence || []);
}

function renderAnnotationResult(result) {
  syncOpenEvidenceRun(result.run_id);
  const allAnnotations = result.annotations || [];
  const visibleAnnotations = allAnnotations.filter((a) => !a.insufficient_evidence);
  const hiddenAnnotations = allAnnotations.filter((a) => a.insufficient_evidence);
  const fusionEvidence = result.fusion_evidence || [];
  const showFusionTab = fusionEvidence.length > 0 || state.fusionEvidenceStatus === "running";
  updateExportState();
  elements.clearResults.disabled = false;
  elements.shareRun.disabled = !result.run_id;

  const total = result.genes_annotated;
  const inputCount = result.fusions_processed || 0;
  elements.runSummary.textContent =
    `${total} gene${total === 1 ? "" : "s"} annotated from ` +
    `${inputCount} input${inputCount === 1 ? "" : "s"}.`;

  elements.tabResultsView.textContent = `Results (${visibleAnnotations.length})`;
  elements.tabFusionEvidenceView.textContent =
    state.fusionEvidenceStatus === "running"
      ? `Fusion evidence (${fusionEvidence.length}...)`
      : `Fusion evidence (${fusionEvidence.length})`;
  elements.tabNoResultView.textContent = `No result (${hiddenAnnotations.length})`;
  elements.tabFusionEvidenceView.classList.toggle("hidden", !showFusionTab);
  elements.resultsViewTabs.classList.toggle("hidden", hiddenAnnotations.length === 0 && !showFusionTab);

  const validModes = new Set(["results"]);
  if (showFusionTab) validModes.add("fusion");
  if (hiddenAnnotations.length) validModes.add("noresult");
  if (!validModes.has(state.resultsViewMode)) {
    state.resultsViewMode = visibleAnnotations.length > 0
      ? "results"
      : (showFusionTab ? "fusion" : "noresult");
  }
  elements.tabResultsView.classList.toggle("active", state.resultsViewMode === "results");
  elements.tabFusionEvidenceView.classList.toggle("active", state.resultsViewMode === "fusion");
  elements.tabNoResultView.classList.toggle("active", state.resultsViewMode === "noresult");

  if (!allAnnotations.length) {
    renderEmptyState(
      result.run_error ? "Run stopped" : "No results",
      result.run_error || "No gene annotations were returned.",
    );
    return;
  }

  applyResultsViewMode(visibleAnnotations, hiddenAnnotations, fusionEvidence);
}

function applyResultsViewMode(visibleAnnotations, hiddenAnnotations, fusionEvidence) {
  cancelOpenEvidenceCardLoads({ abort: false }); // the results list is about to be replaced; cards re-subscribe
  if (state.resultsViewMode === "noresult") {
    renderNoResultView(hiddenAnnotations);
    return;
  }
  if (state.resultsViewMode === "fusion") {
    renderFusionEvidenceView(fusionEvidence);
    return;
  }

  if (!visibleAnnotations.length) {
    renderEmptyState(
      "Insufficient evidence",
      "Every annotated gene had insufficient evidence, so no results are shown here. " +
        "See the \"No result\" tab above for details.",
    );
    return;
  }

  const list = document.createElement("div");
  list.className = "annotation-list";

  visibleAnnotations.forEach((annotation) => {
    const card = document.createElement("article");
    card.className = "annotation-card";
    card.id = `gene-${annotation.gene}`;
    card.innerHTML = `
      <header>
        <div class="annotation-heading">
          <h3>${escapeHtml(annotation.gene)}</h3>
          <div class="subtle">${escapeHtml(annotation.fusions?.length ? formatList(annotation.fusions) : "Gene lookup")}</div>
        </div>
        <div class="annotation-card-meta">
          ${annotation.date_annotated ? `<span class="status-pill">${escapeHtml(annotation.date_annotated)}</span>` : ""}
          <div class="cache-badge-slot"></div>
        </div>
      </header>
      <div class="status-line-slot"></div>
      <div class="annotation-fields"></div>
    `;

    const cacheBadge = renderCacheBadge(annotation);
    if (cacheBadge) card.querySelector(".cache-badge-slot").appendChild(cacheBadge);
    card.querySelector(".status-line-slot").appendChild(renderStatusLine(annotation));

    const fields = card.querySelector(".annotation-fields");
    for (const [field, label, type] of editableFields) {
      if (field === "error" && !annotation.error) continue;
      if (lazyAnnotationFields.has(field)) {
        fields.appendChild(renderLazyField(annotation, field, label, type));
        continue;
      }
      const row = renderField(annotation, field, label, type);
      if (field === "cancer_association_rationale") {
        row.classList.add("field-row-highlight");
        fields.appendChild(row);
        const actionability = renderClinicalActionability(annotation);
        if (actionability) fields.appendChild(actionability);
        // Evidence for the rationale belongs right next to it, not buried
        // below the rest of the fields — also keeps the initial card
        // height down, since the quotes inside are collapsed by default.
        const evidence = renderSupportingEvidence(annotation);
        if (evidence) fields.appendChild(evidence);
        continue;
      }
      fields.appendChild(row);
    }

    const domainsSection = renderDomainsAndTreatments(annotation);
    if (domainsSection) fields.appendChild(domainsSection);

    list.appendChild(card);

    // Independent, non-blocking sidecar card — fetched asynchronously from
    // GET /v1/genes/{gene}/openevidence and never delays the core card
    // above. Omitted entirely (not shown broken/empty) when OpenEvidence is
    // disabled, gated out client-side (see renderOpenEvidenceCard), or the
    // lookup fails.
    const openEvidenceCard = renderOpenEvidenceCard(annotation);
    if (openEvidenceCard) list.appendChild(openEvidenceCard);
  });

  elements.resultsWindow.replaceChildren(list);
  rebuildGeneIndex(visibleAnnotations);
}

function renderNoResultView(hiddenAnnotations) {
  if (!hiddenAnnotations.length) {
    renderEmptyState("No result", "Nothing to show here.");
    return;
  }

  // Reuses the same annotation-card/field-row-highlight visual language as
  // the Results tab (rather than a plain bulleted list) so this reads as
  // part of the same app, not a fallback debug view.
  const list = document.createElement("div");
  list.className = "annotation-list";

  hiddenAnnotations.forEach((annotation) => {
    const isError = Boolean(annotation.error);
    const reason =
      annotation.error ||
      annotation.evidence_support_explanation ||
      "Insufficient evidence to support an annotation.";

    const card = document.createElement("article");
    card.className = "annotation-card";
    card.id = `gene-${annotation.gene}`;
    card.innerHTML = `
      <header>
        <div class="annotation-heading">
          <h3>${escapeHtml(annotation.gene)}</h3>
          <div class="subtle">${escapeHtml(annotation.fusions?.length ? formatList(annotation.fusions) : "Gene lookup")}</div>
        </div>
        <div class="annotation-card-meta">
          <span class="review-badge ${isError ? "quality-warning" : "low"}">
            ${isError ? "Error" : "Insufficient evidence"}
          </span>
        </div>
      </header>
      <div class="field-row field-row-highlight">
        <span class="field-row-label">Reason</span>
        <div class="field-row-value">${escapeHtml(reason)}</div>
      </div>
    `;
    const summary = String(annotation.gene_summary || "").trim();
    if (summary) {
      const summaryRow = document.createElement("div");
      summaryRow.className = "field-row";
      summaryRow.innerHTML = `
        <span class="field-row-label">Interpreted gene summary</span>
        <div class="field-row-value">${escapeHtml(summary)}</div>
      `;
      card.appendChild(summaryRow);
    }
    const evidence = renderSupportingEvidence(annotation);
    if (evidence) card.appendChild(evidence);
    if (!isError) {
      const partnerPrecedent = renderFusionPartnerPrecedent(annotation);
      if (partnerPrecedent) card.appendChild(partnerPrecedent);
    }
    list.appendChild(card);
  });

  elements.resultsWindow.replaceChildren(list);
  rebuildGeneIndex(hiddenAnnotations);
}

function renderFusionEvidenceView(fusionEvidence) {
  const items = fusionEvidence || [];
  if (!items.length) {
    renderEmptyState(
      state.fusionEvidenceStatus === "running" ? "Fusion evidence loading" : "No fusion evidence",
      state.fusionEvidenceStatus === "running"
        ? "Exact fusion-pair PubMed retrieval is running in the background."
        : "No fusion-level evidence has been retrieved for this run.",
    );
    return;
  }

  const list = document.createElement("div");
  list.className = "annotation-list fusion-evidence-list";

  items.forEach((item) => {
    const card = document.createElement("article");
    card.className = "annotation-card fusion-evidence-card";
    card.id = `fusion-${cssSafeId(item.fusion)}`;
    const pmids = item.pmids || [];
    const cards = item.evidence_cards || [];
    card.innerHTML = `
      <header>
        <div class="annotation-heading">
          <h3>${escapeHtml(item.fusion)}</h3>
          <div class="subtle">${escapeHtml(item.tumor_type || "Any tumor type")}</div>
        </div>
        <div class="annotation-card-meta">
          <span class="review-badge ${item.well_supported ? "high" : "low"}">
            ${item.well_supported ? "Well supported" : "Exploratory"}
          </span>
          <span class="status-pill">
            ${item.retrieved_count || pmids.length} PMID${(item.retrieved_count || pmids.length) === 1 ? "" : "s"}
          </span>
        </div>
      </header>
      <div class="field-row field-row-highlight">
        <span class="field-row-label">Fusion interpretation</span>
        <div class="field-row-value">${escapeHtml(item.interpretation || "No interpretation available.")}</div>
      </div>
      <div class="supporting-evidence fusion-supporting-evidence"></div>
    `;

    const evidence = card.querySelector(".fusion-supporting-evidence");
    if (pmids.length) {
      const links = document.createElement("div");
      links.className = "citation-links";
      links.innerHTML = '<span class="field-label">Retrieved PMIDs</span>';
      const linkRow = document.createElement("div");
      linkRow.className = "citation-link-list";
      pmids.forEach((pmid) => linkRow.appendChild(makePubMedLink(pmid)));
      links.appendChild(linkRow);
      evidence.appendChild(links);
    }

    if (cards.length) {
      const cardList = document.createElement("div");
      cardList.className = "evidence-card-list";
      cards.forEach((evidenceCard) => {
        const itemCard = document.createElement("article");
        itemCard.className = "evidence-card";
        const evidenceType = String(evidenceCard.evidence_type || "fusion").replace(/_/g, " ");
        itemCard.innerHTML = `
          ${evidenceCardTopline(evidenceCard.pmid, evidenceType, evidenceCard.abstract)}
          <h5>${escapeHtml(evidenceCard.title || "Untitled PubMed record")}</h5>
          <p class="evidence-card-meta">${escapeHtml(evidenceCard.journal || "Journal unavailable")}</p>
          ${aliasMatchNoticeHtml(evidenceCard)}
          ${evidenceCard.selected_reason ? `<p>${escapeHtml(evidenceCard.selected_reason)}</p>` : ""}
          ${evidenceCard.quote ? `<blockquote>${escapeHtml(evidenceCard.quote)}</blockquote>` : ""}
        `;
        cardList.appendChild(itemCard);
      });
      evidence.appendChild(cardList);
    }

    const partnerPrecedent = renderFusionPartnerPrecedentForFusionEvidence(item);
    if (partnerPrecedent) evidence.appendChild(partnerPrecedent);

    list.appendChild(card);
  });

  elements.resultsWindow.replaceChildren(list);
  rebuildGeneIndex([]);
}

function cssSafeId(value) {
  return String(value || "").replace(/[^a-zA-Z0-9_-]/g, "-");
}

// Shared hover/focus tooltip markup for a PMID pill, so a curator can read
// the paper's abstract without leaving the page. Returns "" when there's no
// abstract to show, so callers can splice it in unconditionally.
function pmidAbstractTooltipHtml(abstract) {
  if (!abstract) return "";
  return `
    <span class="pmid-abstract-tooltip" role="tooltip">
      <span class="pmid-abstract-tooltip-label">Abstract</span>
      ${escapeHtml(abstract)}
    </span>
  `;
}

// A plain PMID link, used throughout for "Retrieved PMIDs"/"Cited on
// PubMed"/etc. lists. Pass `abstract` (when known for that pmid) to get the
// same hover tooltip evidence cards show; omit it for an ordinary pill.
function makePubMedLink(pmid, abstract) {
  const href = `https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(pmid)}/`;
  if (!abstract) {
    const a = document.createElement("a");
    a.href = href;
    a.target = "_blank";
    a.rel = "noreferrer";
    a.className = "citation-link";
    a.textContent = pmid;
    return a;
  }
  const wrapper = document.createElement("span");
  wrapper.className = "pmid-pill";
  wrapper.innerHTML = `
    <a href="${href}" target="_blank" rel="noreferrer" class="citation-link">${escapeHtml(pmid)}</a>
    ${pmidAbstractTooltipHtml(abstract)}
  `;
  return wrapper;
}

// Evidence-card PMID link + evidence-type badge, with the paper's abstract
// (when available) shown in a hover/focus tooltip so curators can read it
// without leaving the page.
function evidenceCardTopline(pmid, evidenceType, abstract) {
  return `
    <div class="evidence-card-topline">
      <span class="pmid-pill">
        <a href="https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(pmid)}/" target="_blank" rel="noreferrer">PMID ${escapeHtml(pmid)}</a>
        ${pmidAbstractTooltipHtml(abstract)}
      </span>
      <span class="review-badge context">${escapeHtml(evidenceType)}</span>
    </div>
  `;
}

// Visible callout for a fusion evidence card matched only through an HGNC
// alias/legacy gene symbol (e.g. KAT6A found via "MOZ"), so a clinician
// reading the paper knows it was retrieved under legacy nomenclature rather
// than assuming an unambiguous literal match. Returns "" for a literal match.
function aliasMatchNoticeHtml(evidenceCard) {
  const matches = evidenceCard.alias_matches || [];
  if (!evidenceCard.matched_via_alias || !matches.length) return "";
  const detail = matches.map((m) => `${escapeHtml(m.alias)} (${escapeHtml(m.gene)})`).join(", ");
  return `
    <p class="evidence-alias-notice">
      <span class="review-badge alias-match">Found via alias</span>
      <span class="evidence-alias-notice-detail">${detail}</span>
    </p>
  `;
}

function renderSupportingEvidence(annotation) {
  const quotes = annotation.supporting_quotes || [];
  const citations = annotation.citations || [];
  const evidenceCards = annotation.evidence_cards || [];
  const allRetrieved = annotation.retrieved_pmids || [];
  const count = annotation.retrieval_count ?? allRetrieved.length;

  if (!quotes.length && !citations.length && !evidenceCards.length && !count) return null;

  const section = document.createElement("div");
  section.className = "supporting-evidence";

  const heading = document.createElement("h4");
  heading.className = "supporting-evidence-heading";
  heading.textContent = "Supporting Evidence";
  section.appendChild(heading);

  // Total retrieved PMIDs — collapsible
  if (count > 0) {
    const details = document.createElement("details");
    details.className = "retrieval-details";
    const summary = document.createElement("summary");
    summary.className = "retrieval-summary";
    summary.innerHTML = `
      <span class="retrieval-count-label">
        ${count} total retrieved PMID${count === 1 ? "" : "s"}
      </span>
      <span class="retrieval-info-icon" title="Click to expand and see every PMID retrieved during literature search for this gene. A subset of these was selected for synthesis; the PMIDs actually used in the final annotation appear in the Cited on PubMed list below.">ⓘ</span>
    `;
    details.appendChild(summary);

    if (allRetrieved.length) {
      const pmidGrid = document.createElement("div");
      pmidGrid.className = "citation-link-list retrieval-pmid-grid";
      allRetrieved.forEach((pmid) => pmidGrid.appendChild(makePubMedLink(pmid)));
      details.appendChild(pmidGrid);
    }
    section.appendChild(details);
  }

  // Cited PMIDs as PubMed links. evidence_cards (when loaded) already carry
  // each cited pmid's abstract, so reuse it here for the same hover tooltip.
  if (citations.length) {
    const abstractByPmid = new Map(
      evidenceCards.filter((card) => card.abstract).map((card) => [card.pmid, card.abstract])
    );
    const citBlock = document.createElement("div");
    citBlock.className = "citation-links";
    const label = document.createElement("span");
    label.className = "field-label";
    label.textContent = "Cited on PubMed";
    citBlock.appendChild(label);
    const linkRow = document.createElement("div");
    linkRow.className = "citation-link-list";
    citations.forEach((pmid) => linkRow.appendChild(makePubMedLink(pmid, abstractByPmid.get(pmid))));
    citBlock.appendChild(linkRow);
    section.appendChild(citBlock);
  }

  const cardDetails = document.createElement("details");
  cardDetails.className = "evidence-card-details";
  const cardSummary = document.createElement("summary");
  cardSummary.className = "retrieval-summary";
  cardSummary.innerHTML = `
    <span class="retrieval-count-label">
      ${evidenceCards.length
        ? `${evidenceCards.length} evidence card${evidenceCards.length === 1 ? "" : "s"}`
        : "Evidence cards"}
    </span>
  `;
  cardDetails.appendChild(cardSummary);

  const cardList = document.createElement("div");
  cardList.className = "evidence-card-list";
  if (evidenceCards.length) {
    evidenceCards.forEach((card) => {
      const item = document.createElement("article");
      item.className = "evidence-card";
      const evidenceType = String(card.evidence_type || "other").replace(/_/g, " ");
      item.innerHTML = `
        ${evidenceCardTopline(card.pmid, evidenceType, card.abstract)}
        <h5>${escapeHtml(card.title || "Untitled PubMed record")}</h5>
        <p class="evidence-card-meta">${escapeHtml(card.journal || "Journal unavailable")}</p>
        ${card.selected_reason ? `<p>${escapeHtml(card.selected_reason)}</p>` : ""}
        ${card.quote ? `<blockquote>${escapeHtml(card.quote)}</blockquote>` : ""}
      `;
      cardList.appendChild(item);
    });
  } else {
    const placeholder = document.createElement("div");
    placeholder.className = "lazy-evidence-placeholder";
    placeholder.textContent = `Expand to load evidence cards for ${annotation.gene}.`;
    cardList.appendChild(placeholder);
  }
  cardDetails.appendChild(cardList);
  cardDetails.addEventListener("toggle", () => {
    if (!cardDetails.open || evidenceCards.length) return;
    cardList.replaceChildren(renderLoadingState(`Loading evidence cards for ${annotation.gene}`));
    enrichAnnotationOnDemand(annotation, "evidence cards")
      .then(() => {
        cardList.textContent = "Updating...";
      })
      .catch((error) => {
        cardList.textContent = "Unable to load evidence cards.";
        setMessage(formatRunError(error.message), "error");
      });
  });
  section.appendChild(cardDetails);

  // Grounding quotes — collapsed by default. These are the deepest level
  // of "show your work" (the actual sentences the annotation was derived
  // from) and take real vertical space; curators who want to check the
  // primary source can expand, but the card shouldn't lead with them.
  if (quotes.length) {
    const quoteDetails = document.createElement("details");
    quoteDetails.className = "quote-details";
    const quoteSummary = document.createElement("summary");
    quoteSummary.className = "quote-summary retrieval-summary";
    quoteSummary.innerHTML = `
      <span class="retrieval-count-label">
        ${quotes.length} supporting quote${quotes.length === 1 ? "" : "s"}
      </span>
    `;
    quoteDetails.appendChild(quoteSummary);

    const quoteList = document.createElement("div");
    quoteList.className = "quote-list";
    quotes.forEach((q) => {
      const blockquote = document.createElement("blockquote");
      blockquote.className = "supporting-quote";
      const p = document.createElement("p");
      p.textContent = q.quote;
      const cite = document.createElement("cite");
      const a = document.createElement("a");
      a.href = `https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(q.pmid)}/`;
      a.target = "_blank";
      a.rel = "noreferrer";
      a.textContent = `PMID ${q.pmid}`;
      cite.appendChild(a);
      blockquote.appendChild(p);
      blockquote.appendChild(cite);
      quoteList.appendChild(blockquote);
    });
    quoteDetails.appendChild(quoteList);
    section.appendChild(quoteDetails);
  }

  return section;
}

// ---------------------------------------------------------------------------
// Domains & treatments (on-demand, lazy-loaded fusion context)
// ---------------------------------------------------------------------------

// Structured facts only — domain names/status and drug names come straight
// from the fusion-annotation service response, never through the LLM. This
// keeps latency and clutter off the default card: nothing here fetches until
// a curator explicitly expands the section for a given fusion.
function fetchFusionContext(fusionKey) {
  if (state.fusionContextByFusion[fusionKey]) {
    return state.fusionContextByFusion[fusionKey];
  }
  const requestBody = { fusion: fusionKey, ...(state.lastInputsByFusion[fusionKey] || {}) };
  const promise = fetch("/v1/fusion-context", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(requestBody),
  })
    .then((response) => {
      if (!response.ok) throw new Error(response.statusText || "Request failed");
      return response.json();
    })
    .catch((error) => {
      delete state.fusionContextByFusion[fusionKey]; // allow retry on next expand
      throw error;
    });
  state.fusionContextByFusion[fusionKey] = promise;
  return promise;
}

function renderDomainPartner(partner) {
  const wrap = document.createElement("div");
  wrap.className = "domain-partner";

  const label = document.createElement("div");
  label.className = "domain-partner-label";
  label.textContent = `${partner.gene} (${partner.side === "five_prime" ? "5′" : "3′"})`;
  wrap.appendChild(label);

  const chipRow = document.createElement("div");
  chipRow.className = "domain-chip-row";
  const addChips = (names, statusClass) => {
    (names || []).forEach((name) => {
      const chip = document.createElement("span");
      chip.className = `domain-chip ${statusClass}`;
      chip.textContent = name;
      chipRow.appendChild(chip);
    });
  };
  addChips(partner.retained_domains, "domain-chip-retained");
  addChips(partner.disrupted_domains, "domain-chip-disrupted");
  addChips(partner.lost_domains, "domain-chip-lost");
  if (!chipRow.children.length) {
    const none = document.createElement("span");
    none.className = "subtle";
    none.textContent = "No domain data";
    chipRow.appendChild(none);
  }
  wrap.appendChild(chipRow);
  return wrap;
}

function renderFusionKnowledge(knowledge) {
  if (!knowledge || !knowledge.therapies?.length) return null;

  const wrap = document.createElement("div");
  wrap.className = "fusion-knowledge";

  const label = document.createElement("span");
  label.className = "field-label";
  label.textContent = "Known treatments";
  wrap.appendChild(label);

  const list = document.createElement("div");
  list.className = "citation-link-list";
  knowledge.therapies.forEach((therapy) => {
    const pill = document.createElement("span");
    pill.className = "review-badge context";
    pill.textContent = therapy;
    list.appendChild(pill);
  });
  wrap.appendChild(list);

  if (knowledge.sources?.length) {
    const sources = document.createElement("div");
    sources.className = "fusion-knowledge-sources subtle";
    sources.textContent = `Source: ${knowledge.sources.join(", ")}`;
    wrap.appendChild(sources);
  }

  return wrap;
}

function renderFusionContextResult(container, response) {
  if (!response.available) {
    container.textContent = "Domain and treatment data is not configured for this deployment.";
    return;
  }
  const context = response.context;
  if (!context || context.error) {
    container.textContent = "Domain and treatment data not available for this fusion.";
    return;
  }

  container.replaceChildren();

  if (context.kinase_gene && context.kinase_domain_status) {
    const kinaseLine = document.createElement("div");
    kinaseLine.className = "domain-kinase-callout";
    kinaseLine.textContent =
      `Kinase domain (${context.kinase_gene}): ${context.kinase_domain_status}`;
    container.appendChild(kinaseLine);
  }

  const partners = document.createElement("div");
  partners.className = "domain-partners";
  partners.appendChild(renderDomainPartner(context.five_prime));
  partners.appendChild(renderDomainPartner(context.three_prime));
  container.appendChild(partners);

  const knowledge = renderFusionKnowledge(context.knowledge);
  if (knowledge) container.appendChild(knowledge);

  if (!knowledge && !context.kinase_gene) {
    const none = document.createElement("div");
    none.className = "subtle";
    none.textContent = "No known treatments reported for this fusion.";
    container.appendChild(none);
  }
}

function renderDomainsAndTreatments(annotation) {
  const fusionKey = annotation.fusions?.[0];
  if (!fusionKey) return null; // singleton gene lookup — no breakpoint to analyze

  const details = document.createElement("details");
  details.className = "retrieval-details";
  const summary = document.createElement("summary");
  summary.className = "retrieval-summary";
  summary.innerHTML = `<span class="retrieval-count-label">Domains &amp; treatments</span>`;
  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "domain-context-body";
  body.textContent = "Loading…";
  details.appendChild(body);

  let loaded = false;
  details.addEventListener("toggle", () => {
    if (!details.open || loaded) return;
    loaded = true;
    fetchFusionContext(fusionKey)
      .then((response) => renderFusionContextResult(body, response))
      .catch(() => {
        body.textContent = "Domain and treatment data not available for this fusion.";
        loaded = false; // allow a retry on next expand
      });
  });

  return details;
}

// ---------------------------------------------------------------------------
// OpenEvidence sidecar card — an independent, non-blocking clinical
// reference card fetched from GET /v1/genes/{gene}/openevidence. Always
// starts loading immediately (no click-to-expand), but never blocks
// rendering the core annotation card above it — see applyResultsViewMode.
// ---------------------------------------------------------------------------

// Caps how many /v1/genes/{gene}/openevidence fetches run at once from this
// page. Without this, rendering a large batch result fires one fetch per
// gene card the instant the list mounts — the server has its own limiter
// (settings.openevidence_sidecar_concurrency) protecting the vendor call
// itself, but queuing client-side too avoids opening a pile of simultaneous
// requests (and showing every card's spinner at once) for no benefit.
const OPENEVIDENCE_MAX_CONCURRENT_FETCHES = 3;
let openEvidenceActiveFetchCount = 0;
const openEvidenceFetchQueue = [];

function runOpenEvidenceFetchQueue() {
  while (
    openEvidenceActiveFetchCount < OPENEVIDENCE_MAX_CONCURRENT_FETCHES &&
    openEvidenceFetchQueue.length
  ) {
    const job = openEvidenceFetchQueue.shift();
    openEvidenceActiveFetchCount += 1;
    job().finally(() => {
      openEvidenceActiveFetchCount -= 1;
      runOpenEvidenceFetchQueue();
    });
  }
}

function enqueueOpenEvidenceFetch(job) {
  openEvidenceFetchQueue.push(job);
  runOpenEvidenceFetchQueue();
}

// "Pending + poll": on a cache miss the server starts the (90-290s)
// OpenEvidence lookup in the background and answers status "pending" (HTTP
// 202 + retry_after_seconds, because we send OPENEVIDENCE_POLL_HEADERS)
// instead of holding the request open past the ingress timeout — see
// GET /v1/genes/{gene}/openevidence in main.py. The
// card keeps its loading state and re-polls the same URL with backoff: the
// first delay is the server's retry_after hint, then x1.5 per poll, capped
// at maxDelayMs, and the card gives up after totalCapMs. Each poll is its
// own short trip through the fetch queue above, so a pending card never
// holds a concurrency slot while it waits.
const OPENEVIDENCE_POLL = {
  defaultDelayMs: 10000,
  minDelayMs: 1000,
  maxDelayMs: 20000,
  backoff: 1.5,
  // Must exceed the server's per-lookup budget
  // (OPENEVIDENCE_SIDECAR_LOOKUP_TIMEOUT_SECONDS, 900s by default) plus some
  // queue time, so the card never gives up on a lookup that can still finish.
  totalCapMs: 20 * 60 * 1000,
};

// Tells the server this client understands status "pending", so it answers
// pending with 202 rather than the 503 it keeps for old cached frontends
// that don't (see get_gene_openevidence's docstring in main.py).
const OPENEVIDENCE_POLL_HEADERS = { "X-OpenEvidence-Poll": "1" };

function nextOpenEvidencePollDelayMs(response, previousDelayMs) {
  const hintSeconds = Number(response?.retry_after_seconds);
  const hintMs = Number.isFinite(hintSeconds) && hintSeconds > 0 ? hintSeconds * 1000 : OPENEVIDENCE_POLL.defaultDelayMs;
  const delayMs = previousDelayMs === null ? hintMs : Math.max(hintMs, previousDelayMs * OPENEVIDENCE_POLL.backoff);
  return Math.min(OPENEVIDENCE_POLL.maxDelayMs, Math.max(OPENEVIDENCE_POLL.minDelayMs, delayMs));
}

const OPENEVIDENCE_MESSAGES = {
  pending: "Still checking OpenEvidence — this can take a few minutes…",
  failed: "OpenEvidence lookup failed. Try again later.",
  timeout: "OpenEvidence is taking longer than expected. Re-run the annotation later to check again.",
};

// Replaces the card body with a one-line status note (failed/timed out).
function renderOpenEvidenceCardNotice(body, message, kind) {
  const note = document.createElement("div");
  note.className = `subtle openevidence-card-notice openevidence-card-${kind}`;
  note.setAttribute("role", "status");
  note.textContent = message;
  body.replaceChildren(note);
}

// One request lifecycle per sidecar key (gene|tumor type|fusion — see
// openEvidenceKey). The lifecycle, not any one card, owns everything about
// the request: its fetch-queue slot, its AbortController, its poll loop and
// a single overall deadline (totalCapMs from when the lifecycle started,
// covering queue wait and stalled requests; never reset). Cards only
// subscribe to it to be told what to show.
//
// - Re-rendering the results list (which happens on every annotation /
//   fusion-evidence job poll) detaches every card; the replacement cards
//   re-subscribe to the still-running lifecycle during the same render, so
//   the in-flight request, its slot and its deadline carry over untouched.
//   A lifecycle no card re-subscribed to by the end of that render (e.g. a
//   new run without that gene) is cancelled.
// - Leaving the results view cancels every lifecycle.
// - Cancelling (or the deadline expiring) aborts the request, frees its
//   slot, stops polling and drops its in-flight memo entry; on expiry the
//   subscribed cards show the timed-out note. Late answers are ignored.
// - A card that has been mounted and then leaves the page (removed, or its
//   results list replaced) is unsubscribed at the lifecycle's next step
//   (when its queued job runs, when an answer arrives, or before a re-poll);
//   a lifecycle left with no cards is cancelled before making its request.
const openEvidenceLifecycles = new Map();

// Terminal outcomes ("timeout" / "failed") per key, kept for the current
// annotation run only: a progress re-render of the same run replays the
// note instead of starting a new lifecycle (no fresh deadline, no new
// requests), while a new run — a different run_id — starts clean and so
// retries. renderAnnotationResult syncs the run; a job's switch from its
// progress id (job_id) to the final run_id is adopted as the same run
// (pollAnnotationJob, before the completed result is first rendered).
const openEvidenceTerminal = { runId: undefined, byKey: new Map() };

function syncOpenEvidenceRun(runId) {
  if (runId === openEvidenceTerminal.runId) return;
  openEvidenceTerminal.runId = runId;
  openEvidenceTerminal.byKey.clear();
  // A genuinely new run must not inherit an old run's lifecycle (and with
  // it the rest of that run's deadline): cancel them so its cards start
  // fresh ones. (A job's job_id -> run_id switch is adopted beforehand,
  // so it never gets here.)
  cancelOpenEvidenceCardLoads();
}

// Renames the current run from a job's progress id to its final run_id —
// only if the current run really is that job's.
function adoptOpenEvidenceRunId(jobId, runId) {
  if (openEvidenceTerminal.runId === jobId) openEvidenceTerminal.runId = runId;
}

function openEvidenceKey(gene, tumorType, fusion) {
  return `${gene}|${tumorType || ""}|${fusion || ""}`;
}

function cancelOpenEvidenceCardLoads({ abort = true } = {}) {
  if (abort) {
    [...openEvidenceLifecycles.values()].forEach((lifecycle) => lifecycle.cancel());
    return;
  }
  openEvidenceLifecycles.forEach((lifecycle) => lifecycle.subscribers.clear());
  Promise.resolve().then(() => {
    [...openEvidenceLifecycles.values()].forEach((lifecycle) => {
      if (lifecycle.subscribers.size === 0) lifecycle.cancel();
    });
  });
}

function createOpenEvidenceLifecycle(key, request) {
  let delayMs = null;
  let pollTimer = null;
  let deadlineTimer = null;
  let controller = null;
  let inflight = null;
  let releaseSlot = null; // settles the fetch-queue job holding the slot
  const lifecycle = {
    key,
    subscribers: new Set(),
    pending: false,
    finished: false,
    cancel: () => stop({ interrupt: true }),
  };

  // interrupt: the request didn't settle on its own (cancel/deadline) —
  // abort it, free its slot and forget its in-flight memo entry.
  const stop = ({ interrupt }) => {
    if (lifecycle.finished) return;
    lifecycle.finished = true;
    if (openEvidenceLifecycles.get(key) === lifecycle) openEvidenceLifecycles.delete(key);
    clearTimeout(pollTimer);
    clearTimeout(deadlineTimer);
    if (!interrupt) return;
    if (controller) controller.abort();
    if (inflight && state.openEvidenceByGene[key] === inflight) delete state.openEvidenceByGene[key];
    if (releaseSlot) releaseSlot();
  };
  const finish = (render, { interrupt = false } = {}) => {
    if (lifecycle.finished) return;
    const subscribers = [...lifecycle.subscribers];
    stop({ interrupt });
    subscribers.forEach(render);
  };
  // Unsubscribes cards that were mounted and have since left the page.
  // False when no card is left, after cancelling the lifecycle.
  const stillWanted = () => {
    lifecycle.subscribers.forEach((subscriber) => {
      if (subscriber.card.isConnected) subscriber.wasConnected = true;
      else if (subscriber.wasConnected) lifecycle.subscribers.delete(subscriber);
    });
    if (lifecycle.subscribers.size > 0) return true;
    lifecycle.cancel();
    return false;
  };
  const settleTerminal = (kind, { interrupt }) => {
    if (lifecycle.finished) return;
    openEvidenceTerminal.byKey.set(key, kind);
    finish((subscriber) => renderOpenEvidenceCardNotice(subscriber.body, OPENEVIDENCE_MESSAGES[kind], kind), {
      interrupt,
    });
  };
  const timeOut = () => settleTerminal("timeout", { interrupt: true });
  const handle = (response) => {
    if (lifecycle.finished || !stillWanted()) return; // late answer, or nobody left to show it
    if (response?.status === "failed") {
      settleTerminal("failed", { interrupt: false });
      return;
    }
    if (response?.status !== "pending") {
      finish((subscriber) => renderOpenEvidenceCardBody(subscriber.card, subscriber.body, response));
      return;
    }
    if (!lifecycle.pending) {
      lifecycle.pending = true;
      lifecycle.subscribers.forEach(showOpenEvidencePending);
    }
    delayMs = nextOpenEvidencePollDelayMs(response, delayMs);
    if (Date.now() - startedAt + delayMs > OPENEVIDENCE_POLL.totalCapMs) {
      timeOut();
      return;
    }
    pollTimer = setTimeout(() => {
      if (lifecycle.finished) return;
      if (!state.openevidenceEnabled) {
        finish((subscriber) => subscriber.card.remove(), { interrupt: true });
        return;
      }
      lifecycle.subscribers.forEach((subscriber) => {
        if (subscriber.card.isConnected === false) {
          lifecycle.subscribers.delete(subscriber);
          subscriber.card.remove();
        }
      });
      if (stillWanted()) poll();
    }, delayMs);
  };
  const job = () => {
    // Checked when the queued job actually runs, not when it was queued.
    if (lifecycle.finished || !stillWanted()) return Promise.resolve();
    controller = typeof AbortController === "function" ? new AbortController() : null;
    const slotReleased = new Promise((resolve) => {
      releaseSlot = resolve;
    });
    inflight = request(controller ? controller.signal : undefined);
    const settled = inflight.then(handle, () => {
      if (!lifecycle.finished) finish((subscriber) => subscriber.card.remove());
    });
    return Promise.race([settled, slotReleased]).finally(() => {
      releaseSlot = null;
      controller = null;
      inflight = null;
    });
  };
  const poll = () => enqueueOpenEvidenceFetch(job);
  const startedAt = Date.now();
  lifecycle.start = () => {
    deadlineTimer = setTimeout(timeOut, OPENEVIDENCE_POLL.totalCapMs);
    poll();
  };
  return lifecycle;
}

function showOpenEvidencePending(subscriber) {
  subscriber.body.replaceChildren(renderLoadingState(OPENEVIDENCE_MESSAGES.pending));
}

// Subscribes one card to its key's lifecycle, starting one if none is
// running — unless the key already timed out or failed in this run, in
// which case the card just shows that note. `request(signal)` is only used
// when a new lifecycle starts.
function loadOpenEvidenceCard(card, body, key, request) {
  let lifecycle = openEvidenceLifecycles.get(key);
  const terminal = openEvidenceTerminal.byKey.get(key);
  if (!lifecycle && terminal) {
    renderOpenEvidenceCardNotice(body, OPENEVIDENCE_MESSAGES[terminal], terminal);
    return;
  }
  const isNew = !lifecycle;
  // Results isn't showing (e.g. an annotation job's progress re-rendered
  // the hidden panel while on Benchmark): start nothing. switchView cancelled
  // the lifecycles on the way out and re-renders the cards on the way back,
  // which starts them then; meanwhile the hidden card keeps its loading state.
  if (isNew && state.currentView !== "annotate") return;
  if (isNew) {
    lifecycle = createOpenEvidenceLifecycle(key, request);
    openEvidenceLifecycles.set(key, lifecycle);
  }
  const subscriber = { card, body, wasConnected: false };
  lifecycle.subscribers.add(subscriber);
  // The caller mounts the card right after rendering it; from then on, a
  // card that is no longer connected has been removed or replaced.
  Promise.resolve().then(() => {
    if (card.isConnected) subscriber.wasConnected = true;
  });
  if (lifecycle.pending) showOpenEvidencePending(subscriber);
  if (isNew) lifecycle.start();
}

function fetchGeneOpenEvidence(
  gene,
  tumorType,
  { cancerAssociated, insufficientEvidence, corePmids, coreTitles, fusion, signal } = {}
) {
  // Defense in depth: renderOpenEvidenceCard is the only caller today and
  // already gates on state.openevidenceEnabled before ever reaching this
  // function, but this function issues the actual network request, so it
  // must not fire regardless of how it gets called.
  if (!state.openevidenceEnabled) {
    return Promise.resolve({ available: false });
  }
  // `fusion` is part of the key because the server asks (and caches) a
  // fusion-specific question when it's present — see _cache_key in
  // src/pipeline/openevidence.py — so a fusion and a plain-gene lookup for
  // the same gene are different answers and must not share a promise.
  const key = openEvidenceKey(gene, tumorType, fusion);
  if (state.openEvidenceByGene[key]) {
    return state.openEvidenceByGene[key];
  }
  const params = new URLSearchParams();
  if (tumorType) params.set("tumor_type", tumorType);
  if (fusion) params.set("fusion", fusion);
  // Lets the server skip the live call for a gene it's already confident has
  // no cancer association — see GET /v1/genes/{gene}/openevidence's gating
  // docstring in main.py. Omitted (undefined/null) rather than sent as
  // "false" when unknown, so the server's default (never skip) applies.
  if (cancerAssociated !== undefined && cancerAssociated !== null) {
    params.set("cancer_associated", String(cancerAssociated));
  }
  if (insufficientEvidence) params.set("insufficient_evidence", "true");
  // The core pipeline's own verified PMIDs/evidence titles for this gene —
  // lets the server drop OpenEvidence citations that just repeat what our
  // own PubMed-abstract retrieval already found (see distill_additive_openevidence).
  (corePmids || []).forEach((pmid) => pmid && params.append("core_pmids", pmid));
  (coreTitles || []).forEach((title) => title && params.append("core_titles", title));
  const query = params.toString();
  const forget = () => {
    if (state.openEvidenceByGene[key] === promise) delete state.openEvidenceByGene[key];
  };
  const url = `/v1/genes/${encodeURIComponent(gene)}/openevidence${query ? `?${query}` : ""}`;
  const init = { headers: OPENEVIDENCE_POLL_HEADERS };
  if (signal) init.signal = signal;
  const promise = fetch(url, init)
    .then(async (response) => {
      // "Pending" arrives as HTTP 202 for us. A 503 with a pending body is
      // still accepted (a server pod that predates the poll header, e.g.
      // mid rolling deploy); any other non-2xx, or a 503 that isn't our
      // pending body (e.g. from the ingress), is an error.
      if (!response.ok && response.status !== 503) throw new Error(response.statusText || "Request failed");
      const payload = await response.json();
      if (payload?.status === "pending" || payload?.status === "failed") {
        // Never memoize "pending" (the next poll must really re-fetch) or
        // "failed" (the server only remembers a failure for a few minutes,
        // so a later re-render must be able to ask again and recover).
        forget();
      } else if (!response.ok) {
        throw new Error(response.statusText || "Request failed");
      }
      return payload;
    })
    .catch((error) => {
      forget(); // allow retry on next render (also covers an aborted request)
      throw error;
    });
  state.openEvidenceByGene[key] = promise;
  return promise;
}

function renderOpenEvidenceGuidelines(distilled) {
  if (!distilled.guidelines?.length) return null;
  const section = document.createElement("div");
  section.className = "openevidence-section";
  const heading = document.createElement("div");
  heading.className = "field-row-label";
  heading.textContent = "Clinical practice guidelines";
  section.appendChild(heading);

  const list = document.createElement("ul");
  list.className = "openevidence-guideline-list";
  distilled.guidelines.forEach((guideline) => {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = guideline.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = guideline.title || guideline.url;
    item.appendChild(link);
    list.appendChild(item);
  });
  section.appendChild(list);
  return section;
}

function renderOpenEvidenceTrialMentions(distilled) {
  if (!distilled.trial_mentions?.length) return null;
  const section = document.createElement("div");
  section.className = "openevidence-section";
  const heading = document.createElement("div");
  heading.className = "field-row-label";
  heading.textContent = "Trial & outcome evidence";
  section.appendChild(heading);

  const list = document.createElement("ul");
  list.className = "openevidence-trial-list";
  distilled.trial_mentions.forEach((mention) => {
    const item = document.createElement("li");
    item.textContent = mention.trial ? `[${mention.trial}] ${mention.sentence}` : mention.sentence;
    list.appendChild(item);
  });
  section.appendChild(list);
  return section;
}

function renderOpenEvidenceCardBody(card, body, response) {
  if (!response?.available || !response.distilled) {
    card.remove(); // not enabled / not found — hide rather than show a broken card
    return;
  }
  const distilled = response.distilled;
  body.replaceChildren();

  if (distilled.consensus_role) {
    const role = document.createElement("p");
    role.className = "openevidence-consensus-role";
    role.textContent = distilled.consensus_role;
    body.appendChild(role);
  }

  const guidelines = renderOpenEvidenceGuidelines(distilled);
  if (guidelines) body.appendChild(guidelines);

  const trials = renderOpenEvidenceTrialMentions(distilled);
  if (trials) body.appendChild(trials);

  if (!distilled.consensus_role && !guidelines && !trials) {
    const empty = document.createElement("div");
    empty.className = "subtle";
    empty.textContent = "OpenEvidence found no clinical guidelines or trial evidence for this gene.";
    body.appendChild(empty);
  }

  if (distilled.redundant_citation_count > 0) {
    const note = document.createElement("div");
    note.className = "subtle openevidence-redundant-note";
    note.textContent =
      distilled.redundant_citation_count === 1
        ? "1 additional OpenEvidence citation was omitted as already covered by this gene's own literature evidence."
        : `${distilled.redundant_citation_count} additional OpenEvidence citations were omitted as already covered by this gene's own literature evidence.`;
    body.appendChild(note);
  }

  const footnote = document.createElement("div");
  footnote.className = "subtle openevidence-footnote";
  footnote.textContent =
    "Unverified supplementary evidence from OpenEvidence, shown here only when it goes beyond this gene's own literature evidence — not PMID-verified.";
  body.appendChild(footnote);
}

function renderOpenEvidenceCard(annotation) {
  // Feature-flag gate: settings.openevidence_enabled, learned from GET
  // /v1/dev/status on page load (see loadDevStatus). When off, render no
  // card at all and never call fetchGeneOpenEvidence, so the client issues
  // no GET /v1/genes/{gene}/openevidence request — the server's
  // available:false response is a runtime fallback for other callers, not a
  // substitute for not asking at all.
  if (!state.openevidenceEnabled) {
    return null;
  }

  // Mirrors the server-side gate in GET /v1/genes/{gene}/openevidence: skip
  // entirely (no card, no fetch, no "Checking OpenEvidence…" flash) when our
  // own pipeline is already confident this gene has no cancer association —
  // OpenEvidence's guideline/trial-focused question has nothing plausible to
  // find there (see that endpoint's docstring for the benchmark evidence).
  // The server enforces this independently too, since it's the only guard
  // for any other caller of that endpoint; checking here too just spares a
  // round-trip and a loading flicker for the common case.
  if (annotation.cancer_associated === false && !annotation.insufficient_evidence) {
    return null;
  }

  const card = document.createElement("article");
  card.className = "annotation-card openevidence-card";
  card.id = `openevidence-${annotation.gene}`;
  card.innerHTML = `
    <header>
      <div class="annotation-heading">
        <h3>OpenEvidence</h3>
        <div class="subtle">Clinical practice guidelines &amp; external trial evidence for ${escapeHtml(annotation.gene)}</div>
      </div>
    </header>
    <div class="openevidence-card-body"></div>
  `;
  const body = card.querySelector(".openevidence-card-body");
  body.appendChild(renderLoadingState("Checking OpenEvidence…"));

  const tumorType = tumorTypeForAnnotation(annotation);
  const fusion = annotation.fusions?.[0];
  loadOpenEvidenceCard(card, body, openEvidenceKey(annotation.gene, tumorType, fusion), (signal) =>
    fetchGeneOpenEvidence(annotation.gene, tumorType, {
      signal,
      cancerAssociated: annotation.cancer_associated,
      insufficientEvidence: annotation.insufficient_evidence,
      corePmids: annotation.citations,
      coreTitles: (annotation.evidence_cards || []).map((card) => card.title).filter(Boolean),
      // annotation.fusions holds only fusion-shaped inputs for this gene, in
      // input order (annotate_one's associated_fusions in orchestrator.py).
      // The first one is the same fusion the offline warmup passes to
      // get_gene_analysis (openevidence_warmup.py's warm_one), so this
      // request hits the cache slot warmup filled.
      fusion,
    })
  );

  return card;
}

// ---------------------------------------------------------------------------
// Fusion partner precedent (lazy — only runs when a curator expands it for a
// gene with insufficient_evidence; never fired automatically)
// ---------------------------------------------------------------------------

function splitFusionGenes(fusion) {
  for (const sep of ["::", "--", "/"]) {
    if (fusion.includes(sep)) {
      return fusion.split(sep).map((g) => g.trim()).filter(Boolean);
    }
  }
  return [];
}

function fusionPartnerGenes(annotation) {
  const seen = new Set();
  const partners = [];
  (annotation.fusions || []).forEach((fusion) => {
    splitFusionGenes(fusion).forEach((gene) => {
      const normalized = gene.trim().toUpperCase();
      if (!normalized || normalized === annotation.gene || seen.has(normalized)) return;
      seen.add(normalized);
      partners.push(normalized);
    });
  });
  return partners;
}

function tumorTypeForAnnotation(annotation) {
  for (const fusion of annotation.fusions || []) {
    const match = state.lastInputsByFusion[fusion];
    if (match && match.tumor_type) return match.tumor_type;
  }
  return null;
}

function fetchFusionPartnerEvidence(gene, { tumorType, agnostic, excludePmids } = {}) {
  const key = `${gene}|${tumorType || ""}|${agnostic ? "1" : "0"}`;
  if (state.fusionPartnerEvidenceByKey[key]) {
    return state.fusionPartnerEvidenceByKey[key];
  }
  const payload = { gene };
  if (tumorType) payload.tumor_type = tumorType;
  if (agnostic) payload.agnostic = true;
  if (excludePmids && excludePmids.length) payload.exclude_pmids = excludePmids;

  const promise = fetch("/v1/fusion-partner-evidence", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then((response) => {
      if (!response.ok) throw new Error(response.statusText || "Request failed");
      return response.json();
    })
    .catch((error) => {
      delete state.fusionPartnerEvidenceByKey[key]; // allow retry
      throw error;
    });
  state.fusionPartnerEvidenceByKey[key] = promise;
  return promise;
}

function renderFusionPartnerResultBody(container, data) {
  container.replaceChildren();

  const scopeNote =
    data.scope === "tumor_type_scoped" && data.tumor_type
      ? ` in ${data.tumor_type}`
      : data.scope === "all_tumor_types"
        ? " across other tumor types"
        : "";

  const summary = document.createElement("div");
  summary.className = "subtle";
  summary.textContent = data.has_precedent
    ? `${data.retrieved_count} paper${data.retrieved_count === 1 ? "" : "s"} found discussing ` +
      `${data.gene} in oncogenic fusions${scopeNote}.`
    : `No oncogenic fusion literature found for ${data.gene}${scopeNote}.`;
  container.appendChild(summary);

  if (data.pmids?.length) {
    const linkRow = document.createElement("div");
    linkRow.className = "citation-link-list";
    data.pmids.forEach((pmid) => linkRow.appendChild(makePubMedLink(pmid)));
    container.appendChild(linkRow);
  }

  if (data.evidence_cards?.length) {
    const cardList = document.createElement("div");
    cardList.className = "evidence-card-list";
    data.evidence_cards.forEach((evidenceCard) => {
      const itemCard = document.createElement("article");
      itemCard.className = "evidence-card";
      const evidenceType = String(evidenceCard.evidence_type || "fusion").replace(/_/g, " ");
      itemCard.innerHTML = `
        ${evidenceCardTopline(evidenceCard.pmid, evidenceType, evidenceCard.abstract)}
        <h5>${escapeHtml(evidenceCard.title || "Untitled PubMed record")}</h5>
        <p class="evidence-card-meta">${escapeHtml(evidenceCard.journal || "Journal unavailable")}</p>
        ${evidenceCard.selected_reason ? `<p>${escapeHtml(evidenceCard.selected_reason)}</p>` : ""}
        ${evidenceCard.quote ? `<blockquote>${escapeHtml(evidenceCard.quote)}</blockquote>` : ""}
      `;
      cardList.appendChild(itemCard);
    });
    container.appendChild(cardList);
  }
}

function appendFusionPartnerAgnosticFollowUp(body, gene, tumorType, seenPmids) {
  const followUp = document.createElement("div");
  followUp.className = "fusion-partner-followup";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "text-button";
  button.textContent = "Explore all tumor types for extra evidence";

  const resultBox = document.createElement("div");

  button.addEventListener("click", () => {
    button.disabled = true;
    resultBox.replaceChildren(renderLoadingState(`Searching all tumor types for ${gene}`));
    followUp.appendChild(resultBox);
    fetchFusionPartnerEvidence(gene, { tumorType, agnostic: true, excludePmids: seenPmids })
      .then((data) => {
        renderFusionPartnerResultBody(resultBox, data);
        button.remove();
      })
      .catch((error) => {
        resultBox.textContent = `Unable to load fusion precedent for ${gene}.`;
        setMessage(formatRunError(error.message), "error");
        button.disabled = false;
      });
  });

  followUp.appendChild(button);
  body.appendChild(followUp);
}

function renderFusionPartnerCheck(gene, tumorType) {
  const details = document.createElement("details");
  details.className = "field-row lazy-field-row";

  const summary = document.createElement("summary");
  summary.className = "lazy-field-summary";
  summary.innerHTML = tumorType
    ? `<span class="field-row-label">Check ${escapeHtml(gene)} for ${escapeHtml(tumorType)} fusion precedent</span>`
    : `<span class="field-row-label">Check ${escapeHtml(gene)} for fusion precedent</span>`;
  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "field-row-value lazy-field-value";
  body.textContent = `Expand to search PubMed for ${gene} fusion precedent.`;
  details.appendChild(body);

  let loaded = false;
  details.addEventListener("toggle", () => {
    if (!details.open || loaded) return;
    loaded = true;
    body.replaceChildren(renderLoadingState(`Searching PubMed for ${gene} fusion precedent`));
    fetchFusionPartnerEvidence(gene, { tumorType, agnostic: !tumorType })
      .then((data) => {
        body.replaceChildren();
        renderFusionPartnerResultBody(body, data);
        if (tumorType) appendFusionPartnerAgnosticFollowUp(body, gene, tumorType, data.pmids || []);
      })
      .catch((error) => {
        body.textContent = `Unable to load fusion precedent for ${gene}.`;
        setMessage(formatRunError(error.message), "error");
        loaded = false; // allow a retry on next expand
      });
  });

  return details;
}

function renderFusionPartnerQueryButton(gene, tumorType) {
  const container = document.createElement("div");
  container.className = "fusion-partner-query";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "fusion-partner-query-button";
  button.textContent = gene;
  const queryLabel = tumorType
    ? `Query ${gene} fusion precedent in ${tumorType}`
    : `Query ${gene} fusion precedent`;
  button.setAttribute("aria-label", queryLabel);
  button.title = queryLabel;

  const resultBox = document.createElement("div");
  resultBox.className = "fusion-partner-query-result hidden";

  button.addEventListener("click", () => {
    button.disabled = true;
    resultBox.classList.remove("hidden");
    resultBox.replaceChildren(renderLoadingState(`Searching PubMed for ${gene} fusion precedent`));
    fetchFusionPartnerEvidence(gene, { tumorType, agnostic: !tumorType })
      .then((data) => {
        resultBox.replaceChildren();
        renderFusionPartnerResultBody(resultBox, data);
        if (tumorType) appendFusionPartnerAgnosticFollowUp(resultBox, gene, tumorType, data.pmids || []);
      })
      .catch((error) => {
        resultBox.textContent = `Unable to load fusion precedent for ${gene}.`;
        setMessage(formatRunError(error.message), "error");
      })
      .finally(() => {
        button.disabled = false;
      });
  });

  container.appendChild(button);
  container.appendChild(resultBox);
  return container;
}

function renderFusionPartnerPrecedentForFusionEvidence(item) {
  const retrievedCount = item.retrieved_count ?? (item.pmids || []).length;
  if (retrievedCount > 0) return null;

  const partners = [];
  const seen = new Set();
  splitFusionGenes(item.fusion || "").forEach((gene) => {
    const normalized = gene.trim().toUpperCase();
    if (!normalized || seen.has(normalized)) return;
    seen.add(normalized);
    partners.push(normalized);
  });
  if (partners.length < 2) return null;

  const wrapper = document.createElement("div");
  wrapper.className = "fusion-partner-precedent fusion-partner-precedent-inline";

  const note = document.createElement("div");
  note.className = "subtle";
  note.textContent = item.tumor_type
    ? `No exact fusion-pair literature was found. Select a partner gene to query fusion precedent in ${item.tumor_type}; after that, the search can be broadened across all tumor types.`
    : "No exact fusion-pair literature was found. Select a partner gene to query fusion precedent across reported fusions.";
  wrapper.appendChild(note);

  const queryList = document.createElement("div");
  queryList.className = "fusion-partner-query-list";
  partners.forEach((partner) => queryList.appendChild(renderFusionPartnerQueryButton(partner, item.tumor_type || null)));
  wrapper.appendChild(queryList);
  return wrapper;
}

// Only for genes that came back insufficient_evidence — prioritizes the
// tumor type already in play for this case (if known), with a subsequent
// tumor-type-agnostic search offered as a follow-up rather than run by
// default, since it's an extra round of PubMed calls per gene.
function renderFusionPartnerPrecedent(annotation) {
  const partners = fusionPartnerGenes(annotation);
  if (!partners.length) return null;

  const tumorType = tumorTypeForAnnotation(annotation);
  const wrapper = document.createElement("div");
  wrapper.className = "fusion-partner-precedent";
  partners.forEach((partner) => wrapper.appendChild(renderFusionPartnerCheck(partner, tumorType)));
  return wrapper;
}

function renderBenchmarkResult(result) {
  updateExportState();
  rebuildGeneIndex([]);

  if (!result) {
    elements.runSummary.textContent = "Run a holdout benchmark to populate this review area.";
    renderEmptyState("No benchmark yet", "Run the holdout benchmark from the sidebar.");
    return;
  }

  const metrics = result.categorical_metrics || {};
  const cancer = metrics.cancer_associated || {};
  const citations = metrics.citations || {};
  const judge = result.judge && result.judge.aggregate ? result.judge.aggregate : null;
  const perGene = result.per_gene_report || [];

  elements.runSummary.textContent =
    `${result.n_genes || metrics.n || 0} holdout gene${(result.n_genes || metrics.n) === 1 ? "" : "s"} evaluated.`;

  const wrap = document.createElement("div");
  wrap.className = "benchmark-report";
  wrap.innerHTML = `
    <section class="metric-grid">
      ${renderMetricCard("Cancer Accuracy", formatMetric(cancer.accuracy), "Cohen's kappa", formatMetric(cancer.cohen_kappa))}
      ${renderMetricCard("Citation F1", formatMetric(citations.f1), "Precision / Recall", `${formatMetric(citations.precision)} / ${formatMetric(citations.recall)}`)}
      ${renderMetricCard("Summary Judge", judge ? `${formatMetric(judge.mean_score)}/4` : "Skipped", "Acceptable", judge ? `${formatMetric(judge.acceptable_pct)}%` : "—")}
    </section>
    <section class="benchmark-table-wrap">
      <table class="benchmark-table">
        <thead>
          <tr>
            <th>Gene</th>
            <th>Cancer</th>
            <th>Citation F1</th>
            <th>TP</th>
            <th>FP</th>
            <th>FN</th>
          </tr>
        </thead>
        <tbody>
          ${perGene.map(renderBenchmarkRow).join("")}
        </tbody>
      </table>
    </section>
  `;
  elements.resultsWindow.replaceChildren(wrap);
}

function renderMetricCard(title, value, label, detail) {
  return `
    <article class="metric-card">
      <span>${escapeHtml(title)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(label)}: ${escapeHtml(detail)}</small>
    </article>
  `;
}

function renderBenchmarkRow(row) {
  const cancerMatch = row.pred_cancer_associated === row.gold_cancer_associated;
  return `
    <tr>
      <td>${escapeHtml(row.gene)}</td>
      <td><span class="review-badge ${cancerMatch ? "high" : "medium"}">${escapeHtml(formatBool(row.pred_cancer_associated))} / ${escapeHtml(formatBool(row.gold_cancer_associated))}</span></td>
      <td>${escapeHtml(formatMetric(row.citation_f1))}</td>
      <td>${escapeHtml(formatList(row.citation_tp))}</td>
      <td>${escapeHtml(formatList(row.citation_fp))}</td>
      <td>${escapeHtml(formatList(row.citation_fn))}</td>
    </tr>
  `;
}

function formatFieldValue(value, type) {
  if (type === "boolean" || type === "booleanRequired") {
    if (value === null || value === undefined) return "—";
    return value ? "Yes" : "No";
  }
  if (type === "list") {
    const items = Array.isArray(value) ? value : [];
    return items.length ? items.map((item) => `• ${item}`).join("\n") : "—";
  }
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

// cancer_type_prevalence is a "- entry per line" string (see synthesis
// prompt) — parsed into chips for display so it doesn't read as a raw
// text dump next to the rest of the redesigned card. Editing still
// works on the same underlying string; only the at-rest display differs.
function parseCancerTypeEntries(value) {
  if (!value) return [];
  return String(value)
    .split("\n")
    .map((line) => line.replace(/^[\s•-]+/, "").trim())
    .filter(Boolean);
}

function renderCancerTypeChips(value) {
  const entries = parseCancerTypeEntries(value);
  if (!entries.length) return null;

  const wrap = document.createElement("div");
  wrap.className = "cancer-type-chips";

  entries.forEach((entry) => {
    const match = entry.match(/^(.*?)\s*(\([^)]*\))?$/);
    const main = (match ? match[1] : entry).trim();
    const context = match && match[2] ? match[2] : "";

    const chip = document.createElement("span");
    chip.className = "cancer-type-chip";

    const mainSpan = document.createElement("span");
    mainSpan.className = "cancer-type-chip-main";
    mainSpan.textContent = main;
    chip.appendChild(mainSpan);

    if (context) {
      const contextSpan = document.createElement("span");
      contextSpan.className = "cancer-type-chip-context";
      contextSpan.textContent = ` ${context}`;
      chip.appendChild(contextSpan);
    }

    wrap.appendChild(chip);
  });

  return wrap;
}

// Populates a field-row-value display element — chips for
// cancer_type_prevalence, plain text (via formatFieldValue) otherwise.
function populateFieldDisplay(display, annotation, field, type) {
  if (field === "cancer_type_prevalence") {
    const chips = renderCancerTypeChips(annotation[field]);
    if (chips) {
      display.replaceChildren(chips);
      return;
    }
  }
  display.textContent = formatFieldValue(annotation[field], type);
}

function hasFieldValue(annotation, field) {
  const value = annotation[field];
  return value !== null && value !== undefined && String(value).trim() !== "";
}

function renderLazyField(annotation, field, label, type) {
  const details = document.createElement("details");
  details.className = "field-row lazy-field-row";

  const summary = document.createElement("summary");
  summary.className = "lazy-field-summary";
  summary.innerHTML = `
    <span class="field-row-label">${escapeHtml(label)}</span>
  `;
  details.appendChild(summary);

  const value = document.createElement("div");
  value.className = "field-row-value lazy-field-value";
  if (hasFieldValue(annotation, field)) {
    populateFieldDisplay(value, annotation, field, type);
  } else {
    value.textContent = `Expand to load ${label.toLowerCase()} for ${annotation.gene}.`;
  }
  details.appendChild(value);

  details.addEventListener("toggle", () => {
    if (!details.open || hasFieldValue(annotation, field)) return;
    value.replaceChildren(renderLoadingState(`Loading ${label.toLowerCase()} for ${annotation.gene}`));
    enrichAnnotationOnDemand(annotation, label.toLowerCase())
      .then(() => {
        value.textContent = "Updating...";
      })
      .catch((error) => {
        value.textContent = `Unable to load ${label.toLowerCase()}.`;
        setMessage(formatRunError(error.message), "error");
      });
  });

  return details;
}

// Plain read-only label + value. This card is for reviewing/understanding
// a run, not correcting it yet — inline editing was removed since there's
// no annotation/approval workflow for it to feed into right now. Revisit
// when that workflow exists.
function renderField(annotation, field, label, type) {
  const wrapper = document.createElement("div");
  wrapper.className = "field-row";

  const labelElement = document.createElement("span");
  labelElement.className = "field-row-label";
  labelElement.textContent = label;
  wrapper.appendChild(labelElement);

  const value = document.createElement("div");
  value.className = "field-row-value";
  populateFieldDisplay(value, annotation, field, type);
  wrapper.appendChild(value);

  return wrapper;
}

function renderClinicalActionability(annotation) {
  const actionability = annotation.clinical_actionability;
  if (!actionability) return null;

  const section = document.createElement("div");
  section.className = "clinical-actionability";

  const header = document.createElement("div");
  header.className = "clinical-actionability-header";
  const title = document.createElement("span");
  title.className = "field-label";
  title.textContent = "High-confidence therapeutic precedent";
  header.appendChild(title);

  const score = document.createElement("span");
  score.className = "review-badge high";
  const scoreValue = Number(actionability.confidence_score || 0);
  score.textContent = `${Math.round(scoreValue * 100)}% confidence`;
  score.title = actionability.confidence_explanation || "";
  header.appendChild(score);
  section.appendChild(header);

  const summary = document.createElement("p");
  summary.className = "clinical-actionability-summary";
  summary.textContent = actionability.summary || "";
  section.appendChild(summary);

  // actionability.evidence carries each supporting pmid's abstract (see
  // clinical_actionability.py's evidence_entries); reuse it for the same
  // hover tooltip the "Cited on PubMed" pills use.
  const abstractByPmid = new Map(
    (actionability.evidence || []).filter((e) => e.abstract).map((e) => [e.pmid, e.abstract])
  );

  if (actionability.pmids?.length) {
    const links = document.createElement("div");
    links.className = "citation-link-list";
    actionability.pmids.forEach((pmid) => links.appendChild(makePubMedLink(pmid, abstractByPmid.get(pmid))));
    section.appendChild(links);
  }

  const breakdown = renderClinicalActionabilityBreakdown(actionability, abstractByPmid);
  if (breakdown) section.appendChild(breakdown);

  if (actionability.confidence_explanation) {
    const explanation = document.createElement("div");
    explanation.className = "subtle";
    explanation.textContent = actionability.confidence_explanation;
    section.appendChild(explanation);
  }

  return section;
}

function renderClinicalActionabilityBreakdown(actionability, abstractByPmid) {
  const components = actionability.score_components || [];
  if (!components.length) return null;

  const details = document.createElement("details");
  details.className = "clinical-actionability-breakdown";

  const summary = document.createElement("summary");
  summary.className = "clinical-actionability-breakdown-summary";
  summary.textContent = "Score derivation";
  details.appendChild(summary);

  const list = document.createElement("div");
  list.className = "clinical-actionability-component-list";
  components.forEach((component) => {
    const row = document.createElement("div");
    row.className = "clinical-actionability-component";

    const delta = document.createElement("span");
    delta.className = `clinical-actionability-delta ${component.delta < 0 ? "negative" : "positive"}`;
    delta.textContent = `${component.delta < 0 ? "" : "+"}${Number(component.delta || 0).toFixed(2)}`;
    row.appendChild(delta);

    const body = document.createElement("div");
    body.className = "clinical-actionability-component-body";
    const label = document.createElement("div");
    label.className = "clinical-actionability-component-label";
    label.textContent = component.label || component.code || "Score component";
    body.appendChild(label);

    if (component.detail) {
      const detail = document.createElement("div");
      detail.className = "subtle";
      detail.textContent = component.detail;
      body.appendChild(detail);
    }

    if (component.pmids?.length) {
      const pmids = document.createElement("div");
      pmids.className = "citation-link-list";
      component.pmids.forEach((pmid) => pmids.appendChild(makePubMedLink(pmid, abstractByPmid.get(pmid))));
      body.appendChild(pmids);
    }

    row.appendChild(body);
    list.appendChild(row);
  });
  details.appendChild(list);
  return details;
}

// Compact Yes/No status for the two booleans curators check first —
// plain colored text next to the header, not a form field.
function renderStatusLine(annotation) {
  const wrapper = document.createElement("div");
  wrapper.className = "status-line";

  for (const [field, label] of statusFields) {
    const value = annotation[field];
    const text = value === null || value === undefined ? "—" : value ? "Yes" : "No";
    const item = document.createElement("span");
    item.className =
      "status-value " +
      (value === true ? "status-yes" : value === false ? "status-no" : "status-unknown");
    item.textContent = `${label}: ${text}`;
    wrapper.appendChild(item);
  }

  (annotation.quality_flags || []).forEach((flag) => {
    const badge = document.createElement("span");
    const severity = ["info", "warning", "critical"].includes(flag.severity)
      ? flag.severity
      : "info";
    badge.className = `review-badge quality-${severity}`;
    badge.title = flag.code === "low_evidence_score"
      ? annotation.evidence_support_explanation || flag.detail || ""
      : flag.detail || "";
    badge.textContent = flag.label || flag.code;
    wrapper.appendChild(badge);
  });

  return wrapper;
}

function renderCacheBadge(annotation) {
  const status = annotation.cache_status;
  const reason = annotation.cache_reason;
  if (!status && !reason) return null;

  const badge = document.createElement("span");
  badge.className = "review-badge cache-badge";
  const labelByReason = {
    fresh_final_annotation: "Cached final",
    no_new_pubmed_pmids_since_last_check: "Cached after PubMed check",
    force_refresh: "Force refreshed",
    computed_new_annotation: "Freshly computed",
    cache_miss_or_stale: "Freshly computed",
  };
  const labelByStatus = {
    reused: "Cached",
    refreshed: "Freshly computed",
    bypassed: "Cache bypassed",
  };
  badge.textContent = labelByReason[reason] || labelByStatus[status] || "Cache status";
  badge.title = [status, reason].filter(Boolean).join(": ");
  return badge;
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function exportCsv() {
  if (!state.currentResult?.annotations?.length) return;
  const blob = new Blob([annotationResultToCsv(state.currentResult)], {
    type: "text/csv;charset=utf-8",
  });
  downloadBlob(blob, "annotation_results.csv");
}

function exportJson() {
  const payload = state.currentView === "benchmark" ? state.currentBenchmark : state.currentResult;
  if (!payload) return;
  const filename = state.currentView === "benchmark" ? "benchmark_report.json" : "results.json";
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  downloadBlob(blob, filename);
}

function exportPdf() {
  const payload = state.currentView === "benchmark" ? state.currentBenchmark : state.currentResult;
  if (!payload) return;
  const filename = state.currentView === "benchmark" ? "benchmark_report.pdf" : "annotation_results.pdf";
  downloadBlob(buildPdfBlob(buildPdfLines(payload)), filename);
}

async function exportSelectedFormat() {
  const format = elements.exportFormat.value;
  if (format === "json") {
    exportJson();
    return;
  }
  if (format === "csv") {
    if (state.currentView === "benchmark") {
      throw new Error("CSV export is available for annotation results only.");
    }
    await exportCsv();
    return;
  }
  if (format === "pdf") {
    exportPdf();
  }
}

const CSV_HEADERS = [
  "gene",
  "fusions",
  "in_oncokb",
  "cancer_associated",
  "cancer_association_rationale",
  "cancer_type_prevalence",
  "gene_class",
  "signaling_pathways",
  "gene_summary",
  "citations",
  "publication_links",
  "date_annotated",
  "retrieval_count",
  "insufficient_evidence",
  "evidence_support_score",
  "evidence_support_explanation",
  "clinical_actionability_score",
  "clinical_actionability_summary",
  "clinical_actionability_pmids",
  "clinical_actionability_explanation",
  "clinical_actionability_score_components",
  "quality_flags",
  "evidence_card_count",
  "cache_status",
  "cache_reason",
  "cached_at",
  "last_pubmed_checked_at",
  "error",
];

function uniqueValues(values) {
  const seen = new Set();
  return (values || []).filter((value) => {
    if (seen.has(value)) return false;
    seen.add(value);
    return true;
  });
}

function csvCell(value) {
  const text = Array.isArray(value) ? value.join("; ") : String(value ?? "");
  return `"${text.replaceAll('"', '""')}"`;
}

function formatClinicalActionabilityComponents(annotation) {
  const components = annotation.clinical_actionability?.score_components || [];
  return components
    .map((component) => {
      const delta = component.delta === undefined ? "" : `${component.delta >= 0 ? "+" : ""}${Number(component.delta).toFixed(2)}`;
      const pmids = component.pmids?.length ? ` PMID(s): ${component.pmids.join(", ")}` : "";
      const detail = component.detail ? ` ${component.detail}` : "";
      return `${delta} ${component.label || ""}.${detail}${pmids}`.trim();
    })
    .filter(Boolean)
    .join(" | ");
}

function annotationToCsvRow(annotation) {
  const citations = uniqueValues(annotation.citations || []);
  const actionability = annotation.clinical_actionability;
  return {
    gene: annotation.gene,
    fusions: uniqueValues(annotation.fusions || []).join("; "),
    in_oncokb:
      annotation.in_oncokb === null || annotation.in_oncokb === undefined
        ? ""
        : formatBool(annotation.in_oncokb),
    cancer_associated:
      annotation.cancer_associated === null || annotation.cancer_associated === undefined
        ? ""
        : formatBool(annotation.cancer_associated),
    cancer_association_rationale: annotation.cancer_association_rationale,
    cancer_type_prevalence: annotation.cancer_type_prevalence,
    gene_class: annotation.gene_class,
    signaling_pathways: annotation.signaling_pathways,
    gene_summary: annotation.gene_summary,
    citations: citations.join("; "),
    publication_links: citations.map((pmid) => `https://pubmed.ncbi.nlm.nih.gov/${pmid}/`).join("; "),
    date_annotated: annotation.date_annotated,
    retrieval_count: annotation.retrieval_count,
    insufficient_evidence: formatBool(annotation.insufficient_evidence),
    evidence_support_score: annotation.evidence_support_score,
    evidence_support_explanation: annotation.evidence_support_explanation,
    clinical_actionability_score: actionability?.confidence_score ?? "",
    clinical_actionability_summary: actionability?.summary || "",
    clinical_actionability_pmids: (actionability?.pmids || []).join("; "),
    clinical_actionability_explanation: actionability?.confidence_explanation || "",
    clinical_actionability_score_components: formatClinicalActionabilityComponents(annotation),
    quality_flags: (annotation.quality_flags || []).map((flag) => flag.code).join("; "),
    evidence_card_count: (annotation.evidence_cards || []).length,
    cache_status: annotation.cache_status,
    cache_reason: annotation.cache_reason,
    cached_at: annotation.cached_at,
    last_pubmed_checked_at: annotation.last_pubmed_checked_at,
    error: annotation.error,
  };
}

function annotationResultToCsv(result) {
  const rows = (result.annotations || []).map(annotationToCsvRow);
  return [
    CSV_HEADERS.join(","),
    ...rows.map((row) => CSV_HEADERS.map((header) => csvCell(row[header])).join(",")),
  ].join("\n");
}

function buildPdfLines(payload) {
  return state.currentView === "benchmark"
    ? buildBenchmarkPdfLines(payload)
    : buildAnnotationPdfLines(payload);
}

function buildAnnotationPdfLines(result) {
  const lines = [
    "Cancer Gene Classification Results",
    `Run ID: ${result.run_id || ""}`,
    `Timestamp: ${result.timestamp || ""}`,
    `Inputs processed: ${result.fusions_processed ?? ""}`,
    `Genes annotated: ${result.genes_annotated ?? ""}`,
    "",
  ];

  (result.annotations || []).forEach((annotation) => {
    lines.push(
      annotation.gene || "Unknown gene",
      `Inputs: ${formatList(annotation.fusions)}`,
      `In OncoKB: ${formatBool(annotation.in_oncokb)}`,
      `Cancer associated: ${formatBool(annotation.cancer_associated)}`,
      `Evidence support: ${annotation.evidence_support_score ?? ""}`,
      `Clinical actionability: ${annotation.clinical_actionability?.confidence_score ?? ""}`,
      `Gene class: ${annotation.gene_class || ""}`,
      `Signaling pathways: ${annotation.signaling_pathways || ""}`,
      `Insufficient evidence: ${formatBool(annotation.insufficient_evidence)}`,
      `Citations: ${formatList(uniqueValues(annotation.citations || []))}`,
      `Rationale: ${annotation.cancer_association_rationale || ""}`,
      `Gene summary: ${annotation.gene_summary || ""}`,
      "",
    );
  });
  return lines;
}

function buildBenchmarkPdfLines(result) {
  const metrics = result.categorical_metrics || {};
  const cancer = metrics.cancer_associated || {};
  const citations = metrics.citations || {};
  const judge = result.judge && result.judge.aggregate ? result.judge.aggregate : null;
  const lines = [
    "Benchmark Report",
    `Genes evaluated: ${result.n_genes || metrics.n || 0}`,
    `Cancer accuracy: ${formatMetric(cancer.accuracy)}`,
    `Cancer Cohen's kappa: ${formatMetric(cancer.cohen_kappa)}`,
    `Citation F1: ${formatMetric(citations.f1)}`,
    `Citation precision / recall: ${formatMetric(citations.precision)} / ${formatMetric(citations.recall)}`,
    `Summary judge: ${judge ? `${formatMetric(judge.mean_score)}/4` : "Skipped"}`,
    "",
  ];

  (result.per_gene_report || []).forEach((row) => {
    lines.push(
      `${row.gene || "Unknown gene"}: cancer ${formatBool(row.pred_cancer_associated)} / ${formatBool(row.gold_cancer_associated)}, citation F1 ${formatMetric(row.citation_f1)}`,
      `TP: ${formatList(row.citation_tp)}`,
      `FP: ${formatList(row.citation_fp)}`,
      `FN: ${formatList(row.citation_fn)}`,
      "",
    );
  });
  return lines;
}

function sanitizePdfText(value) {
  return String(value ?? "")
    .replace(/[^\x09\x0A\x0D\x20-\x7E]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function wrapPdfLine(line, maxChars = 94) {
  const text = sanitizePdfText(line);
  if (!text) return [""];
  const words = text.split(" ");
  const lines = [];
  let current = "";
  words.forEach((word) => {
    const next = current ? `${current} ${word}` : word;
    if (next.length > maxChars && current) {
      lines.push(current);
      current = word;
    } else {
      current = next;
    }
  });
  if (current) lines.push(current);
  return lines;
}

function escapePdfString(value) {
  return sanitizePdfText(value)
    .replaceAll("\\", "\\\\")
    .replaceAll("(", "\\(")
    .replaceAll(")", "\\)");
}

function buildPdfBlob(rawLines) {
  const lines = rawLines.flatMap((line) => wrapPdfLine(line));
  const pageLineCount = 52;
  const pages = [];
  for (let i = 0; i < lines.length; i += pageLineCount) {
    pages.push(lines.slice(i, i + pageLineCount));
  }
  if (!pages.length) pages.push(["No exportable data."]);

  const objects = [];
  const fontObjectNumber = 3;
  const pageObjectNumbers = pages.map((_, index) => 4 + index * 2);
  const contentObjectNumbers = pages.map((_, index) => 5 + index * 2);

  objects[1] = "<< /Type /Catalog /Pages 2 0 R >>";
  objects[2] = `<< /Type /Pages /Kids [${pageObjectNumbers.map((n) => `${n} 0 R`).join(" ")}] /Count ${pages.length} >>`;
  objects[fontObjectNumber] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>";

  pages.forEach((pageLines, index) => {
    const pageNumber = pageObjectNumbers[index];
    const contentNumber = contentObjectNumbers[index];
    const commands = [
      "BT",
      "/F1 10 Tf",
      "14 TL",
      "50 770 Td",
      ...pageLines.map((line, lineIndex) =>
        `${lineIndex === 0 ? "" : "T* "}(${escapePdfString(line)}) Tj`.trim(),
      ),
      "ET",
    ];
    const stream = commands.join("\n");
    objects[pageNumber] =
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] ` +
      `/Resources << /Font << /F1 ${fontObjectNumber} 0 R >> >> /Contents ${contentNumber} 0 R >>`;
    objects[contentNumber] = `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`;
  });

  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  for (let i = 1; i < objects.length; i += 1) {
    offsets[i] = pdf.length;
    pdf += `${i} 0 obj\n${objects[i]}\nendobj\n`;
  }
  const xrefOffset = pdf.length;
  pdf += `xref\n0 ${objects.length}\n0000000000 65535 f \n`;
  for (let i = 1; i < objects.length; i += 1) {
    pdf += `${String(offsets[i]).padStart(10, "0")} 00000 n \n`;
  }
  pdf += `trailer\n<< /Size ${objects.length} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`;
  return new Blob([pdf], { type: "application/pdf" });
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function formatList(value) {
  if (!Array.isArray(value)) return value || "";
  return value.join("; ");
}

function formatMetric(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  return number.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

function formatBool(value) {
  if (value === true) return "TRUE";
  if (value === false) return "FALSE";
  return "—";
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// For use in HTML attribute values (already escapes quotes)
function escapeAttr(value) {
  return escapeHtml(value);
}

// ---------------------------------------------------------------------------
// Event binding
// ---------------------------------------------------------------------------

function bindEvents() {
  elements.runButton.addEventListener("click", runAnnotation);
  elements.runBenchmarkButton.addEventListener("click", runBenchmark);
  elements.navAnnotate.addEventListener("click", () => switchView("annotate"));
  elements.navBenchmark.addEventListener("click", () => switchView("benchmark"));

  elements.tabSingle.addEventListener("click", () => switchMode("single"));
  elements.tabBatch.addEventListener("click", () => switchMode("batch"));

  elements.tabResultsView.addEventListener("click", () => switchResultsView("results"));
  elements.tabFusionEvidenceView.addEventListener("click", () => switchResultsView("fusion"));
  elements.tabNoResultView.addEventListener("click", () => switchResultsView("noresult"));

  elements.openFeedback.addEventListener("click", () => showFeedbackModal());
  elements.closeFeedback.addEventListener("click", () => hideFeedbackModal());
  elements.submitFeedback.addEventListener("click", submitFeedbackForm);

  document.querySelectorAll(".input-examples, .input-examples-compact").forEach((container) => {
    container.addEventListener("click", (event) => {
      const button = event.target.closest(".example-pill");
      if (!button) return;
      applyInputExample(button.dataset.exampleId, button.dataset.exampleMode);
    });
  });

  elements.addFusionBtn.addEventListener("click", addToQueue);
  elements.clearQueueBtn.addEventListener("click", clearQueue);

  elements.singleFusion.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addToQueue();
    }
  });

  // Batch grid — event delegation
  elements.batchGridBody.addEventListener("input", handleGridInput);
  elements.batchGridBody.addEventListener("keydown", handleGridKeydown);
  elements.batchGridBody.addEventListener("paste", handleGridPaste);
  elements.batchGridBody.addEventListener("click", handleGridClick);

  elements.addRowBtn.addEventListener("click", () => {
    state.batchRows.push(emptyRow());
    renderGrid({ row: state.batchRows.length - 1, col: 0 });
  });

  elements.batchUploadBtn.addEventListener("click", () => elements.batchUploadInput.click());
  elements.batchUploadInput.addEventListener("change", handleBatchFileUpload);

  elements.shareRun.addEventListener("click", copyShareLink);
  elements.clearResults.addEventListener("click", clearResults);
  elements.exportButton.addEventListener("click", async () => {
    try {
      await exportSelectedFormat();
    } catch (error) {
      setMessage(error.message, "error");
    }
  });
  elements.exportFormat.addEventListener("change", updateExportState);
}

initSidebarResize();
bindEvents();
renderGrid();
updateExportState();
loadDevStatus();
loadSharedRun();
checkAuth();
