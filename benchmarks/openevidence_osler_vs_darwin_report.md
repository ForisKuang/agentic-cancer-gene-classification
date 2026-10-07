# OpenEvidence osler versus darwin: live sidecar benchmark

## Recommendation

**Recommend `openevidence_model=osler` for both live sidecar first lookups and cache warming, using one cache namespace.** Retain darwin later as an explicit opt-in deep-review path with its own cache slot. The default change belongs in a separate decision; this PR leaves the application default at `darwin` and changes no `src/` runtime code.

Osler's overall median/p90 total latency is **38.5/58.2 seconds**, versus **151.3/296.6 seconds** for darwin. Among the seven established cases, medians are **55.2s osler versus 289.5s darwin**. All 17 osler first lookups completed under 300 seconds; darwin TP53 and ALK exceeded the approximate ingress limit. This operational headroom supports the recommendation, while the verified ALINA/adjuvant omission, ALCL approval-scope overstatement, missing amivantamab anticoagulation detail in paired prose, and shared ALEX numeric error require curator review. Lexical overlap and unvalidated judge scores do not establish clinical equivalence.

`_cache_key` includes `openevidence_model`, and warmup and sidecar share that one setting: darwin-warmed entries cannot serve osler lookups. Warming should therefore use osler too. A future explicit darwin deep-review option needs model routing and its own cache slot; it is not a substitute for warming the live model's namespace.

## Method

Run date: October 6, 2026. Base: `origin/main` at `5cece15`. Credentials were loaded through the application's `Settings` from the gitignored `.env` and checked for non-empty presence only. No key is recorded in artifacts.

The prior benchmark's complete 16-gene panel is retained: TP53, KRAS, EGFR, BRAF, BRCA1, ALK, ACACB, AIRE, ANKRD13A, CRACD, DENND2C, FAM117A, RFX7, RP1, TRARG1, CLCN3P1. EML4::ALK is added as a seventeenth paired case. EGFR is first, providing an osler acceptance probe before further spending. Osler runs first, then darwin; within each model, the probe is followed by batches of up to three concurrent calls. This sequential model order is a possible temporal/load confound, and each pair has only one successful observation per model.

