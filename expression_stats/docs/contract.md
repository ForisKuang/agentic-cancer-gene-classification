# Expression context (optional, removable module)

Status: artifact schema_version 1, contract 1.5 (either-arm confidence tier and the
additive `strong_arm` field; additive alteration detail and scope; per-pair
`confidence_tier`), expression method v3, alteration method v2 — 2026-11-13

## Goal

Given a gene, surface only the (gene, cancer type) pairs where:

1. TCGA PanCancer Atlas RNA-seq expression is **statistically significantly
   elevated in that cancer type relative to the other TCGA tumor types**
   (tumor-only data: this is *tumor-type enrichment*, NOT tumor-vs-normal), and
2. the literature characterizes the gene in that tumor type.

Each surfaced pair shows the statistics and the supporting citations side by side.

## Architecture (hybrid)

```
 ┌────────────── expression_stats/ (isolated producer) ──────────────┐
 │ GitHub Action (cron weekly + manual)                       │
 │  1. download 32 *_tcga_pan_can_atlas_2018                  │
 │     data_mrna_seq_v2_rsem.txt from cBioPortal datahub      │
 │     (pinned commit), plus mutation/GISTIC/SV files and     │
 │     case lists for alteration detail                       │
 │  2. stats gate for every gene (one-vs-rest, see below);    │
 │     alteration-type enrichment as detail only              │
 │  3. cheap PubMed-count pre-check for stats-passing pairs   │
 │  4. publish verified GitHub release bundles                 │
 └───────────────────────┬────────────────────────────────────┘
                         │  GitHub release download → local MySQL import
                         ▼
 ┌────────────── ACGC: src/expression_context/ ──────────────┐
 │ opt-in router (flag EXPRESSION_CONTEXT_ENABLED)            │
 │  - reads precomputed hits for the gene                     │
 │  - literature gate on demand via LiteratureGate adapter    │
 │    (v1: ACGC pipeline; later: LRAS /lras/v1/retrieve)      │
 │  - stores joined verdicts in its own MySQL table           │
 └────────────────────────────────────────────────────────────┘
```

No new instances: the stats side is GitHub Actions + release assets; the ACGC side is a
route on the existing deployment, off by default.

## Removability requirements (hard)

The consumer module remains independently removable. Producer removal is
documented in [../REMOVAL.md](../REMOVAL.md).

- All code lives in `src/expression_context/` (router, artifact client, service,
  literature-gate adapter, store, settings, models). Nothing else in `src/`
  imports from it except ONE registration call in `src/main.py`.
- Own settings class with env prefix `EXPRESSION_CONTEXT_` — do **not** add
  fields to `src/config.py`.
- `EXPRESSION_CONTEXT_ENABLED` defaults to `false`. When false: router not
  mounted, table not created, no outbound calls, no extra startup work.
- Own table `expression_context_results`, created by the module only when
  enabled. No foreign keys to, and no new columns on, existing tables
  (`runs`, `gene_annotations`, …). Reads ACGC's cache/pipeline only through
  public functions, never by editing their schema.
- No new heavy dependencies in ACGC (no numpy/scipy/pandas): all statistics are
  computed in expression_stats/.
- Dependency on the producer is release-asset-only against the versioned contract
  below (no submodule, no shared package).
- Literature access goes through a `LiteratureGate` protocol so the ACGC
  pipeline implementation can be swapped for LRAS without touching the router.
- Guard tests: (a) an import-boundary test fails if any module outside
  `src/expression_context/` (other than the single registration line in
  `src/main.py`) imports it; (b) with the flag off the app boots, the routes 404,
  and the table is not created.
- `src/expression_context/REMOVAL.md` lists the exact removal steps
  (delete package, delete registration line + import, delete tests,
  `DROP TABLE expression_context_results`, remove env from k8s manifest).

## Statistics (expression_stats/; method_version "3")

Use the recommended branch-aware hybrid from the follow-up evaluation:
`(v2_lineage_pass AND NOT broadly_expressed) OR subgroup_pass`. This is an
empirical screening rule, not tumor-versus-normal inference. The curated panel
was inspected during method development, not a blinded external validation.

Inputs and sample selection are unchanged: batch-normalized linear RSEM from
32 pinned TCGA PanCancer Atlas studies (COAD+READ combined as COADREAD), primary
samples (`01`, or `03` for LAML), deterministic one-per-patient selection and
Entrez aggregation. Transform finite measurements as `x = log2(RSEM + 1)`;
NaNs are omitted and measured zeros remain measurements. Let
`F = expression_floor_log2 = log2(11) = 3.4594316186372973` (RSEM 10).
Percentiles use NumPy's linear interpolation in log space, then clamp to F.

### Broad baseline and lineage arm

`broadly_expressed` is a non-null boolean on the per-gene document. Set it true
only if every one of the 32 types has at least 10 measured samples, at least
29 types have >=90% of their measured samples at RSEM >=10 (`x >= F`), and the
population CV (`ddof=0`) of `2**median(x_type) - 1` across the 32 types is <=0.5.
Type medians have equal weights, irrespective of cohort size. An undefined CV
or insufficient measurements makes the flag false. It describes a baseline,
not a curated housekeeping-gene list, and may also mark true amplified genes.

The lineage arm retains the m2 test unchanged: two-sided asymptotic
Mann–Whitney U with tie/continuity corrections and per-gene BH across all 32
types; q<0.05, clamped median log2FC>=1, Cliff's delta>=0.5,
median(type)>=F, n>=10. A broadly expressed gene cannot pass this arm.
`log2fc=max(median(type),F)-max(median(rest),F)`;
`log2fc_vs_median_of_type_medians` uses the median of the other type medians
and clamps both operands to F. Raw medians, ranks, AUC and delta are unchanged.
`detection_log2` remains derived from the same floor; the FC gate implies an
effective type median >=F+1. Existing p/q fields continue describing this
lineage test even when a subgroup call passes it by a different test.

### Subgroup arm

For each gene, form the label-invariant threshold
`T=max(Q99(x_all_measured_TCGA), F)`. In each type and its rest, count finite
samples strictly greater than T as k and k_rest. Require **all**:

- n>=10 and n_rest>=10;
- k>=10 and k/n>=0.03;
- `(k/n)/(max(k_rest,1)/n_rest)>=3` (a conservative one-case rest floor);
- `max(Q95(x_type),F)-max(Q95(x_rest),F)>=1.5` log2 units (~2.83-fold);
- one-sided Fisher p=`hypergeom.sf(k-1,n+n_rest,k+k_rest,n)`, with BH across
  all 32 types for this gene, q<0.025.

