# Leads Discovery

A resumable Python pipeline for finding and ranking US/Canadian PVF distributor prospects from public evidence.

M2 performs the paid discovery, research, and structured extraction work. M3 is a deterministic local derivation layer: it validates cited facts, computes score and evidence coverage separately, makes conservative accepted/rejected/uncertain decisions, exports reviewable artifacts, and compares manual A/B/C labels without changing policy automatically.

## Setup

Python 3.12+ is required.

```bash
python -m pip install -e .
```

For live M2-backed runs, provide credentials through the environment:

```text
EXA_API_KEY=
DEEPSEEK_API_KEY=
APIFY_TOKEN=        # optional; missing token disables optional Apify discovery
```

Do not put credentials on the command line or in committed files.

## Commands

### Full run

A full run is dry by default. Without `--execute-live`, it does not read provider credentials, create provider clients, or make network calls.

```bash
python -m leads_discovery run \
  --run-id RUN \
  --deepseek-budget-usd 1.00
```

Authorize paid work explicitly:

```bash
python -m leads_discovery run \
  --run-id RUN \
  --max-candidates 100 \
  --max-evaluated 20 \
  --exa-budget-usd 1.00 \
  --exa-request-reservation-usd 0.10 \
  --deepseek-budget-usd 1.00 \
  --include-apify \
  --apify-budget-usd 0.25 \
  --execute-live
```

The Exa, Apify, and DeepSeek ceilings are independent. There is no aggregate budget. Existing provider spend is replayed from M2's append-only usage ledger on resume, so restarting a run does not reset spend.

`--exa-request-reservation-usd` is the operator-supplied conservative upper bound on the charge of **one Exa HTTP request**. It applies to Exa discovery and to each bounded Exa research request. Before every Exa dispatch, M2 requires `known Exa spend + request reservation <= Exa budget`. Live execution fails configuration if either the Exa budget or a positive finite reservation is missing; do not authorize live Exa work when you cannot choose a safe finite per-request upper bound.

`--max-evaluated` is limited to `1..20` and is passed directly to M2 as the extraction cap. `--max-candidates` remains limited to `1..100`.

If M2 pauses with `paused_budget` or `paused_unknown`, the full runner still evaluates and exports every extraction that was already completed locally, then returns the original pause status. Re-run the same run ID after resolving the pause; M2 resumes/skips durable paid work and M3 safely recomputes its derived files.

The existing narrow M2 entry point remains supported:

```bash
python -m leads_discovery.pipeline.m2_batch --help
```

### Local score

```bash
python -m leads_discovery score --run-id RUN --max-evaluated 20
```

`score` reads existing M2 artifacts only. Its execution path does not read provider credentials, construct provider clients, append usage events, or make network/DNS calls.

### Local calibration

Start from `data/RUN/calibration_template.csv`, then create a label CSV containing at least:

```csv
company_id,manual_label,manual_notes
cmp_example,A,strong manual fit
```

Labels are exactly `A`, `B`, or `C` after trimming/case normalization. Partial labeling is allowed by omitting unlabeled companies.

```bash
python -m leads_discovery calibrate --run-id RUN --labels labels.csv
```

`calibrate` is also local-only and zero-provider-spend. It reports disagreements; it never edits scores, decisions, policy, checkpoint state, evidence, or usage.

## Score and coverage are different

M3 deliberately keeps two questions separate:

```text
score    = how positive are the usable facts we have?
coverage = how much of the configured evidence weight is usable?
```

Unknown, malformed, unsupported, low-confidence, or uncited facts do not become zero. They contribute neither positive nor negative points. Category scores normalize only over usable subfeature weight, while category coverage records how much configured evidence was usable.

The four product weights are:

```text
workload / pain likelihood   40
 economic fit                25
 low incumbent exposure      25
 direct pain evidence        10
```

The final score uses each category's product weight multiplied by that category's coverage. This prevents one known positive fact from receiving the influence of a fully researched category.

## Decisions

Hard rejection is evaluated first. A company is `rejected` only when one of these exact high-confidence rules fires:

- cited `pvf_relevant` is false at confidence `>= 0.85`;
- canonical country is outside US/CA and retained discovery provenance reports the same country code;
- canonical status is exactly `inactive` or `dead`;
- cited current direct-competitor customer status is true at confidence `>= 0.85`;
- employee count `<10`, one branch, no inside-sales/estimating presence, and no RFQ/quote workflow are all usable at confidence `>= 0.85`.

A low score alone never rejects.

Without a hard rejection, `accepted` requires every gate:

```text
PVF relevance true at confidence >= 0.75
final score >= 70
overall coverage >= 0.70
workload coverage >= 0.60
economic coverage >= 0.50
at least one incumbent fact usable, with no usable incumbent fact true
```

If any acceptance gate fails, the decision is `uncertain`. For example, a score of 82 with coverage 0.55 is uncertain rather than accepted.

Historical competitor evaluation is review context only; it does not lower the score or reject a company.

## Artifacts

M2 keeps its append-only paid-work artifacts. M3 writes separate derived files under `data/<run_id>/`:

```text
companies_evaluated.jsonl
companies_ranked.csv
companies_rejected.csv
companies_uncertain.csv
calibration_template.csv
calibration_report.json       # after calibrate
companies_calibrated.csv      # after calibrate
run_summary.json
```

`companies_evaluated.jsonl` contains one complete evaluated snapshot per selected company and is atomically replaced on recomputation.

CSV ranking is deterministic: accepted, uncertain, rejected; then final score descending; overall coverage descending; normalized name and company ID ascending. Missing category scores are blank. Externally sourced CSV text beginning, after whitespace, with `=`, `+`, `-`, or `@` is prefixed with an apostrophe to prevent spreadsheet formula execution. JSON keeps the original text.

`run_summary.json` records the policy version, M2 checkpoint state, decision counts, relative M3 artifact names, and the existing provider usage totals without inventing a combined exact cost when provider events do not supply one.

## Calibration report

Calibration builds a complete manual-label by machine-decision matrix and reports:

- critical disagreements: manual `A` + rejected, manual `C` + accepted;
- review disagreements: manual `A` + uncertain, manual `B` + accepted/rejected, manual `C` + uncertain;
- per-label final-score and overall-coverage summaries when labels are present.

Calibration is report-only. A human can use the report to propose a later versioned policy change; M3 never tunes `m3-v1` automatically.

## Safety and scope

Derived files are written atomically inside the validated run directory and pre-existing symlink targets are rejected. Run IDs cannot escape the configured data root.

This project does not add a database, frontend, contact enrichment, outreach, CRM integration, autonomous agent, or LLM-based scoring. The LLM is used only by the existing M2 structured extraction stage; M3 scoring and decisions are deterministic local code.