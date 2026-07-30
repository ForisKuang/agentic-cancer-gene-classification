const API_BASE_URL_KEY = "agcg.apiBaseUrl";
const ONCOKB_TOKEN_KEY = "agcg.oncokbToken";
const NCBI_KEY_KEY = "agcg.ncbiApiKey";

const GRID_COLUMNS = [
  { key: "fusion",           label: "Fusion",        required: true,  type: "text",   width: 140 },
  { key: "tumor_type",       label: "Tumor Type",    required: false, type: "text",   width: 110 },
  { key: "five_exon",        label: "5′ Exon",       required: false, type: "number", width: 62 },
  { key: "three_exon",       label: "3′ Exon",       required: false, type: "number", width: 62 },
  { key: "five_genomic",     label: "5′ Genomic",    required: false, type: "text",   width: 100 },
  { key: "three_genomic",    label: "3′ Genomic",    required: false, type: "text",   width: 100 },
  { key: "five_transcript",  label: "5′ Transcript", required: false, type: "text",   width: 105 },
  { key: "three_transcript", label: "3′ Transcript", required: false, type: "text",   width: 105 },
];

function emptyRow() {
  return GRID_COLUMNS.reduce((acc, col) => ({ ...acc, [col.key]: "" }), {});
}

const state = {
  isRunning: false,
  isBenchmarkRunning: false,
  currentResult: null,
  currentBenchmark: null,
  currentView: "annotate",
  inputMode: "single",
  queue: [],
  batchRows: Array.from({ length: 5 }, emptyRow),
};

const elements = {
  annotatePanel: document.querySelector("#annotate-panel"),
  apiBaseUrlInput: document.querySelector("#api-base-url-input"),
  apiBaseUrlModal: document.querySelector("#api-base-url-modal"),
  benchmarkJudge: document.querySelector("#benchmark-judge"),
  benchmarkLocalBackend: document.querySelector("#benchmark-local-backend"),
  benchmarkMaxGenes: document.querySelector("#benchmark-max-genes"),
  benchmarkPanel: document.querySelector("#benchmark-panel"),
  saveApiUrl: document.querySelector("#save-api-url"),
  saveApiUrlModal: document.querySelector("#save-api-url-modal"),
  closeSetup: document.querySelector("#close-setup"),
  dismissSetup: document.querySelector("#dismiss-setup"),
  exportCsv: document.querySelector("#export-csv"),
  exportJson: document.querySelector("#export-json"),
  geneIndex: document.querySelector("#gene-index"),
  installOutput: document.querySelector("#install-output"),
  messageBox: document.querySelector("#message-box"),
  ncbiApiKeyInput: document.querySelector("#ncbi-api-key-input"),
  ncbiStatus: document.querySelector("#ncbi-status"),
  navAnnotate: document.querySelector("#nav-annotate"),
  navBenchmark: document.querySelector("#nav-benchmark"),
  oncokbStatus: document.querySelector("#oncokb-status"),
  oncokbTokenInput: document.querySelector("#oncokb-token-input"),
  openSetup: document.querySelector("#open-setup"),
  resultsWindow: document.querySelector("#results-window"),
  runButton: document.querySelector("#run-button"),
  runBenchmarkButton: document.querySelector("#run-benchmark-button"),
  runSummary: document.querySelector("#run-summary"),
  saveNcbiApiKey: document.querySelector("#save-ncbi-api-key"),
  saveOncokbToken: document.querySelector("#save-oncokb-token"),
  setupModal: document.querySelector("#setup-modal"),
  setupSummary: document.querySelector("#setup-summary"),
  workspaceTitle: document.querySelector("#workspace-title"),
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
  // dev-mode annotation backend
  annotateBackendField: document.querySelector("#annotate-backend-field"),
  annotateLocalBackend: document.querySelector("#annotate-local-backend"),
};