Broadly expressed genes **may** pass the subgroup arm. All thresholds are
recorded in manifest params. The two arms have separate testing families;
these are per-gene screening q-values, not a claimed genome-wide FDR or a
joint 0.05 FDR for the OR rule.

### Wire fields and nullability

All previous fields remain. `stats_pass` equals `pass_mode != null`:

| New field | Type / meaning |
| --- | --- |
| document `broadly_expressed` | bool, never null; definition above |
| `pass_mode` | `"lineage"`, `"subgroup"`, or null; lineage wins if both arms pass |
| `lineage_blocked_by_broad` | bool, never null; original m2 lineage rule passed and gene is broad, even if subgroup rescues it |
| `subgroup_high_count` | int or null; k, null if type has no measured samples |
| `subgroup_high_fraction` | float or null; k/n, null if n=0 |
| `subgroup_high_fraction_ratio` | float or null; rest-floor ratio above, null if n=0 or n_rest=0 |
| `p95_log2_diff` | float or null; clamped P95 difference, null if either group has no measurements |
| `subgroup_q` | float or null; subgroup BH q, null if either group has no measurements |

A small but measured group may have defined diagnostics/q while still failing
min_n. Undefined tests enter their respective 32-type BH family as p=1,
then serialize as null. A constant measured gene has defined lineage p=1
and subgroup q=1. Group-specific measured summaries remain defined when the
opposite group is empty. Existing nullable fields retain their semantics.
`p90_log2`/`rest_p90_log2` remain informational. `tissue_of_origin_risk` and
`small_n` remain informational flags. Do not interpret a null as measured zero.

### Confidence tier (contract 1.3; either-arm criteria since 1.5)

Product rule: only high-confidence pairs may be surfaced to curators; every weaker v3
pass is an unsurfaced **candidate** that curators can still find. On top of the
unchanged v3 pass (every rule above still required), each pair carries

| Field | Type / meaning |
| --- | --- |
| `confidence_tier` | `"high"`, `"candidate"`, or null; null exactly when `stats_pass` is false |
| `strong_arm` | `"lineage"`, `"subgroup"`, `"both"`, or null; which arm(s) met their strong cutoffs. Non-null exactly when `confidence_tier` is `"high"` (purity demotion clears it) |

A v3 pass is `"high"` if **either** arm meets its strong cutoffs (inclusive), otherwise
`"candidate"`:

| Arm | Strong cutoffs (manifest `params`) |
| --- | --- |
| lineage | `log2fc >= strong_log2fc` (2.0) **and** `cliffs_delta >= strong_cliffs_delta` (0.70); retains the broad-expression block |
| subgroup | the **base subgroup gate passes** (counts, P95 shift, FDR; every rule above) **and** `subgroup_high_fraction >= strong_subgroup_min_fraction` (0.05) **and** `subgroup_high_fraction_ratio >= strong_subgroup_min_fraction_ratio` (5.0) |

The subgroup strong criteria are evaluated from the subgroup statistics regardless of
`pass_mode`, but only after the base subgroup gate passes: a pair that passes by lineage
first (including one in which lineage wins `pass_mode`) is promoted when its subgroup
tail clears the strong bar. Fraction and ratio alone are not sufficient; the subgroup
arm must also satisfy `n`, `n_rest`, `subgroup_high_count`, `p95_log2_diff` and
`subgroup_q`. A broadly expressed gene whose lineage arm is blocked can therefore be high
via `strong_arm: "subgroup"` only. `strong_arm` is `"both"` only when both strong masks
hold.

The tier is derived read-only from the v3 arrays: no v3 threshold, pass/fail call,
`pass_mode` or label changes, and every v3 statistics field is byte-identical to the
contract 1.2 build (tested). The four `strong_*` params are hashed into the release
ID. The cutoffs come from the read-only confidence-tier evaluation; they are a selective
statistical screen, not a calibrated precision. Purity demotion (contract 1.4) always
applies after the tier is assigned, including to pairs newly promoted by the subgroup
arm; the purity re-test evaluates the filtered strong statistics of both arms.

**Optional field.** `confidence_tier` is optional in the schema: bundles before contract
1.3 omit it and still load. `strong_arm` is optional for every contract version; readers
that predate 1.5 ignore it. A `strong_arm` value is never used to gate or surface a pair
(the tier and purity state remain authoritative). Only for a bundle whose manifest `contract_version` is a
well-formed version string (exactly `^[0-9]+\.[0-9]+(\.[0-9]+)?$`: no sign, no
whitespace) **below** `"1.3"` may consumers treat a **missing** field on a v3 pass as
`"high"` (the pre-1.3 behaviour). Every other manifest requires the field: 1.3 or later,
and also a missing, null, empty, non-string or malformed `contract_version` (fail
closed; every bundle since contract 1.1 writes the version). A missing tier on a v3 pass
in such a bundle is malformed and never high: ACGC rejects the bundle at load, and any
pair that still reaches it fails closed, logged once per bundle. An explicit null or any
unknown value on a v3 pass is never high.

**Consumer rule (ACGC).** A pair is surfaced only if its statistics pass (v3, minus
broad-blocked pairs), `confidence_tier` is (effectively) `"high"`, the effective PubMed
precheck is not a fresh `false`, and a valid tumor-specific v2 literature verdict for the
active release supports it. Candidates are never sent to the paid literature check and
never surfaced or exported as golden-set rows.

## Alteration-type statistics (detail only; alteration method "2", contract 1.2)

Alongside expression, every (gene, cohort) pair carries per-category genomic-alteration
enrichment for the curators' categories, spelled exactly **"Point Mutation"**,
**"Structural Variant"** and **"Copy Number Alteration"** (expression remains
"Expression"). These results are **detail only**: they never feed `stats_pass`,
`pass_mode`, `lineage_blocked_by_broad`, `broadly_expressed`, flags, PubMed prechecks,
the expression controls, or which pairs surface. For identical inputs every expression
field is byte-for-byte identical with or without alterations (tested), and every
expression pass, label and flag is identical to the alteration method 1 build. The expression `method_version` stays `"3"` and the
release ID keeps its `m3` token; alterations are versioned separately as
`alteration_method_version`.