Every request uses `OpenEvidenceClient.get_gene_analysis`, the current `_build_question` targeted guideline/trial question, the production streaming endpoint and SSE parser, `distill_openevidence`, and `distill_additive_openevidence` (the #90 filter). EML4::ALK is requested with `gene="ALK", fusion="EML4::ALK"`. The benchmark intentionally asks all panel genes, including genes the sidecar's availability gate might suppress; this preserves the historical negative controls. No full annotation or synthesis runs, and OpenEvidence is never injected into core synthesis. Raw arm artifacts contain the production-parsed analysis and citations, matching the prior layout; they are not complete wire-level SSE captures. Production deliberately ignores table events, so table-only clinical content is absent from both the card and the judge input.

`openevidence.cached_call` is replaced with direct computation for the benchmark process, so **every model/gene makes a live request, regardless of production-warmed EGFR or any Redis entry**. No Redis is read or written. The harness mutates the shared `settings.openevidence_timeout_seconds` and `settings.openevidence_model` globally during the run and restores both in `finally`; it should run in an isolated process, not alongside app requests. The timeout is 900 seconds, with at most three simultaneous calls and a transport-level hard cap of 45 OpenEvidence attempts including retries. The production retry policy remains unchanged: transient network/408/429/5xx failures can retry up to three times; timeouts do not retry. The timeout is httpx's per-operation/read inactivity timeout, not an overall deadline. Measured total wall time includes any retry/backoff, parsing and both deterministic distillations, excludes waiting for a concurrency slot, and ends only after the response stream completes.

The recorded `ttft_seconds` is a **widget-metadata arrival diagnostic, not clinical time-to-first-token**. Although the observer excludes complete search-widget events and citation-only markers, fragmented metadata survives and triggers the first-text measurement. It is retained in raw artifacts but omitted from clinical/latency tables. Overall median/p90 values are 2.66/2.90s darwin and 2.68/3.48s osler. The sidecar accumulates the entire stream and returns JSON, so this diagnostic does not make the card visible early; ingress suitability is determined by total latency.

### PubMed evidence and additivity

Both models use identical **saved historical pipeline evidence**, not newly retrieved PubMed evidence: per-gene `annotation.citations` and `annotation.evidence_cards[].title` from [the September 8 disabled arm](results/openevidence_pointed_20260908/disabled.json). For EML4::ALK, the union of the two partner annotations in [the historical fusion case](results/openevidence_pointed_20260908/eml4_alk_fusion_qualitative.json) is used. This makes additivity reproducible and paired, but reflects the old full-mode pipeline's finite evidence set rather than today's complete PubMed corpus or a new core-mode lookup. Inputs are copied into every raw row and source SHA-256 hashes are saved in `environment.json`.

Additive counts mean “survives the production #90 overlap policy.” NCCN/ASCO/ESMO guidelines and trial registry records are always additive by policy; journal articles are removed on matching core PMID or normalized title. Surviving journal articles are supplementary references, not independently verified new facts or newly validated PMIDs. Guidelines can be PubMed-indexed, particularly ASCO journal guidelines.

### Counts, agreement, and quality rubric

Citation counts are the production parser's deduplicated per-answer citation keys. Guideline-citation counts include society-domain links, society authors, and the production society-guideline title pattern, including ASCO guidelines published in journals. The narrower **card guideline** count uses `distill_openevidence`'s domain-only extraction. Page anchors are retained explicitly in the raw distilled guidelines and the link table below. Trial-mention counts are the production extractor's sentences matching its seed acronym list or PFS/OS/HR/ORR/DFS; they are not counts of unique trials. Outcome-only sentences can count, and some valid named trials are missed by the seed list.

Agreement uses darwin as a **coverage reference, not clinical truth**. Guideline identities use citation URL host/path without page fragments; trial labels use a declared extended acronym seed list plus eight-digit NCT IDs, with case-sensitive acronym matching and spaces/hyphens normalized. Ambiguous PROFILE/SOLO/PRIMA/ARROW seeds are excluded to avoid ordinary words and the PRIMA-1 drug. This is an approximate lexical metric: acronym and NCT labels for one trial can count separately; different guideline versions/PMIDs can be counted separately; unseeded trial names are missed. Per-gene shared and one-arm-only labels are saved in `comparison.json`; the blinded judge compares semantic clinical content separately.

The judge model is resolved from `settings.selection_model` via `resolve_sdk_model(..., "selection")`, not a fixed benchmark model. The final blinded comparison uses **eight Claude requests**: one per clinically established case (EGFR, TP53, KRAS, BRAF, BRCA1, ALK, EML4::ALK) and one grouping the ten shorter negative controls. Model identities, latency, counts and arm order are withheld. A deterministic SHA-256 assignment swaps A/B labels per gene; identities are revealed only in output artifacts. Neutral primary-source facts are supplied without model-specific findings. The rubric scores factual correctness, context-appropriate specificity (drugs/trials/guideline recommendations, or justified absence of actionable evidence), and hallucination safety on a 1–5 scale, higher better.

The judge sees full answer prose and concise citation metadata, has no web access, and receives the same spot-check facts for both answers. These are screening scores from one nonexpert LLM, not validated clinical accuracy. The initial full-source-text request exceeded its context limit (HTTP 400, 253,989 tokens > 200,000 maximum). A reduced whole-panel request succeeded but gave almost all 5s, conflated content across genes, and missed verified errors; it is retained as `blinded_judge.json` and **excluded from the recommendation**. Smaller requests improved differentiation but still misattribute some content and falsely suspect genuine recent papers. Raw explanations are retained; only provisional scores are tabulated, and direct paired-answer review plus primary checks below determine the clinically important findings. There is no claim of statistically established quality equivalence.

## Results

All **34 live OpenEvidence calls succeeded (17/17 per model)**, with **0 timeouts, 0 OpenEvidence API errors and 0 retries**. There were **44 total benchmark API attempts**: 34 OpenEvidence calls, 9 completed Claude judge calls, and 1 context-limit rejection before judge generation. Billing for the rejected request is unknown. One completed whole-panel assessment was excluded for attribution errors; the final scored comparison uses 8 smaller blinded requests.

| Metric | darwin | osler |
|---|---:|---:|
| Median total latency (s) | 151.31 | 38.48 |
| p90 total latency (s) | 296.65 | 58.17 |
| Mean answer length (characters) | 6979.71 | 4960.59 |
| Mean citations | 23.06 | 14.47 |
| Mean guideline citations (all recognized) | 3.94 | 1.35 |
| Mean card guidelines (domain-only) | 3.47 | 1.12 |
| Mean page-anchored card guidelines | 3.47 | 1.06 |
| Mean trial/outcome mentions | 4.47 | 3.82 |
| Mean additive citations after #90 | 22.53 | 14.00 |

**darwin:** 17/17 success; 15/17 under 300 seconds.

**osler:** 17/17 success; 17/17 under 300 seconds.

Osler has a **3.93× lower median total latency** in this sample. Percentiles use linear interpolation over the 17 successful calls; there is no repeated-run confidence interval.

**Guidelines agreement:** osler covers 17/67 darwin reference labels (25.4% micro recall; 19.0% macro recall over genes with nonempty darwin labels).

**Trials agreement:** osler covers 27/43 darwin reference labels (62.8% micro recall; 55.4% macro recall over genes with nonempty darwin labels).

### Established cases versus negative controls

Established cases (7): EGFR, TP53, KRAS, BRAF, BRCA1, ALK, EML4::ALK. Negative controls (10): ACACB, AIRE, ANKRD13A, CRACD, DENND2C, FAM117A, RFX7, RP1, TRARG1, CLCN3P1. Each model succeeded in every case. Values are recomputed from saved `models.json` into `comparison.json`; counts and recalls below are paired within each group.

| Metric | Established darwin | Established osler | Negative darwin | Negative osler |
|---|---:|---:|---:|---:|
| Median total latency (s) | 289.47 | 55.21 | 122.33 | 30.71 |
| p90 total latency (s) | 301.24 | 62.95 | 151.43 | 38.83 |
| Mean guideline citations (all recognized) | 5.29 | 3.14 | 3.00 | 0.10 |
| Mean card guidelines | 4.14 | 2.57 | 3.00 | 0.10 |
| Mean trial/outcome mentions | 10.43 | 9.14 | 0.30 | 0.10 |
| Mean additive citations after #90 | 28.71 | 23.29 | 18.20 | 7.50 |
| Success rate | 7/7 (100%) | 7/7 (100%) | 10/10 (100%) | 10/10 (100%) |
| Total card guidelines | 29 | 18 | 30 | 1 |
| Completed under 300s | 5/7 | 7/7 | 10/10 | 10/10 |

| Osler recall of darwin labels | Established cases | Negative controls |
|---|---:|---:|
| Guideline micro recall | 17/37 = 45.9% | 0/30 = 0% |
| Guideline macro recall (nonempty references) | 50.7% | 0% |
| Trial micro recall | 27/40 = 67.5% | 0/3 = 0% |
| Trial macro recall (nonempty references) | 63.3% | 0% |

The negative-control guideline surplus is **absence-of-evidence citation**, not evidence of actionable treatment support. For example, darwin cites NCCN NSCLC, Breast and Biliary guidelines for CLCN3P1 to explain that it is **not** on biomarker tables. The card shows bare guideline title + page, losing that qualification; a curator can mistake these links for guideline support. Overall guideline totals therefore exaggerate darwin's useful coverage advantage, and negative-control zero recall does not mean osler missed a recommended therapy. Even established-case counts are coverage measures rather than verified recommendations: TP53 has **3 osler card guidelines versus 0 darwin**, despite darwin's longer trial/failure discussion.

### Per-gene measurements

Every arrow is **darwin → osler**. G = recognized guideline citations / domain-only card guidelines; T = production trial/outcome sentences; A = citations surviving #90. All rows have success/success status. Screening judge scores are reported separately below.

| Gene | Total s | Chars | Citations | G | T | A |
|---|---:|---:|---:|---:|---:|---:|
| EGFR | 241.8 → 45.1 | 8531 → 5664 | 21 → 18 | 3/2 → 1/1 | 13 → 20 | 21 → 18 |
| TP53 | 301.8 → 61.5 | 5386 → 7141 | 14 → 23 | 0/0 → 3/3 | 7 → 3 | 14 → 23 |
| KRAS | 293.8 → 56.0 | 9440 → 5555 | 29 → 37 | 4/4 → 4/4 | 5 → 12 | 29 → 37 |
| BRAF | 262.5 → 55.2 | 10712 → 7642 | 46 → 26 | 12/10 → 4/2 | 9 → 10 | 46 → 26 |
| BRCA1 | 289.5 → 51.8 | 11282 → 5062 | 40 → 25 | 9/5 → 5/3 | 13 → 8 | 40 → 25 |
| ALK | 300.9 → 65.1 | 11246 → 7527 | 31 → 21 | 7/6 → 3/3 | 16 → 6 | 30 → 20 |
| ACACB | 139.2 → 31.7 | 6198 → 4139 | 25 → 12 | 5/5 → 1/1 | 0 → 0 | 25 → 12 |
| AIRE | 151.3 → 31.5 | 6760 → 4030 | 24 → 12 | 3/3 → 0/0 | 0 → 0 | 24 → 12 |
| ANKRD13A | 109.2 → 27.3 | 4614 → 9515 | 22 → 7 | 7/7 → 0/0 | 0 → 0 | 20 → 6 |
| CRACD | 139.4 → 36.5 | 5725 → 3963 | 12 → 6 | 2/2 → 0/0 | 0 → 0 | 11 → 5 |
| DENND2C | 117.0 → 28.7 | 5357 → 3018 | 22 → 8 | 4/4 → 0/0 | 1 → 0 | 22 → 8 |
| FAM117A | 121.2 → 27.1 | 5812 → 3180 | 13 → 6 | 2/2 → 0/0 | 0 → 1 | 11 → 4 |
| RFX7 | 123.4 → 42.0 | 4562 → 4428 | 18 → 7 | 2/2 → 0/0 | 0 → 0 | 16 → 4 |
| RP1 | 152.6 → 38.5 | 4754 → 3217 | 17 → 10 | 1/1 → 0/0 | 1 → 0 | 17 → 10 |
| TRARG1 | 114.1 → 29.9 | 4330 → 2500 | 14 → 7 | 1/1 → 0/0 | 1 → 0 | 13 → 7 |
| CLCN3P1 | 106.6 → 26.9 | 4830 → 2907 | 23 → 7 | 3/3 → 0/0 | 0 → 0 | 23 → 7 |
| EML4::ALK | 207.8 → 42.9 | 9116 → 4842 | 21 → 14 | 2/2 → 2/2 | 10 → 5 | 21 → 14 |

### Guideline links returned to the card

Links and page numbers below are exactly as returned, not a validation that the cited PDF page supports every accompanying claim. Journal-hosted ASCO guideline citations are counted above and retained in raw citation metadata but are omitted by the current domain-only card extractor.

| Gene | darwin | osler |
|---|---|---|
| EGFR | [Non-Small Cell Lung Cancer (page=48)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=48); [Colon Cancer (page=46)](https://www.nccn.org/professionals/physician_gls/pdf/colon.pdf#page=46) | [Non-Small Cell Lung Cancer (page=110)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=110) |
| TP53 | — | [Acute Myeloid Leukemia (page=33)](https://www.nccn.org/professionals/physician_gls/pdf/aml.pdf#page=33); [Myelodysplastic Syndromes (page=15)](https://www.nccn.org/professionals/physician_gls/pdf/mds.pdf#page=15); [Genetic/Familial High-Risk Assessment: Breast, Ovarian, Pancreatic, and Prostate (page=74)](https://www.nccn.org/professionals/physician_gls/pdf/genetics_bopp.pdf#page=74) |
| KRAS | [Non-Small Cell Lung Cancer (page=53)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=53); [Colon Cancer (page=47)](https://www.nccn.org/professionals/physician_gls/pdf/colon.pdf#page=47); [Pancreatic Adenocarcinoma (page=60)](https://www.nccn.org/professionals/physician_gls/pdf/pancreatic.pdf#page=60); [Rectal Cancer (page=64)](https://www.nccn.org/professionals/physician_gls/pdf/rectal.pdf#page=64) | [Non-Small Cell Lung Cancer (page=53)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=53); [Colon Cancer (page=47)](https://www.nccn.org/professionals/physician_gls/pdf/colon.pdf#page=47); [Rectal Cancer (page=64)](https://www.nccn.org/professionals/physician_gls/pdf/rectal.pdf#page=64); [Pancreatic Adenocarcinoma (page=21)](https://www.nccn.org/professionals/physician_gls/pdf/pancreatic.pdf#page=21) |
| BRAF | [Melanoma: Cutaneous (page=58)](https://www.nccn.org/professionals/physician_gls/pdf/cutaneous_melanoma.pdf#page=58); [Colon Cancer (page=48)](https://www.nccn.org/professionals/physician_gls/pdf/colon.pdf#page=48); [Non-Small Cell Lung Cancer (page=59)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=59); [Thyroid Carcinoma (page=64)](https://www.nccn.org/professionals/physician_gls/pdf/thyroid.pdf#page=64); [Biliary Tract Cancers (page=38)](https://www.nccn.org/professionals/physician_gls/pdf/btc.pdf#page=38); [Central Nervous System Cancers (page=28)](https://www.nccn.org/professionals/physician_gls/pdf/cns.pdf#page=28); [Hairy Cell Leukemia (page=7)](https://www.nccn.org/professionals/physician_gls/pdf/hairy_cell.pdf#page=7); [Histiocytic Neoplasms (page=45)](https://www.nccn.org/professionals/physician_gls/pdf/histiocytic_neoplasms.pdf#page=45); [Rectal Cancer (page=65)](https://www.nccn.org/professionals/physician_gls/pdf/rectal.pdf#page=65); [Ampullary Adenocarcinoma (page=36)](https://www.nccn.org/professionals/physician_gls/pdf/ampullary.pdf#page=36) | [Melanoma: Cutaneous (page=58)](https://www.nccn.org/professionals/physician_gls/pdf/cutaneous_melanoma.pdf#page=58); [Non-Small Cell Lung Cancer (page=110)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=110) |
| BRCA1 | [Genetic/Familial High-Risk Assessment: Breast, Ovarian, Pancreatic, and Prostate (page=28)](https://www.nccn.org/professionals/physician_gls/pdf/genetics_bopp.pdf#page=28); [Breast Cancer (page=92)](https://www.nccn.org/professionals/physician_gls/pdf/breast.pdf#page=92); [Pancreatic Adenocarcinoma (page=59)](https://www.nccn.org/professionals/physician_gls/pdf/pancreatic.pdf#page=59); [Ovarian Cancer Including Fallopian Tube Cancer and Primary Peritoneal Cancer (page=11)](https://www.nccn.org/professionals/physician_gls/pdf/ovarian.pdf#page=11); [Prostate Cancer (page=97)](https://www.nccn.org/professionals/physician_gls/pdf/prostate.pdf#page=97) | [Breast Cancer (page=29)](https://www.nccn.org/professionals/physician_gls/pdf/breast.pdf#page=29); [Ovarian Cancer Including Fallopian Tube Cancer and Primary Peritoneal Cancer (page=11)](https://www.nccn.org/professionals/physician_gls/pdf/ovarian.pdf#page=11); [Prostate Cancer (page=97)](https://www.nccn.org/professionals/physician_gls/pdf/prostate.pdf#page=97) |
| ALK | [Non-Small Cell Lung Cancer (page=54)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=54); [Soft Tissue Sarcoma (page=63)](https://www.nccn.org/professionals/physician_gls/pdf/sarcoma.pdf#page=63); [T-Cell Lymphomas (page=21)](https://www.nccn.org/professionals/physician_gls/pdf/t-cell.pdf#page=21); [Histiocytic Neoplasms (page=48)](https://www.nccn.org/professionals/physician_gls/pdf/histiocytic_neoplasms.pdf#page=48); [Neuroblastoma (page=37)](https://www.nccn.org/professionals/physician_gls/pdf/neuroblastoma.pdf#page=37); [Uterine Neoplasms (page=60)](https://www.nccn.org/professionals/physician_gls/pdf/uterine.pdf#page=60) | [Non-Small Cell Lung Cancer (page=110)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=110); [Soft Tissue Sarcoma (page=63)](https://www.nccn.org/professionals/physician_gls/pdf/sarcoma.pdf#page=63); [Neuroblastoma (page=21)](https://www.nccn.org/professionals/physician_gls/pdf/neuroblastoma.pdf#page=21) |
| ACACB | [Breast Cancer (page=96)](https://www.nccn.org/professionals/physician_gls/pdf/breast.pdf#page=96); [Biliary Tract Cancers (page=38)](https://www.nccn.org/professionals/physician_gls/pdf/btc.pdf#page=38); [Neuroendocrine and Adrenal Tumors (page=109)](https://www.nccn.org/professionals/physician_gls/pdf/neuroendocrine.pdf#page=109); [Genetic/Familial High-Risk Assessment: Breast, Ovarian, Pancreatic, and Prostate (page=19)](https://www.nccn.org/professionals/physician_gls/pdf/genetics_bopp.pdf#page=19); [Genetic/Familial High-Risk Assessment: Colorectal, Endometrial, Esophageal, and Gastric (page=21)](https://www.nccn.org/professionals/physician_gls/pdf/genetics_ceeg.pdf#page=21) | [Non-Small Cell Lung Cancer (page=100)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=100) |
| AIRE | [Colon Cancer (page=50)](https://www.nccn.org/professionals/physician_gls/pdf/colon.pdf#page=50); [Breast Cancer (page=96)](https://www.nccn.org/professionals/physician_gls/pdf/breast.pdf#page=96); [Thymomas and Thymic Carcinomas (page=16)](https://www.nccn.org/professionals/physician_gls/pdf/thymic.pdf#page=16) | — |
| ANKRD13A | [Breast Cancer (page=96)](https://www.nccn.org/professionals/physician_gls/pdf/breast.pdf#page=96); [Colon Cancer (page=50)](https://www.nccn.org/professionals/physician_gls/pdf/colon.pdf#page=50); [Non-Small Cell Lung Cancer (page=100)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=100); [Esophageal and Esophagogastric Junction Cancers (page=49)](https://www.nccn.org/professionals/physician_gls/pdf/esophageal.pdf#page=49); [Gastric Cancer (page=31)](https://www.nccn.org/professionals/physician_gls/pdf/gastric.pdf#page=31); [Genetic/Familial High-Risk Assessment: Breast, Ovarian, Pancreatic, and Prostate (page=19)](https://www.nccn.org/professionals/physician_gls/pdf/genetics_bopp.pdf#page=19); [Genetic/Familial High-Risk Assessment: Colorectal, Endometrial, Esophageal, and Gastric (page=21)](https://www.nccn.org/professionals/physician_gls/pdf/genetics_ceeg.pdf#page=21) | — |
| CRACD | [Small Cell Lung Cancer (page=4)](https://www.nccn.org/professionals/physician_gls/pdf/sclc.pdf#page=4); [Gastric Cancer (page=28)](https://www.nccn.org/professionals/physician_gls/pdf/gastric.pdf#page=28) | — |
| DENND2C | [Breast Cancer (page=96)](https://www.nccn.org/professionals/physician_gls/pdf/breast.pdf#page=96); [Biliary Tract Cancers (page=38)](https://www.nccn.org/professionals/physician_gls/pdf/btc.pdf#page=38); [Neuroendocrine and Adrenal Tumors (page=109)](https://www.nccn.org/professionals/physician_gls/pdf/neuroendocrine.pdf#page=109); [Non-Small Cell Lung Cancer (page=110)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=110) | — |
| FAM117A | [Genetic/Familial High-Risk Assessment: Breast, Ovarian, Pancreatic, and Prostate (page=15)](https://www.nccn.org/professionals/physician_gls/pdf/genetics_bopp.pdf#page=15); [Genetic/Familial High-Risk Assessment: Colorectal, Endometrial, Esophageal, and Gastric (page=18)](https://www.nccn.org/professionals/physician_gls/pdf/genetics_ceeg.pdf#page=18) | — |
| RFX7 | [B-Cell Lymphomas (page=72)](https://www.nccn.org/professionals/physician_gls/pdf/b-cell.pdf#page=72); [T-Cell Lymphomas (page=65)](https://www.nccn.org/professionals/physician_gls/pdf/t-cell.pdf#page=65) | — |
| RP1 | [Melanoma: Cutaneous (page=34)](https://www.nccn.org/professionals/physician_gls/pdf/cutaneous_melanoma.pdf#page=34) | — |
| TRARG1 | [Non-Small Cell Lung Cancer (page=108)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=108) | — |
| CLCN3P1 | [Non-Small Cell Lung Cancer (page=108)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=108); [Breast Cancer (page=96)](https://www.nccn.org/professionals/physician_gls/pdf/breast.pdf#page=96); [Biliary Tract Cancers (page=38)](https://www.nccn.org/professionals/physician_gls/pdf/btc.pdf#page=38) | — |
| EML4::ALK | [Non-Small Cell Lung Cancer (page=54)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=54); [Soft Tissue Sarcoma (page=63)](https://www.nccn.org/professionals/physician_gls/pdf/sarcoma.pdf#page=63) | [Non-Small Cell Lung Cancer (page=54)](https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=54); [Comparative effectiveness of ALK tyrosine kinase inhibitors in ALK-positive non–small cell lung cancer: A systematic review and network meta-analysis. (no page anchor)](https://meetings.asco.org/abstracts-presentations/266509) |

### Screening only: blinded rubric scores

Judge: `claude-haiku-4-5-20251001`, resolved from `settings.selection_model`. These scores are screening artifacts only; the recommendation rests on measured latency and the verified spot checks, not these scores. Scores are correctness / specificity / hallucination safety, all 1–5 with higher better. Scores remain unverified and prompt-sensitive. The initial whole-panel judge was excluded; raw explanations remain in artifacts because even smaller assessments occasionally misattribute content.

| Screening mean (1–5) | darwin | osler |
|---|---:|---:|
| Factual correctness | 4.76 | 4.65 |
| Specificity | 5.00 | 4.76 |
| Hallucination safety | 4.76 | 4.59 |

| Gene | darwin C/S/H | osler C/S/H | A/B mapping revealed after judging |
|---|---:|---:|---|
| EGFR | 4/5/4 | 4/5/4 | A=osler, B=darwin |
| TP53 | 5/5/5 | 4/4/3 | A=osler, B=darwin |
| KRAS | 4/5/4 | 4/5/4 | A=osler, B=darwin |
| BRAF | 4/5/4 | 4/4/4 | A=darwin, B=osler |
| BRCA1 | 5/5/5 | 4/4/4 | A=darwin, B=osler |
| ALK | 4/5/4 | 4/4/4 | A=darwin, B=osler |
| EML4::ALK | 5/5/5 | 5/5/5 | A=darwin, B=osler |
| ACACB | 5/5/5 | 5/5/5 | A=darwin, B=osler |
| AIRE | 5/5/5 | 5/5/5 | A=darwin, B=osler |
| ANKRD13A | 5/5/5 | 5/5/5 | A=osler, B=darwin |
| CRACD | 5/5/5 | 5/5/5 | A=osler, B=darwin |
| DENND2C | 5/5/5 | 5/5/5 | A=darwin, B=osler |
| FAM117A | 5/5/5 | 5/5/5 | A=darwin, B=osler |
| RFX7 | 5/5/5 | 5/5/5 | A=osler, B=darwin |
| RP1 | 5/5/5 | 5/5/5 | A=osler, B=darwin |
| TRARG1 | 5/5/5 | 5/5/5 | A=osler, B=darwin |
| CLCN3P1 | 5/5/5 | 5/5/5 | A=darwin, B=osler |

## Notable cases and independent spot checks

### Clinically important omissions and disagreements

- **ALK and EML4::ALK:** darwin discusses adjuvant alectinib/ALINA in resected NSCLC; neither osler answer includes ALINA or an adjuvant section. This is a meaningful disease-setting omission, not merely a missing trial acronym. ALINA is a genuine randomized study showing adjuvant disease-free survival benefit ([primary PubMed publication](https://pubmed.ncbi.nlm.nih.gov/38598794/)). For the fusion case, total latency was 207.8s darwin versus 42.9s osler. Both still cover the principal metastatic ALK inhibitor options and CROWN/ALEX evidence.
- **ALK approval context:** osler says crizotinib is FDA-approved for “adult and pediatric” ALCL broadly. Darwin correctly specifies pediatric and young-adult relapsed/refractory disease. The [FDA label](https://www.accessdata.fda.gov/drugsatfda_docs/nda/2025/217581Orig1s000Lbl.pdf) limits this indication to children ≥1 year and young adults, and notes efficacy/safety are not established in older adults. This is a real osler overgeneralization, distinct from trial-ID fabrication.
- **ALK numeric error shared by both:** each ALK answer gives ALEX final median overall survival as 81.1 versus **57.2** months. The [final ALEX PubMed abstract](https://pubmed.ncbi.nlm.nih.gov/41110693/) reports 81.1 versus **54.2** months, HR 0.78. Neither answer identifies a different population/data cut explaining 57.2. The original judge blessed this value; the smaller ALK judge notices the mismatch in darwin but incorrectly says osler lacks the numeric value. Direct raw-answer review confirms the error in both. Darwin is a coverage reference, not ground truth.
- **TP53:** both appropriately state there is no approved TP53-directed standard therapy and that direct p53 reactivation remains investigational. Darwin adds the negative magrolimab ENHANCE-2 result and detailed PYNNACLE/KRAS-wild-type context; osler lacks magrolimab/ENHANCE-2 trial discussion in its parsed answer. The [ENHANCE-2 abstract](https://pubmed.ncbi.nlm.nih.gov/40009500/) confirms the failure and 4.4 versus 6.6-month nonintensive-arm overall survival. The [PYNNACLE primary publication](https://pubmed.ncbi.nlm.nih.gov/41740031/) supports the response-rate and KRAS context. This is useful failure evidence for interpreting investigational strategies. Darwin's parsed TP53 prose ends at a guideline-management heading; production ignores table events, so citation volume alone does not establish how much guideline content reaches the card.
- **EGFR:** both cover metastatic, adjuvant, stage III consolidation, and exon 20 insertion pathways. Darwin adds explicit amivantamab anticoagulation/prophylaxis detail, mutation-specific testing/resistance caveats and later-line management; osler mentions VTE risk but not the prophylactic anticoagulation instruction. This is a potentially important safety-context omission in the paired prose, although we did not independently validate the exact current NCCN PDF wording. Darwin's newer quantitative ADAURA/LAURA results are supported by [the exploratory eight-year ADAURA abstract](https://pubmed.ncbi.nlm.nih.gov/42732874/) and [LAURA](https://pubmed.ncbi.nlm.nih.gov/38828946/).
- **KRAS:** both include the recently approved daraxonrasib pancreatic therapy. This is genuine, not an invented drug/indication: [FDA's August 26, 2026 approval](https://www.fda.gov/drugs/resources-information-approved-drugs/fda-approves-daraxonrasib-metastatic-pancreatic-adenocarcinoma) confirms the indication. Darwin is more explicit about pairing-specific CRC evidence, treatment line, performance-status extrapolation and safety. No independently established clinically decisive one-model-only drug omission was found in this gene.
- **BRAF and BRCA1:** both retain the major biomarker/disease-specific treatment distinctions (BRAF/MEK versus BRAF/EGFR in CRC; germline versus somatic BRCA eligibility). Darwin gives more detailed guideline/eligibility and rare-disease coverage. These are qualitative coverage observations, not a claim that every numerical or label statement was adjudicated. Osler also provides some references darwin does not (for example ARIEL4 in BRCA1); the longer model is not a strict superset.
- **Negative controls:** direct review of the saved paired prose finds both models refrain from asserting established gene-directed therapy for the ten lower-evidence genes. RP1 drug/gene ambiguity and TRARG1/TARG1 name ambiguity remain important to distinguish. This is absence-of-actionability handling, not proof that every preclinical hypothesis is correct. The production availability gate may suppress some of these genes; they were deliberately queried as historical negative controls.

### Suspicious trial/publication checks

The registry API independently returned matching records for **NCT03855371** (PANDA-T0), **NCT04585750** (PYNNACLE), **NCT06765109** (ALKAZAR/neladalkib), and **NCT07001384** (alectinib/duvelisib in ALCL): [saved registry facts](results/openevidence_osler_vs_darwin_20261006/registry_spot_checks.json). These checked IDs are not invented; registry identity does not establish efficacy.

The smaller TP53 judge suspected the recent PANDA-T0 paper/DOI and editorial. Primary checks disprove the fabrication suspicion: [Song et al.](https://pubmed.ncbi.nlm.nih.gov/42585289/) is the genuine August 2026 paper (DOI `10.1126/scitranslmed.ads9325`) and reports four marrow remissions among five pilot patients with NCT03855371; the [Wiman editorial](https://pubmed.ncbi.nlm.nih.gov/42823505/) is genuine October 2026 commentary, not primary efficacy evidence. Darwin's [Y220C acquired-resistance publication](https://pubmed.ncbi.nlm.nih.gov/41504628/) is genuine too. Recent dates alone are not hallucination evidence. Raw judge scores are not retroactively edited to conceal these false-positive flags.

Both EGFR answers' WU-KONG28 PFS figures (10.3 vs 7.5 months, HR 0.65) match [the primary abstract](https://pubmed.ncbi.nlm.nih.gov/42212913/). Checks were targeted to suspicious or important claims, not exhaustive validation of every citation, trial, numeric outcome or linked page. The checked subset found real IDs/publications but also a shared numerical error and an osler approval-scope error: “real citation” and “correct supported claim” are separate properties. [All publication check findings](results/openevidence_osler_vs_darwin_20261006/publication_spot_checks.json) are saved.

### Shared parser/card limitation

All 34 production-parsed analyses begin with leftover search-widget metadata. In addition, osler leaks mid-answer `REACTCOMPONENT` widgets for TP53, ALK and RFX7 (**3/17 osler versus 0/17 darwin**), interrupting clinical prose. Their `consensus_role` therefore begins with metadata rather than a clean clinical summary. The existing parser also omits table events and the card's guideline extractor misses journal-hosted ASCO guidelines, despite their being retained by the #90 citation policy. These are shared presentation/extraction limitations, not evidence of one model's medical quality. They explain why similar ~2.7s first-text SSE latency does not imply equally fast clinical text or visible cards. No runtime parser fix is included in this PR.


## Cost and operational implications

Per-model API pricing and OpenEvidence token usage are unknown: no model-specific price or billing usage appears in these responses or in our configured contract information. Do not infer that faster means cheaper or apply the clinician website's free-use policy to enterprise API calls. The judge's SDK-reported token usage is saved per successful request in `judge_attempts.json`. Conservatively counting the rejected context-limit request, the run used **44 recorded API attempts**, below the approximate 45-attempt cap (34 OpenEvidence, 10 judge attempts); 43 generated completed outputs, including one excluded judge assessment. The original judge guard checked only a fixed OpenEvidence count and did not enforce the combined limit. This revision reserves each judge attempt in `judge_attempts.json` before sending and checks OpenEvidence plus all recorded judge attempts, including rejected/failed/interrupted attempts, against 45. No new OpenEvidence or judge requests were made for this revision. No API cost estimate is fabricated.

The ingress limit is about 300 seconds for an individual first lookup. The service-time measurements exclude queueing behind the production sidecar semaphore. Thus passing below 300 seconds in this sample is evidence of headroom for an isolated lookup, not an SLA for bursts: queueing, retries, vendor load and stream inactivity can still extend request wall time. The benchmark used a 900s read/inactivity timeout. The code default is 60s, but production runs `OPENEVIDENCE_TIMEOUT_SECONDS=300` (600s pending in k8s #662, per deployment information supplied in cross-review). With a **300s per-read inactivity timeout**, a stream that keeps sending data never trips that read timeout; the **~300s total ingress limit** instead cuts the request. Darwin **TP53 (301.8s)** and **ALK (300.9s)** would be cut off by ingress. Raising the read timeout to 600s does not raise the ingress limit. Osler's observed maximum of 65.1s leaves substantial isolated-lookup headroom, subject to queueing/load caveats above. Cache warming with osler removes live-call latency within the same model namespace; darwin-warmed entries cannot serve osler requests.

## Validation and reproduction

- `.venv/bin/pytest tests/ -q` — **427 collected test cases: 399 passed, 27 skipped, 1 failed**. The sole failure is the authorized known environmental exception, `tests/test_local_backends_e2e.py::test_claude_code_backend_real_round_trip`, reporting `Not logged in · Please run /login`. No login/runtime code was changed to mask it. The Codex CLI e2e case is among environment-dependent skips.
- `.venv/bin/pytest tests/ -q --deselect tests/test_local_backends_e2e.py::test_claude_code_backend_real_round_trip` — **399 passed, 27 skipped, 1 deselected** across `tests/`.
- `.venv/bin/pytest tests/test_openevidence_benchmark.py -q` — **15 collected cases, 15 passed** in the touched test file, covering cache bypass/model payloads/fusion questions, streamed timing with fragmented/CRLF events, fail-fast rejection, call budget, agreement null/zero semantics and subgroup aggregation, case-sensitive trial labels excluding ambiguous words/drugs, timeout/RetryError classification, restored global settings, a seeded cache hit ignored by bypass, the aggregate judge guard refusing over-budget calls, and blinded judge payloads with retries disabled and large source quotes excluded.
- `.venv/bin/ruff check .` — passed, full repository. `git diff --check` — passed.
- No separate static typecheck is configured in `pyproject.toml`; imports and execution paths are exercised by the tests.

```sh
uv run --extra dev python -m benchmarks.run_openevidence_benchmark \
  --compare-models osler darwin --timeout 900 --concurrency 3 \
  --output benchmarks/results/NEW_OSLER_DARWIN_RUN
uv run --extra dev python -m benchmarks.compare_openevidence \
  benchmarks/results/NEW_OSLER_DARWIN_RUN --models
uv run --extra dev python -m benchmarks.judge_openevidence_models \
  benchmarks/results/NEW_OSLER_DARWIN_RUN
```

Completed run/judge result files are never overwritten; a rejected request input may be regenerated on retry. Each permitted judge invocation disables SDK retries and reserves one API attempt in the persistent ledger before sending; the aggregate guard refuses an attempt once the combined count reaches 45. Reproduce the final scored assessment by judging each of the seven critical genes separately with `--genes GENE --suffix UNIQUE_NAME`, and the ten negative controls together with another unique suffix. Neutral facts in `publication_spot_checks.json` must be copied without model-specific findings; the runner strips findings from judge input. The assessment is prompt-sensitive and does not replace clinical review. Network fixtures used by tests are independent of the live artifacts. Benchmark-only settings overrides are restored after a run; `openevidence_model` remains `"darwin"` in `src/config.py`.

Artifacts: [raw arms](results/openevidence_osler_vs_darwin_20261006/models.json), [comparison](results/openevidence_osler_vs_darwin_20261006/comparison.json), [blinded judge input](results/openevidence_osler_vs_darwin_20261006/blinded_judge_input.json), [final judge output and mappings](results/openevidence_osler_vs_darwin_20261006/blinded_judge_reviewed.json), [all judge attempt accounting](results/openevidence_osler_vs_darwin_20261006/judge_attempts.json), [registry spot checks](results/openevidence_osler_vs_darwin_20261006/registry_spot_checks.json), [environment and provenance](results/openevidence_osler_vs_darwin_20261006/environment.json).

Final scoring inputs: [EGFR](results/openevidence_osler_vs_darwin_20261006/blinded_judge_EGFR_input.json), [TP53](results/openevidence_osler_vs_darwin_20261006/blinded_judge_TP53_input.json), [KRAS](results/openevidence_osler_vs_darwin_20261006/blinded_judge_KRAS_input.json), [BRAF](results/openevidence_osler_vs_darwin_20261006/blinded_judge_BRAF_input.json), [BRCA1](results/openevidence_osler_vs_darwin_20261006/blinded_judge_BRCA1_input.json), [ALK](results/openevidence_osler_vs_darwin_20261006/blinded_judge_ALK_input.json), [EML4::ALK](results/openevidence_osler_vs_darwin_20261006/blinded_judge_EML4_ALK_input.json), [negative controls](results/openevidence_osler_vs_darwin_20261006/blinded_judge_negative_controls_input.json). These preserve the exact primary facts available at scoring time, before later false-positive checks.

[Gate result accounting](results/openevidence_osler_vs_darwin_20261006/gate_results.json).