const editableFields = [
  ["in_oncokb", "In OncoKB", "boolean"],
  ["cancer_associated", "Cancer associated", "boolean"],
  ["cancer_association_rationale", "Rationale", "long"],
  ["cancer_type_prevalence", "Cancer type prevalence", "text"],
  ["gene_class", "Gene class", "text"],
  ["signaling_pathways", "Signaling pathways", "text"],
  ["gene_summary", "Gene summary", "long"],
  ["citations", "Supporting citation PMIDs", "list"],
  ["insufficient_evidence", "Insufficient evidence", "booleanRequired"],
];

// ---------------------------------------------------------------------------
// API URL helpers
// ---------------------------------------------------------------------------

function getApiBaseUrl() {
  return (localStorage.getItem(API_BASE_URL_KEY) || "").replace(/\/$/, "");
}

function apiUrl(path) {
  const base = getApiBaseUrl();
  return base ? base + path : path;
}

// ---------------------------------------------------------------------------
// App section switching
// ---------------------------------------------------------------------------

async function loadDevStatus() {
  try {
    const response = await fetch(apiUrl("/v1/dev/status"));
    if (!response.ok) {
      elements.navBenchmark.classList.add("hidden");
      elements.annotateBackendField.classList.add("hidden");
      if (state.currentView === "benchmark") switchView("annotate");
      return;
    }
    const payload = await response.json();
    elements.navBenchmark.classList.toggle("hidden", !payload.enabled);
    elements.annotateBackendField.classList.toggle("hidden", !payload.enabled);
    if (!payload.enabled && state.currentView === "benchmark") switchView("annotate");
  } catch {
    elements.navBenchmark.classList.add("hidden");
    elements.annotateBackendField.classList.add("hidden");
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
  elements.exportCsv.classList.toggle("hidden", isBenchmark);
  elements.exportJson.disabled = isBenchmark ? !state.currentBenchmark : !state.currentResult;

  if (isBenchmark) {
    renderBenchmarkResult(state.currentBenchmark);
  } else if (state.currentResult) {
    renderAnnotationResult(state.currentResult);
  } else {
    renderEmptyState("No results yet", "Paste fusions, then click Run to annotate.");
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
    setMessage("Enter a fusion name before adding to queue.", "error");
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

function renderQueue() {
  const count = state.queue.length;
  elements.fusionQueue.classList.toggle("hidden", count === 0);
  elements.queueCount.textContent = `${count} fusion${count === 1 ? "" : "s"} queued`;
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
    ? `${filled} fusion${filled === 1 ? "" : "s"} ready.`
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

function parseFusions() {
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
// Settings persistence
// ---------------------------------------------------------------------------

function loadSettings() {
  const savedUrl = getApiBaseUrl();
  elements.apiBaseUrlInput.value = savedUrl;
  elements.apiBaseUrlModal.value = savedUrl;

  const hasOncokb = Boolean(localStorage.getItem(ONCOKB_TOKEN_KEY));
  const hasNcbi = Boolean(localStorage.getItem(NCBI_KEY_KEY));
  elements.oncokbStatus.textContent = hasOncokb
    ? "OncoKB token is saved locally."
    : "Not configured. Paste a token to enable OncoKB membership lookup.";
  elements.ncbiStatus.textContent = hasNcbi
    ? "NCBI API key is saved locally."
    : "Recommended. Add an NCBI API key to reduce PubMed rate-limit delays.";
  elements.setupSummary.textContent = savedUrl
    ? `API: ${savedUrl}`
    : "Using same-host API.";
}

function saveApiUrl(url) {
  const trimmed = url.trim().replace(/\/$/, "");
  if (trimmed) {
    localStorage.setItem(API_BASE_URL_KEY, trimmed);
  } else {
    localStorage.removeItem(API_BASE_URL_KEY);
  }
  elements.apiBaseUrlInput.value = trimmed;
  elements.apiBaseUrlModal.value = trimmed;
  elements.setupSummary.textContent = trimmed ? `API: ${trimmed}` : "Using same-host API.";
  setInstallOutput("Backend URL saved", trimmed || "Using same-host API.");
  loadDevStatus();
}

function saveOncoKBToken() {
  const token = elements.oncokbTokenInput.value.trim();
  if (!token) {
    setInstallOutput("OncoKB token not entered", "Paste a token before saving.", "error");
    return;
  }
  localStorage.setItem(ONCOKB_TOKEN_KEY, token);
  elements.oncokbTokenInput.value = "";
  elements.oncokbStatus.textContent = "OncoKB token is saved locally.";
  setInstallOutput("OncoKB token saved", "The token will be sent with each annotation request.");
}

function saveNCBIApiKey() {
  const key = elements.ncbiApiKeyInput.value.trim();
  if (!key) {
    setInstallOutput("NCBI API key not entered", "Paste a key before saving.", "error");
    return;
  }
  localStorage.setItem(NCBI_KEY_KEY, key);
  elements.ncbiApiKeyInput.value = "";
  elements.ncbiStatus.textContent = "NCBI API key is saved locally.";
  setInstallOutput("NCBI API key saved", "The key will be sent with each annotation request.");
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

function setBenchmarkRunning(isRunning) {
  state.isBenchmarkRunning = isRunning;
  elements.runBenchmarkButton.textContent = isRunning ? "Running..." : "Run Benchmark";
  elements.runBenchmarkButton.disabled = isRunning;
}

function showSetupModal() {
  elements.setupModal.classList.remove("hidden");
}

function hideSetupModal() {
  elements.setupModal.classList.add("hidden");
}

function setInstallOutput(title, body, type = "info") {
  elements.installOutput.className = "install-output";
  elements.installOutput.innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <p>${escapeHtml(body)}</p>
  `;
  elements.installOutput.classList.toggle("error", type === "error");
  elements.installOutput.classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// Annotation run
// ---------------------------------------------------------------------------

async function runAnnotation() {
  const fusionInputs = parseFusions();
  if (!fusionInputs.length) {
    setMessage(
      state.inputMode === "single"
        ? "Add at least one fusion to the queue before running."
        : "Enter at least one fusion in the Fusion column before running.",
      "error"
    );
    return;
  }

  // Collapse the expanded batch sidebar so results have room
  document.body.classList.remove("batch-active");

  setRunning(true);
  clearMessage();
  setMessage(`Submitting ${fusionInputs.length} fusion${fusionInputs.length === 1 ? "" : "s"} for annotation…`, "info");

  try {
    const localBackend = elements.annotateLocalBackend.value || undefined;
    const body = { fusions: fusionInputs };
    if (localBackend) body.local_backend = localBackend;
    const response = await fetch(apiUrl("/v1/annotate"), {
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

    const result = await response.json();
    state.currentResult = result;
    renderAnnotationResult(result);
    setMessage("Annotation complete. Review fields before exporting.", "info");
  } catch (error) {
    setMessage(formatRunError(error.message), "error");
  } finally {
    setRunning(false);
  }
}

function formatRunError(message) {
  if (String(message).includes("ONCOKB_API_TOKEN")) {
    return "OncoKB API token is required. Open Settings and configure your OncoKB token.";
  }
  return message;
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
    const response = await fetch(apiUrl("/v1/dev/benchmark"), {
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
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <p>${escapeHtml(body)}</p>
  `;
  elements.resultsWindow.replaceChildren(empty);
  rebuildGeneIndex([]);
}

function renderAnnotationResult(result) {
  const annotations = result.annotations || [];
  const hasAnnotations = annotations.length > 0;
  elements.exportCsv.disabled = !hasAnnotations;
  elements.exportJson.disabled = !hasAnnotations;

  const total = result.genes_annotated;
  elements.runSummary.textContent =
    `${total} gene${total === 1 ? "" : "s"} annotated from ` +
    `${result.fusions_processed} fusion${result.fusions_processed === 1 ? "" : "s"}.`;

  if (!hasAnnotations) {
    renderEmptyState(
      result.run_error ? "Run stopped" : "No results",
      result.run_error || "No gene annotations were returned.",
    );
    return;
  }

  const list = document.createElement("div");
  list.className = "annotation-list";

  annotations.forEach((annotation, index) => {
    const card = document.createElement("article");
    card.className = "annotation-card";
    card.id = `gene-${annotation.gene}`;
    card.innerHTML = `
      <header>
        <div>
          <h3>${escapeHtml(annotation.gene)}</h3>
          <div class="subtle">${escapeHtml(formatList(annotation.fusions))}</div>
          <div class="review-badges">${renderCompactBadges(annotation)}</div>
        </div>
        <span class="status-pill">${escapeHtml(annotation.date_annotated || "")}</span>
      </header>
      <div class="annotation-fields"></div>
    `;

    const fields = card.querySelector(".annotation-fields");
    for (const [field, label, type] of editableFields) {
      fields.appendChild(renderEditableField(annotation, index, field, label, type));
    }

    const evidence = renderSupportingEvidence(annotation);
    if (evidence) card.appendChild(evidence);

    list.appendChild(card);
  });

  elements.resultsWindow.replaceChildren(list);
  rebuildGeneIndex(annotations);
}

function makePubMedLink(pmid) {
  const a = document.createElement("a");
  a.href = `https://pubmed.ncbi.nlm.nih.gov/${encodeURIComponent(pmid)}/`;
  a.target = "_blank";
  a.rel = "noreferrer";
  a.className = "citation-link";
  a.textContent = pmid;
  return a;
}

function renderSupportingEvidence(annotation) {
  const quotes = annotation.supporting_quotes || [];
  const citations = annotation.citations || [];
  const allRetrieved = annotation.retrieved_pmids || [];
  const count = annotation.retrieval_count ?? allRetrieved.length;

  if (!quotes.length && !citations.length && !count) return null;

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
      <span class="retrieval-info-icon" title="Total number of PubMed abstracts fetched during literature retrieval for this gene. A subset of these was selected for synthesis; the PMIDs used in the final annotation appear in the Supporting Citation PMIDs field above.">ⓘ</span>
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

  // Cited PMIDs as PubMed links
  if (citations.length) {
    const citBlock = document.createElement("div");
    citBlock.className = "citation-links";
    const label = document.createElement("span");
    label.className = "field-label";
    label.textContent = "Cited on PubMed";
    citBlock.appendChild(label);
    const linkRow = document.createElement("div");
    linkRow.className = "citation-link-list";
    citations.forEach((pmid) => linkRow.appendChild(makePubMedLink(pmid)));
    citBlock.appendChild(linkRow);
    section.appendChild(citBlock);
  }

  // Grounding quotes
  if (quotes.length) {
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
    section.appendChild(quoteList);
  }

  return section;
}

function renderBenchmarkResult(result) {
  elements.exportCsv.disabled = true;
  elements.exportJson.disabled = !result;
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

function reviewPriority(annotation) {
  if (annotation.insufficient_evidence) {
    return {
      label: "Low priority",
      tone: "low",
      title: "Retrieved evidence was insufficient for a confident cancer annotation.",
    };
  }
  if (annotation.cancer_associated === false) {
    return {
      label: "Low priority",
      tone: "low",
      title: "Current evidence does not support a cancer association.",
    };
  }
  if (annotation.in_oncokb) {
    return {
      label: "High priority",
      tone: "high",
      title: "This gene is already represented in OncoKB and may need curator attention.",
    };
  }
  if (annotation.cancer_associated === true) {
    return { label: "Review", tone: "neutral", title: "Review this result before export." };
  }
  return { label: "Review", tone: "neutral", title: "Review this result before export." };
}

function evidenceSignal(annotation) {
  if (annotation.insufficient_evidence) {
    return {
      label: "Insufficient evidence",
      tone: "low",
      title: "The model did not find enough grounded evidence to classify this gene.",
    };
  }
  if (annotation.cancer_associated === false) {
    return {
      label: "No cancer evidence",
      tone: "low",
      title: "Current retrieved evidence does not support a cancer association.",
    };
  }
  if (annotation.cancer_associated === true) {
    return {
      label: "Cancer associated",
      tone: "neutral",
      title: "Literature supports a cancer association.",
    };
  }
  return null;
}

function compactBadges(annotation) {
  return [
    reviewPriority(annotation),
    evidenceSignal(annotation),
    annotation.in_oncokb
      ? { label: "OncoKB", tone: "high", title: "OncoKB membership lookup returned true." }
      : null,
  ].filter(Boolean);
}

function renderCompactBadges(annotation) {
  return compactBadges(annotation)
    .map(
      (badge) => `
        <span class="review-badge ${escapeHtml(badge.tone)}" title="${escapeHtml(badge.title)}">
          ${escapeHtml(badge.label)}
        </span>
      `,
    )
    .join("");
}

function renderEditableField(annotation, index, field, label, type) {
  const wrapper = document.createElement("label");
  wrapper.className = type === "long" || type === "list" ? "field wide" : "field";
  const labelElement = document.createElement("span");
  labelElement.textContent = label;
  wrapper.appendChild(labelElement);

  let control;
  if (type === "boolean" || type === "booleanRequired") {
    control = document.createElement("select");
    if (type === "boolean") {
      control.appendChild(new Option("", ""));
    }
    control.appendChild(new Option("TRUE", "true"));
    control.appendChild(new Option("FALSE", "false"));
    control.value =
      annotation[field] === null || annotation[field] === undefined
        ? ""
        : String(Boolean(annotation[field]));
  } else if (type === "long" || type === "list") {
    control = document.createElement("textarea");
    control.rows = type === "long" ? 4 : 2;
    control.value =
      type === "list" ? formatList(annotation[field]) : annotation[field] || "";
  } else {
    control = document.createElement("input");
    control.type = type === "number" ? "number" : "text";
    if (field === "confidence") {
      control.step = "0.01";
      control.min = "0";
      control.max = "1";
    }
    control.value =
      annotation[field] === null || annotation[field] === undefined ? "" : annotation[field];
  }

  control.dataset.index = index;
  control.dataset.field = field;
  control.dataset.type = type;
  control.addEventListener("input", handleAnnotationEdit);
  wrapper.appendChild(control);
  return wrapper;
}

function handleAnnotationEdit(event) {
  if (!state.currentResult) return;
  const { index, field, type } = event.target.dataset;
  const annotation = state.currentResult.annotations[Number(index)];
  let value = event.target.value;

  if (type === "boolean" || type === "booleanRequired") {
    value = value === "" ? null : value === "true";
  } else if (type === "list") {
    value = value
      .split(";")
      .map((item) => item.trim())
      .filter(Boolean);
  } else if (type === "number") {
    value = value === "" ? 0 : Number(value);
  }

  annotation[field] = value;
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
  if (!state.currentResult) return;
  const response = await fetch(apiUrl("/v1/export/annotation-results.csv"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(state.currentResult),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || "CSV export failed");
  }
  const blob = await response.blob();
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
  elements.openSetup.addEventListener("click", () => showSetupModal());
  elements.closeSetup.addEventListener("click", () => hideSetupModal());
  elements.dismissSetup.addEventListener("click", () => hideSetupModal());

  elements.tabSingle.addEventListener("click", () => switchMode("single"));
  elements.tabBatch.addEventListener("click", () => switchMode("batch"));

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

  elements.saveApiUrl.addEventListener("click", () =>
    saveApiUrl(elements.apiBaseUrlInput.value),
  );
  elements.saveApiUrlModal.addEventListener("click", () =>
    saveApiUrl(elements.apiBaseUrlModal.value),
  );
  elements.saveOncokbToken.addEventListener("click", saveOncoKBToken);
  elements.saveNcbiApiKey.addEventListener("click", saveNCBIApiKey);

  elements.exportJson.addEventListener("click", exportJson);
  elements.exportCsv.addEventListener("click", async () => {
    try {
      await exportCsv();
    } catch (error) {
      setMessage(error.message, "error");
    }
  });
}

bindEvents();
renderGrid();
loadSettings();
loadDevStatus();