**Inputs.** The same pinned datahub commit and the same 32 `*_tcga_pan_can_atlas_2018`
studies: `data_mutations.txt` (MAF), `data_cna.txt` (GISTIC discrete) and
`data_sv.txt`, plus the case lists `case_lists/cases_sequenced.txt`, `cases_cna.txt`
and `cases_sv.txt`. Each raw file is parsed once into a compact, selection-independent
summary cached by LFS object id (`.cache/alterations/p<parser>/`); CI deletes each raw
file right after parsing.

**Population and denominators.** Exactly the expression sample selection (one primary
tumour per patient, `01`, or `03` for LAML, chosen once across studies). For each
category the denominator is the selected patients that are **profiled** for that data
type: in the category's case list (and, for CNA, a column of the GISTIC matrix with a
measured value for the gene). Unprofiled patients never enter a target or rest
denominator as wild type. Point Mutation additionally excludes hypermutated tumours
(below) from both target and rest.

Matching is by the patient's **selected sample ID** only (15-character
`TCGA-XX-XXXX-NN`; longer aliquot barcodes are truncated to it). Other samples of the
same patient (for example a `06` metastasis) never count. Each patient contributes at
most one sample to numerator and denominator: for CNA exactly one GISTIC column is
used, the column whose ID equals the selected sample (if a file repeated that ID, the
first such column in file order); mutation and fusion events are de-duplicated per
(gene, selected sample). The build refuses a selection with two samples of one patient.
In the pinned data every GISTIC column, MAF `Tumor_Sample_Barcode` and SV `Sample_Id` is
already a unique 15-character sample ID, so these rules are safeguards.

**Event definitions (patient level; a patient counts once per gene and category).**

- *Point Mutation* — putative drivers under the TCGA-derived rule of
  `point-mutation-options.md` (rule C), using the swappable driver source
  `mutation_driver_source` (today `"bailey2018+new_framework"`, 345 genes: Bailey's 299
  plus the 46 genes curators annotate Point Mutation in New_Framework that Bailey lacks;
  see *Pinned gene lists*): the gene must be in the eligibility list;
  a missense call counts when the same (gene, `Transcript_ID`, protein position) is
  mutated in ≥`missense_min_recurrence` (10) retained tumours pan-cancer; a truncating
  call (nonsense, frameshift indel, splice site) counts only in genes labelled TSG or
  possible TSG in any Bailey row, or, for the 46 added genes, whose curated "OG or TSG"
  label is TSG or OG/TSG (added genes labelled Oncogene or with no label count only
  recurrent missense and in-frame changes); an in-frame insertion/deletion counts when the exact
  (gene, transcript, `HGVSp_Short`) recurs in ≥`inframe_min_recurrence` (5) retained
  tumours. Tumours with more than `hypermutator_max_nonsynonymous` (500) nonsynonymous
  MAF rows (missense, nonsense, nonstop, splice site, translation start, frameshift and
  in-frame indels, all genes) are excluded from events, recurrence and denominators.
  Genes outside the eligibility list are `not_testable` with reason
  `gene not in mutation driver list (bailey2018+new_framework)`; this is
  "no supported driver annotation", not evidence of neutrality.
- *Copy Number Alteration* — GISTIC exactly **+2** (amplification, `amp`) and exactly
  **−2** (deep deletion, `del`), tested separately; +1/−1 are comparators. Duplicate
  source rows for one gene combine per sample. **Scope:** only genes on the pinned
  copy-number driver list `cna_driver_source` (`"bailey2018+new_framework+sanchezvega2018"`,
  378 genes) can be called; every other gene is `not_testable` with reason exactly
  `not a copy-number driver` (co-amplified / co-deleted passengers are not reported as
  "not enriched"). **Direction by role** (`cna_direction_by_role`): a gene whose known
  roles are oncogene only may be called for `amp` only, tumour suppressor only for `del`
  only; both roles or no known role allow both. The other direction's statistics remain
  in `amp`/`del` with `enriched: false`, and the `reason` of a non-enriched record
  explains it (`del: not called: oncogene role allows amplification only`). Both
  directions keep their own 32-cohort BH family whatever the role. `allowed_directions`
  lists the callable directions (`[]` when not testable). The category is enriched if an
  allowed direction passes; `enriched_directions` lists which. **SKCM** GISTIC data cover only
  metastatic (type `06`) samples, so no selected primary is profiled and SKCM CNA is
  `not_testable` with that reason; SKCM primaries are absent from every CNA rest
  denominator.
- *Structural Variant* — any non-germline `data_sv.txt` record with the gene as Site1
  or Site2 (fusion partner either side). Counts are observed curated fusion calls, not
  assay sensitivity; fewer than the SV `min_events` (3) gives `insufficient_data`, never
  `not_enriched`.

**Test.** For each gene, category (and CNA direction) and cohort: one-sided Fisher exact
test (hypergeometric upper tail) of `[[events, profiled − events], [rest_events,
rest_profiled − rest_events]]`, the cohort versus the other 31 cohorts' profiled
patients. BH runs across the 32 cohorts separately for each gene × category, and for
CNA separately for each direction (one 32-comparison family for `amp`, another for
`del`), matching Point Mutation and Structural Variant. Undefined comparisons enter BH as p=1 and
serialize as null. `ratio = frequency / rest_frequency` (null when rest frequency is 0;
a zero rest with ≥1 target event passes the ratio gate as unbounded). Enriched requires
**all** of the category gates:

| Category | q < | ratio ≥ | events ≥ | frequency ≥ |
| --- | --- | --- | --- | --- |
| Point Mutation | 0.05 | 2 | 5 | 2% |
| Copy Number Alteration (each direction) | 0.05 | 2 | 5 | 2% |
| Structural Variant | 0.05 | 3 | 3 | 0.5% |

The SV gates are the alterations report's rare-event proposal; PM/CNA use the requested
defaults. A cohort with fewer than `min_profiled` (20) profiled patients is
`insufficient_data`. All values are `alteration_params` in the manifest. These are
per-gene, per-category screening q-values, not genome-wide FDR.

**Statuses (every gene × cohort × category, never omitted).**
`enriched` (all gates pass); `not_enriched` (tested; `reason` lists failed gates);
`insufficient_data` (too few profiled patients, or too few fusion events); `not_testable`
(`reason` says why: no profiled primaries / metastatic-only data, gene not in the
mutation eligibility list, `not a copy-number driver`, gene not in GISTIC data or
unmeasured in that cohort). Cohort-level reasons (e.g. SKCM metastatic-only CNA) take
precedence over gene-level ones. Every non-enriched status has a non-null `reason`.

**Bailey driver list (licence caveat).** `gene_expression_relevance/bailey2018_drivers.tsv`
is Table S1 of Bailey et al., *Comprehensive Characterization of Cancer Driver Genes and
Mutations*, Cell 173:371–385 (2018), doi:10.1016/j.cell.2018.02.060 (supplement
`1-s2.0-S009286741830237X-mmc1.xlsx`, SHA-256
`806a39951ab8c7caa6965ab7029b99498c9630e7508fe054b299168f16618790`), reduced to gene,
cancer, 20/20+ role and decision columns. `drivers-pin.json` pins the TSV SHA-256, which
is verified at load and recorded as `source.mutation_driver_sha256`. The publisher
supplement carries **no explicit data licence**; it is used here as a research gene
list. OncoKB oncogenic/TSG labels can replace it by registering another source in
`drivers.py` and changing `mutation_driver_source`, which changes the release identity.

**Pinned gene lists (alteration method 2).** Every list is a committed file whose
SHA-256 is verified at load; source names, a combined SHA-256 and per-file SHA-256s are
recorded (`source.mutation_driver_*`, `source.cna_driver_*`) and hashed into the release
identity, and all files are part of the code fingerprint. Roles from several sources are
unioned per gene.

| List | File / pin | Used for | Licence note |
| --- | --- | --- | --- |
| Bailey 2018 Table S1 (299 genes, 20/20+ roles) | `bailey2018_drivers.tsv` / `drivers-pin.json` | PM eligibility and TSG rule; CNA scope and role | publisher supplement, no explicit licence (above) |
| New_Framework curated Copy Number Alteration genes (38) | `new_framework_cna_genes.tsv` / `curated-lists-pin.json` | CNA scope; curated OG/TSG role | internal OncoKB curation, no public licence; symbols and one-word role only |
| New_Framework curated Point Mutation genes not in Bailey (46) | `new_framework_mutation_genes.tsv` / `curated-lists-pin.json` | PM eligibility; truncating counted only for TSG or OG/TSG | as above |
| Sanchez-Vega et al. 2018 Table S3 pathway templates (334 genes; 109 qualify) | `sanchezvega2018_pathway_genes.tsv` / `sanchezvega2018-pin.json` | CNA scope and role, only genes the paper lists as recurrent GISTIC targets in a role-consistent direction (OG with GISTIC amp, TSG with GISTIC del, other labels either) | publisher supplement, no explicit licence; attribution as Bailey |

The curator lists are derived from the New_Framework CSV export, read as data, by
`scripts/derive_curated_lists.py` (source CSV SHA-256 in the pin). Curator roles map
"Oncogene" → OG, "TSG" → TSG, "OG/TSG" → both; blank, "Neither" and "Insufficient
Evidence" are no known role. Sanchez-Vega is needed because MDM2 (a gated SARC
amplification control) is in neither Bailey nor New_Framework.

**Wire format.** Each per-type record gains a nullable `alterations` object (null only
when a review build used `--skip-alterations`; published bundles always populate it):

```json
"alterations": {
  "alteration_types_enriched": ["Copy Number Alteration"],
  "categories": {
    "Point Mutation": {"status": "not_enriched", "reason": "q=0.31 >= 0.05",
      "n_events": 30, "n_profiled": 1048, "frequency": 0.0286,
      "rest_n_events": 98, "rest_n_profiled": 8647, "rest_frequency": 0.0113,
      "ratio": 2.53, "p_value": 0.01, "q_value": 0.31, "direction": null},
    "Structural Variant": {"status": "insufficient_data", "reason": "...", "...": "..."},
    "Copy Number Alteration": {"status": "enriched", "reason": null,
      "n_events": 122, "n_profiled": 1068, "frequency": 0.114, "...": "...",
      "direction": "amp", "enriched_directions": ["amp"],
      "amp": {"n_events": 122, "...": "...", "enriched": true},
      "del": {"n_events": 0, "...": "...", "enriched": false}}
  }
}
```

`alteration_types_enriched` lists enriched categories in the fixed order Point Mutation,
Structural Variant, Copy Number Alteration. Category keys are always all three. Numeric
fields are null when undefined or `not_testable`; never read null as zero. For CNA the
top-level statistics describe `direction` (the first enriched direction, else the one
with the smaller p); `amp`/`del` hold both. `n_profiled` is the category denominator
(CNA: patients with a measured call for the gene). The manifest adds `contract_version`
("1.5"; "1.4" before `strong_arm`, "1.3" before `confidence_tier`, "1.2" before the CNA driver lists), `alteration_method_version`, `alteration_params`, `build.skip_alterations`,
`source.alteration_inputs_sha256`, `source.mutation_driver_source`,
`source.mutation_driver_sha256`, `source.mutation_driver_files`, `source.cna_driver_source`,
`source.cna_driver_sha256`, `source.cna_driver_files` (contract 1.2), and an `alterations`
summary (per-cohort selected / profiled / hypermutator counts, the SHA-256 of every input
file and case list, and `cna_driver_genes*` / `cna_driver_directions` counts).

**Backward compatibility.** `schema_version` stays `"1"`; all additions are new keys.
Consumers ignoring unknown fields (the current `src/expression_context` loader) keep
working unchanged; documents without `alterations` (contract 1.0) remain valid. Contract
1.2 changes no field type or status vocabulary: it adds `allowed_directions`, the
manifest `*_driver_*` keys and new `not_testable` reason strings, and alteration method 2
changes which genes / directions can be enriched. Expression fields are untouched.

**Identity.** The build fingerprint (release ID suffix) also hashes
`alteration_method_version`, `alteration_params` (including `mutation_driver_source`,
`cna_driver_source` and `cna_direction_by_role`), the mutation and copy-number driver
source names with combined and per-file SHA-256s, and the SHA-256 of every alteration
input file and case list; every pinned list TSV and pin JSON is part of the code
fingerprint. Changing any of them yields a new release ID.

### Curated controls and PubMed

`expression_stats/controls.yaml` is JSON-compatible YAML, read with stdlib
JSON, with original source SHA-256, PMIDs, class and expected outcomes. It
contains 25 lineage positives, 10 subgroup positives, 16 ADC/I-O abundance positives
(classification `D`), 24 negatives and three regression negatives. ERBB2/ESCA is reported
but excluded from scoring.

The 16 `D` pairs are the curator workbook's recommended must-pass additions (each passed
the unchanged m3 rule when proposed; a descriptive, not held-out, panel): biomarker-free
ADC anchors NECTIN4/BLCA, CD19/DLBC, CD79B/DLBC, TNFRSF8/DLBC; clinical/trial abundance
targets CEACAM5/COADREAD, MSLN/MESO, MSLN/OV, MSLN/PAAD, GPC3/LIHC, CA9/KIRC,
CLDN6/TGCT, CLDN6/OV, KLK2/PRAD, STEAP1/PRAD, SLC34A2/OV, SLC34A2/LUAD. Each row's
`note` names the agent(s), biomarker-free status and histology scope and `source` the
workbook provenance.

`expression_candidate_misses` documents clinically supported pairs the RNA rule does not
recover (F3/CESC, MET/LUAD). They carry no `expected` field, are evaluated and reported
under the report's `candidate_misses` (`gating: false`, observed `stats_pass` and deciding
statistics), never counted in positive/negative totals and never fail the gate or
publication. Their ids and pairs must not duplicate any gated control.
The panel is included in the code fingerprint so changing it changes identity.
Control IDs and (gene, tcga) pairs must both be unique; evaluation and publication
reject duplicates. Publisher positive/negative totals are derived from this panel,
not hard-coded, while requiring all positives pass, zero negative passes and
zero missing scored pairs.

Every build writes `v1/controls-report.json`. Full builds must pass all 51
positives and pass none of the 27 negatives; missing scored genes/types fail
closed, including missing negatives. The gate runs before PubMed. Failure
raises, preserves the report and manifest under `<out>-failed/`, retains any
previous complete output, and cannot reach publication. The workflow uploads
failed control artifacts for review. Subset builds report `skipped_subset`,
do not enforce the gate, and remain unpublishable. The manifest's `controls`
summary records status, panel SHA-256, counts, missing count and failures.
Publication independently requires the complete passing report for the current
panel, 51/51 positives and 0/27 negatives, in addition to full precheck coverage.

**Control tiers (contract 1.3).** The gate stays on the v3 pass: positives are never
required to be high. The report's `tiers` block (also in the manifest `controls`
summary) records `positive_high`, `positive_candidate`, `negative_high`, the totals and
`positive_candidate_ids`, and each control row records its `confidence_tier`. Any
negative with `confidence_tier: "high"` fails the gate (implied by the v3 negative rule,
asserted separately). With the cutoffs above, PAX8–KIRC, ERBB2–BRCA, ERBB2–STAD,
EGFR–GBM, MSLN–PAAD and SLC34A2–OV are candidates (45/51 high, 0/27 negatives high).
Publication also requires `tiers.negative_high == 0`, and the publish-time recheck
compares every control's re-derived tier and the `tiers` block.
`controls-report.json` is a separate release asset with its existing bytes.

**Alteration controls.** `alteration_controls` in the same panel gate category statuses
(`gene`, `tcga`, `category`, optional CNA `direction`, `expected`). `tcga: "*"` expands
to one row per build cohort with id `<id>:<code>`. Must be enriched (13): Point
Mutation KRAS/COADREAD, KRAS/PAAD, NRAS/COADREAD, BRAF/SKCM, BRAF/THCA, IDH1/LGG,
PIK3CA/BRCA, EGFR/LUAD; Copy Number Alteration with `amp` enriched ERBB2/BRCA,
ERBB2/STAD, MDM2/SARC, CDK4/SARC; Structural Variant ROS1/LUAD. Must not be enriched
(66): TTN and MUC16 Point Mutation in every cohort (64), ERBB2/COADREAD and MET/LUAD
Copy Number Alteration. ALK/LUAD Structural Variant is recorded with `expected: null`
(borderline, not gated). Duplicate ids or (gene, cohort, category) after expansion are
rejected; a missing gene/record fails closed. The report gains an `alterations`
sub-report (and the manifest `controls.alterations` summary) with the same counters;
any alteration failure fails the whole gate before PubMed. Publication additionally
requires `build.skip_alterations == false`, a passing alteration sub-report,
13/13 positives, 0/66 negative passes and zero missing. `--skip-alterations` and subset
builds are review-only.

**Publish-time recheck.** The publisher does not trust counters or statuses copied into
`controls-report.json` or the manifest. After verifying `genes.jsonl.gz` size and
SHA-256, it reads every expression and alteration control gene's document from the
bundle and re-derives each control with the same stdlib evaluator: the expression
`stats_pass` and `confidence_tier` for the cohort, and the alteration category status (and required CNA
direction) for the cohort. It refuses to publish if a control gene or record is
missing, a positive is not enriched/passing, a negative is enriched/passing, or the
re-derived counters or per-control calls (id, observed, directions, events, profiled,
matched) disagree with the bundled report.

PubMed runs only for final stats-passing pairs, using
`"GENE"[tiab] AND (<tumor_type_query>)`. Precheck passes for count>=1. Cached
counts invalidate when queries change. Expiry is deterministically spread by
SHA-256 of (gene, type): `fetched_at + max_ttl * (1 - 0.5 * hash_fraction)`,
giving 15–30 days for the 30-day maximum TTL. Existing fetched timestamps are
reused; no count ever lives longer than the maximum. The default
rate is **3 requests/s even with an API key**, reserving capacity for ACGC.
Configure `EXPRESSION_STATS_PUBMED_REQUESTS_PER_SECOND` (or the Python client
`requests_per_second` argument); it must be finite and >0, capped at 3 without
a key or 10 with a key. Retries share the same limiter. The effective rate is recorded as the unhashed
`source.precheck_requests_per_second`, never in method `params`.
`runtime-settings.json` stores operational defaults outside the source fingerprint;
changing its rate or the environment override leaves `release_id` unchanged.
Workflow prechecks still require NCBI_API_KEY.

The precheck has a default 300-minute phase budget, configurable with
`EXPRESSION_STATS_PRECHECK_TIME_BUDGET_SECONDS` or `runtime-settings.json`.
It is also capped at 315 minutes from builder start, leaving at least 15 minutes
before the workflow's 330-minute step timeout for thread shutdown and cache
saving. `source.precheck_time_budget_seconds` and `source.build_time_budget_seconds`
record these unhashed settings. Socket timeouts, retries, rate-limiter waits and
`Retry-After` sleeps respect the remaining budget. Pending tasks are cancelled;
in-flight workers stop at the deadline. Successful counts commit individually.
Budget exhaustion raises an error, preserves prior complete output and writes
`<out>-incomplete/{manifest.json,controls-report.json,precheck-report.json}`.
Its manifest marks `source.precheck_incomplete=true` and has no completed
`precheck_expires_at`; publication refuses it. The always-run PubMed cache save
still runs, and the workflow uploads the incomplete report for review. Re-run
the workflow to reuse committed counts; statistics are rebuilt, but successful
queries are not repeated while fresh. No partial gene bundle is published.

## Release bundle contract v1 (expression_stats/ → ACGC)

The producer and reader share this single authoritative contract. Reader implementation
is maintained separately (PR #167). No shared Python imports are permitted.

`manifest.json` release asset:

```json
{
  "schema_version": "1",
  "contract_version": "1.5",
  "release_id": "datahub-<12hex>-m3-<build_sha12>",
  "bundle_tag": "expression-stats-datahub-<12hex>-m3-<build_sha12>-pubmed-<YYYYMMDD>",
  "pubmed_snapshot_date": "<YYYY-MM-DD>",
  "gene_count": 20488,
  "sha256": "<sha256 of compressed genes.jsonl.gz>",
  "size_bytes": 12345678,
  "method_version": "3",
  "params": {
    "fdr": 0.05,
    "log2fc": 1.0,
    "cliffs_delta": 0.5,
    "min_n": 10,
    "expression_floor_log2": 3.4594316186372973,
    "broad_sample_fraction": 0.9,
    "broad_min_types": 29,
    "broad_type_median_cv": 0.5,
    "broad_required_types": 32,
    "subgroup_percentile": 99,
    "subgroup_min_count": 10,
    "subgroup_min_fraction": 0.03,
    "subgroup_min_fraction_ratio": 3.0,
    "subgroup_p95_percentile": 95,
    "subgroup_p95_log2_diff": 1.5,
    "subgroup_fdr": 0.025,
    "strong_log2fc": 2.0,
    "strong_cliffs_delta": 0.7,
    "strong_subgroup_min_fraction": 0.05,
    "strong_subgroup_min_fraction_ratio": 5.0,
    "precheck_min_pmids": 1,
    "precheck_cache_ttl_days": 30,
    "detection_log2": 3.4594316186372973
  },
  "source": {
    "datahub_commit": "<sha>",
    "generated_at": "<iso8601>",
    "precheck_requests_per_second": 3.0,
    "precheck_time_budget_seconds": 18000,
    "build_time_budget_seconds": 18900,
    "hgnc_sha256": "<sha256>",
    "code_sha256": "<sha256>",
    "overrides_sha256": "<sha256>",
    "build_sha256": "<sha256>",
    "cohort_mapping_sha256": "<sha256>"
  },
  "build": {
    "genes": null,
    "skip_precheck": false,
    "skip_alterations": false
  },
  "cancer_types": [
    {
      "study_id": "brca_tcga_pan_can_atlas_2018",
      "oncotree_code": "BRCA",
      "name": "Breast Invasive Carcinoma",
      "tumor_type_query": "breast cancer",
      "import_date": "2026-06-05 15:34:03"
    }
  ],
  "controls": {
    "status": "passed",
    "controls_sha256": "<sha256 of controls.yaml>",
    "positive_passed": 51,
    "positive_total": 51,
    "negative_passed": 0,
    "negative_total": 27,
    "missing": 0,
    "failures": [],
    "tiers": {
      "positive_high": 45,
      "positive_candidate": 6,
      "positive_total": 51,
      "negative_high": 0,
      "negative_total": 27,
      "positive_candidate_ids": ["..."]
    }
  }
}
```

`genes.jsonl.gz`: gzip-compressed JSON Lines, one existing per-gene document
per line, unchanged schema; symbols are upper-case canonical symbols.

```json
{
  "schema_version": "1",
  "release_id": "datahub-<12hex>-m3-<build_sha12>",
  "gene": {"hugo_symbol": "ERBB2", "entrez_gene_id": 2064},
  "broadly_expressed": false,
  "cancer_types": [
    {"oncotree_code": "BRCA", "n": 1082, "n_rest": 8989,
     "median_log2": 0.0, "rest_median_log2": 0.0,
     "p90_log2": 0.0, "rest_p90_log2": 0.0,
     "log2fc": 0.0, "log2fc_vs_median_of_type_medians": 0.0,
     "cliffs_delta": 0.0, "auc": 0.0, "p_value": 0.0, "q_value": 0.0,
     "stats_pass": false, "pass_mode": null, "lineage_blocked_by_broad": false,
     "confidence_tier": null, "strong_arm": null,
     "subgroup_high_count": 0, "subgroup_high_fraction": 0.0,
     "subgroup_high_fraction_ratio": 0.0, "p95_log2_diff": 0.0, "subgroup_q": 1.0,
     "pubmed_count": null, "literature_precheck_pass": null,
     "flags": []}
  ]
}
```

`aliases.json` release asset → `{"HER2": "ERBB2", …}` (unambiguous aliases only).

`oncotree_code` contains **TCGA PanCancer cohort codes**, including COADREAD,
GBM, LGG, PCPG and SARC. These preserve the 32 study partitions and are not
necessarily current OncoTree leaf codes. Consumers must use the manifest's
mapping and disease phrase rather than reinterpret codes as leaf identifiers.

The following per-type numeric fields are nullable when n == 0, the relevant
sample group has no measurements, or the statistic is undefined:
`median_log2`, `rest_median_log2`, `log2fc`,
`log2fc_vs_median_of_type_medians`, `cliffs_delta`, `auc`, `p_value`, `q_value`,
`p90_log2`, `rest_p90_log2`. Group-specific medians/percentiles can remain
measured when the opposite group is empty. Undefined p/q are null; BH still
includes that comparison as p=1 internally. `n` and `n_rest` remain integers
and `stats_pass` remains boolean (false for an undefined comparison).
`pubmed_count` and `literature_precheck_pass` are null when the precheck was not
run for that pair, including stats_pass=false and `--skip-precheck`.
ACGC integration must accept these numeric and precheck fields as Optional;
no nullable numeric value should be treated as a measured zero.

**Consumer semantics:** `literature_precheck_pass: null` means the precheck was
not run; a stats-passing pair must still be passed to the consumer's literature
gate. Only an explicit `literature_precheck_pass: false` excludes a pair at the
precheck stage. Thus eligibility is `stats_pass && (literature_precheck_pass is
not false)`. `stats_pass: false` still excludes the pair regardless of precheck.
This file is the **single source of truth** for the producer/consumer contract;
consumer repositories should link here rather than duplicate these semantics.

Release IDs have the exact form `datahub-<12hex>-m3-<build_sha12>`. They are
input identities, not build dates. The suffix hashes method parameters,
override contents, HGNC snapshot hash, code fingerprint (including the curated controls panel), gene subset,
precheck mode, the alteration identity (method, params, driver source and every
alteration input hash; see Alteration-type statistics), and the cohort mapping fingerprint. The latter hashes the sorted
list of (`study_id`, `oncotree_code`, `name`, `tumor_type_query`), excluding
`import_date`, and is recorded as `source.cohort_mapping_sha256`. Neither the build date, precheck freshness nor fetch timestamps
participate. A forced rebuild due to PubMed TTL expiry with identical inputs
keeps the same release_id, allowing ACGC to reuse its literature verdicts.
`source.generated_at` and `source.precheck_expires_at` record freshness separately.
The code fingerprint hashes package source (including the vendored Bailey TSV), override JSONs, pin,
`pyproject.toml`, `constraints.txt`, and the resolved numpy/scipy/pandas versions.
Build and CI dependencies use the committed exact constraints. CI installs
dependencies before computing that fingerprint and deciding early exit.
Operational `runtime-settings.json` is excluded from this fingerprint.
The HGNC URL points to a dated quarterly archive snapshot, with SHA-256 verified
before use. Early exit checks these fingerprints, parameters and options as
well as datahub commit/method. Before early-exiting a matching release, the
workflow also compares the current API cohort mapping fingerprint.
When prechecks run, `source.precheck_expires_at` records the earliest expiry
of the counts used in that release. Expired counts trigger rebuilding even if
the artifact was generated more recently. Subset and skip-precheck workflow
builds always produce downloadable review artifacts and must never be published as releases. Scheduled/full precheck builds
require NCBI_API_KEY. The PubMed cache is saved with an always-run step, including
after build failure or timeout.

Consumers must ignore unknown fields and reject unknown `schema_version`.


### Release transport and atomicity

- Tag: `expression-stats-<release_id>-pubmed-<YYYYMMDD>`.
- Name: `Expression stats <release_id> (PubMed <YYYY-MM-DD>)`.
- `pubmed_snapshot_date` is the UTC date the completed build began; counts may
  come from the existing 30-day TTL cache. It is a bundle snapshot date, not a
  claim that every PubMed count was fetched on that date. Earliest count expiry
  remains `source.precheck_expires_at`. Readers requiring PubMed freshness must
  check `source.precheck_expires_at`, not the tag date or snapshot date.
- Releases are not prereleases and are created and undrafted with
  `make_latest=false`, preserving the application's Latest release.
- Assets: `manifest.json`, `genes.jsonl.gz`, `controls-report.json`, and existing `aliases.json` with
  its existing name and bytes (and any future existing index JSON unchanged).
  Manifest retains all existing fields and adds `bundle_tag` (the exact tag),
  `pubmed_snapshot_date` (`YYYY-MM-DD`), `gene_count` (number of JSON lines),
  `sha256` (lower-case SHA-256 hex of compressed `genes.jsonl.gz`), and
  `size_bytes` (compressed asset byte length). Consumers verify size/hash before import.
- Publish atomically: create draft, upload all assets, download each and verify
  byte size and SHA-256, then undraft. Upload/verification failures best-effort
  delete only the draft created by that invocation, then re-raise the failure.
  If cleanup fails or the job is interrupted, a matching existing draft fails
  with `stale draft <id>; delete it`; an existing published tag skips successfully.
  Release creation sets `target_commitish` to the building `GITHUB_SHA`.
  Partial gene or skipped-precheck builds are
  review artifacts only and never published. Missing NCBI_API_KEY fails fast
  before dependency installation for any requested precheck build.
- Discovery: paginate the repository's releases, filter non-draft tags beginning
  `expression-stats-`, select newest by `published_at`, then download assets.
  Refreshing PubMed leaves the date-free `release_id` unchanged but yields a new
  dated tag, so readers detect refreshes while reusing literature verdicts.
- Full fresh matching manifests may be reused until PubMed expiry; this checks
  input fingerprints and current cohort mapping. Existing matching tags are
  also skipped immediately before upload if published; stale drafts fail loudly.
  Generated artifacts are not committed.
- The build job has only `contents: read`; checkout never persists credentials.
  A read token is scoped to the release-discovery step, never pip or data builds.
  Bundles cross to a separate publisher via Actions upload/download-artifact.
  Only that job has `contents: write`, and its GH_TOKEN is scoped to publication.
  Publishing requires `github.ref == 'refs/heads/main'`; branch dispatches always
  build review artifacts, even when a matching published build is fresh.
- Workflow concurrency is keyed by event name and ref, so queued manual runs
  cannot replace pending cron runs. Same-event/ref pending runs may coalesce
  under GitHub's concurrency policy. Builds with prechecks share the
  `expression-stats-pubmed` concurrency group with `cancel-in-progress: false`,
  so cron and manual builds (including branch dispatches) cannot simultaneously
  use the shared NCBI_API_KEY. This serializes the entire build, including cache
  restore/save for prechecked builds. Skipped prechecks instead use a unique
  per-run group, cannot displace a pending cron precheck, and never restore/save
  the PubMed cache (so they cannot shadow newer resumable counts). GitHub retains at most one pending job
  per shared group; a newer pending job can replace an older one, while running
  jobs are never cancelled. The event/ref workflow groups remain separate so
  manual runs cannot replace pending cron runs at the workflow queue level.
  A shared `expression-stats-publish` job
  concurrency group serializes all main-branch publications, preventing tag
  races across schedule and dispatch. Running jobs are never cancelled.
  Since only schedule/main and dispatch/main reach publication, each with one
  active workflow run, at most one publisher waits behind the active publisher.

## ACGC API (when enabled)

- `POST /v1/expression-context/jobs` `{ "gene": "ERBB2" }` → 202
  `{job_id, status_url}`. Runs the literature gate for each pair with
  `stats_pass && (literature_precheck_pass is not false)`. Reuses stored verdicts
  for the same
  (gene, oncotree_code, release_id, literature_gate).
- `GET /v1/expression-context/jobs/{job_id}` → status + result when done.
- `GET /v1/expression-context/genes/{gene}` → latest stored result.
  `surfaced` pairs by default; `?include_excluded=true` adds excluded pairs with
  a reason (`stats`, `precheck`, `literature`).
- `GET /v1/expression-context/genes?limit&cursor` → history of genes run.
- All behind `require_auth`.

Literature verdict (v1, `literature_gate = "acgc_pipeline_v1"`): run the
existing pipeline with `tumor_type = tumor_type_query`; supported iff
`insufficient_evidence` is false AND ≥1 cited PMID. Stored with PMIDs and
rationale.

## Table `expression_context_results`

`hugo_symbol, oncotree_code, release_id, literature_gate, surfaced (bool),
exclusion_reason, stats_json, evidence_json, computed_at, requested_by`;
unique `(hugo_symbol, oncotree_code, release_id, literature_gate)`.

## Later

- Swap `LiteratureGate` to LRAS when `/lras/v1/retrieve` ships.
- Normal-tissue comparison (TCGA adjacent / GTEx) as a separate track.

### Contract 1.4: supplementary tumor-purity gate

Purity only demotes an existing high `confidence_tier` to `candidate`. It never
changes v3 statistics, `stats_pass`, `pass_mode`, broad-expression flags, alteration
results, or the strong cutoffs. `purity_check` is optional in the schema and is
`null` for pairs that were not high before this gate. High pairs receive `pass`,
`demoted`, `not_applicable`, or `unassessed`. `purity_rho` and `purity_q` retain
within-cohort Spearman correlation and exploratory BH adjustment for transparency
(on all v3 passes; unavailable correlation is null and enters BH as p=1).

Rule R2 exactly follows the read-only evaluation: within-cohort Spearman
rho(log2(RSEM+1), ABSOLUTE purity) <= -0.30 and q < 0.05, with >=30 matched
finite samples, AND failure to remain strong when both target and pooled rest
are restricted to purity >=0.6, with >=30 retained target samples. The correlation
BH family is every existing v3 pass across all genes/cohorts (including exempt
cohorts and unavailable tests). Correlations require >=10 finite samples and
nonconstant vectors. Filtered statistics recompute pooled P99/P95, effect sizes,
MW/Fisher tests and per-gene BH across all 32 cohorts; the original broad flag is
frozen and lineage wins before subgroup. All thresholds are in `PARAMS` and change
the release fingerprint. A subset build uses its subset's v3-pass BH family and
is review-only, as before.

DLBC, LAML, THYM are `not_applicable`; insufficient matched or retained target
coverage is `unassessed` (including CHOL with 24 high-purity samples). Both states
retain high but mean **unassessed by this gate, not validated**. Constant-vector
correlations are also unassessed. Correlation is association with bulk cell
mixture, not evidence of the expressing cell; R2 can retain stromal signals and
cannot establish tumor-cell expression. These selected-family q values are
exploratory, not genome-wide or post-selection FDR guarantees.

ACGC requires a known retaining purity state for high pairs in contract 1.4+;
missing/null/unknown states fail closed. Only a well-formed pre-1.4 version keeps
legacy behavior. The loader rejects a 1.4+ high pair missing the field; the service
also checks independently, including seeded bundles and stored supported
literature. A `demoted` pair is always candidate, even if its stored tier says
high, and never triggers paid literature work or surfaces on any read/export
path. Its “Needs verification” badge explains “Expression may come from non-tumor
cells (falls with tumor purity)”. Coverage/exemption notes appear in Details and
the per-pair CSV includes `purity_check`. Controls and publish-time rechecks keep
the existing v3 gate; positive tiers are reported separately.

Purity source: GDC PanCanAtlas `TCGA_mastercalls.abs_tables_JSedit.fixed.txt`,
[download](https://api.gdc.cancer.gov/data/4f277128-f793-4354-a13d-30cc7fe9f6b5),
SHA-256 `f430a975433d82e0098d7405619d4f12a0c765fcd97e7d63cc9b1de7f2d763cd`.
The content-addressed cache verifies both cached and downloaded bytes; mismatch
fails the build. URL, version and hash are in manifest provenance and the release
fingerprint. Use only `call status=called`, finite purity in [0,1], joined on the
first 15 barcode characters to the exact selected primary expression sample;
patient-only matches are never used. Duplicate called keys fail closed. DNA and
RNA can represent different aliquots of the selected sample.

### Contract 1.5: either-arm strong criteria and `strong_arm`

`confidence_tier` is `"high"` when a v3 pass meets the strong cutoffs of **either** arm,
not only the arm recorded in `pass_mode`. The lineage strong criteria keep the existing
broad-expression block (a broad-blocked lineage pass cannot be high via lineage); the
subgroup strong criteria retain the base subgroup v3 gate and are evaluated regardless
of `pass_mode`; fraction and ratio alone cannot promote a lineage pass. No v3 threshold,
`stats_pass`, `pass_mode`, broad flag,
statistic, flag or PubMed field changes. The release identity changes because the tier
derivation is code, not because any parameter moved.

The additive `strong_arm` field records which arm(s) produced a high tier
(`"lineage"`, `"subgroup"`, `"both"`), and is null when the pair is not high (including
after a purity demotion). It is optional in the schema and ignored by readers that
predate 1.5. Purity demotion (contract 1.4) applies unchanged after promotion and its
high-purity re-test evaluates both arms' strong statistics. PubMed precheck coverage is
unchanged: every `stats_pass` pair is checked, so newly promoted candidates already have
their counts.

We acknowledge TCGA, GDC, and the ABSOLUTE/PanCanAtlas source for these open-access
data: [PanCanAtlas supplemental data](https://gdc.cancer.gov/about-data/publications/pancanatlas).
No software/SPDX licence is asserted for the table. See the
[GDC data-access policy](https://docs.gdc.cancer.gov/Encyclopedia/pages/Data_Access_Policy/)
and [analysis policy](https://gdc.cancer.gov/analyze-data/data-analysis-policies).
