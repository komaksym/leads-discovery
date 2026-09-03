# Leads Discovery

A resumable Python pipeline for finding and ranking US/Canadian PVF distributor prospects from public evidence.

M2 performs the paid discovery, research, and structured extraction work. M3 is a deterministic local derivation layer: it validates cited facts, computes score and evidence coverage separately, makes conservative accepted/rejected/uncertain decisions, exports reviewable artifacts, and compares manual A/B/C labels without changing policy automatically. M4 is an explicit, separate contact-enrichment stage that consumes only M3 `accepted` companies, finds current employees close to the software-buying decision, enriches at most the top two contacts, verifies work email, and produces artifacts only. It performs no outreach.

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

For explicit live M4 enrichment, additionally configure:

```text
CLAY_PUBLIC_API_KEY=
CLAY_CONTACT_ROUTINE_ID=
APOLLO_API_KEY=
INSTANTLY_API_KEY=
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
  --deepseek-budget-usd 1.00 \
  --include-apify \
  --apify-budget-usd 0.25 \
  --execute-live
```

Normal discovery is batch-oriented. Set a market, repeat `--search-term` for additional
criteria, and restrict the target geography with `--target-geography`; none of these options
identifies a single company:

```bash
python -m leads_discovery run \
  --run-id RUN \
  --market "industrial pumps" \
  --search-term "regional distributors" \
  --search-term "RFQ workflow" \
  --target-geography CA \
  --max-candidates 50 \
  --max-evaluated 20 \
  --deepseek-budget-usd 1.00
```

The Exa, Apify, and DeepSeek ceilings are independent. There is no aggregate budget. Existing provider spend is replayed from M2's append-only usage ledger on resume, so restarting a run does not reset spend.

`--max-evaluated` is limited to `1..20` and is passed directly to M2 as the extraction cap. `--max-candidates` remains limited to `1..100`.

If M2 pauses with `paused_budget` or `paused_unknown`, the full runner still evaluates and exports every extraction that was already completed locally, then returns the original pause status. Re-run the same run ID after resolving the pause; M2 resumes/skips durable paid work and M3 safely recomputes its derived files.

The existing narrow M2 entry point remains supported:

```bash
python -m leads_discovery.pipeline.m2_batch --help
```

### Normal batch production flow (M1–M4)

A normal M1–M4 batch uses the same run ID across two explicit commands. `run` discovers, deduplicates, researches, canonicalizes, and evaluates the company batch; `enrich` then consumes only that run's current `accepted` companies. Keeping M4 as an explicit second command preserves a clean recovery and authorization boundary; it does not make the product single-company.

```bash
python -m leads_discovery run \
  --run-id RUN \
  --market "industrial pumps" \
  --search-term "regional distributors" \
  --search-term "RFQ workflow" \
  --target-geography US \
  --max-candidates 50 \
  --max-evaluated 20 \
  --exa-budget-usd 1.00 \
  --deepseek-budget-usd 1.00 \
  --execute-live

python -m leads_discovery enrich \
  --run-id RUN \
  --exa-people-budget-usd 1.00 \
  --max-contacts-per-company 3 \
  --max-paid-contacts-per-company 2 \
  --execute-live
```

Live authorization and batch cardinality are separate controls. `--execute-live` authorizes provider calls; `--max-candidates`, `--max-evaluated`, and the contact caps only bound how much work may be attempted. Raising a cap never turns a dry command into a live one.

Normal batch artifacts remain runner-local under `data/RUN/`. The normal `run` and `enrich` commands do not publish them. Publication exists only at an explicitly approved boundary such as the fixed production canary workflow described below.

### Contact enrichment

M4 is a separate command. It never runs implicitly from `run`, `score`, or `calibrate`.

Dry mode is the default and is intentionally side-effect free: it validates scalar arguments only and does not read provider credentials, construct live clients, touch run artifacts, or access the network.

```bash
python -m leads_discovery enrich --run-id RUN
```

Explicit live execution requires an Exa People Search USD ceiling and the configured Exa, Clay, Apollo, and Instantly credentials:

```bash
python -m leads_discovery enrich \
  --run-id RUN \
  --exa-people-budget-usd 1.00 \
  --clay-max-contacts 10 \
  --apollo-credit-cap 5 \
  --instantly-verification-call-cap 5 \
  --execute-live
```

M4 reads `companies_evaluated.jsonl` and only exact `accepted` companies can cause provider calls. `uncertain` and `rejected` companies cause zero Exa People, Clay, Apollo, and Instantly calls. It never expands the M3 universe beyond 20 evaluated companies.

Selection is deterministic. Exa returns at most 10 people per accepted company; M4 requires structured evidence that a person currently works there, ranks by distance to the buying decision, exact-deduplicates, and retains at most three contacts. Direct owners/executives rank first, relevant Sales/Operations/Commercial/Estimating/Inside Sales leadership second, and credible operational deputies third. Rank 3 is retained for review but never enters paid enrichment.

Only the first two retained contacts whose decision rank is 1 or 2 can enter the paid waterfall:

```text
Clay work-email Routine -> Apollo work-email fallback -> Instantly verification
```

Clay uses the configured asynchronous Routine and persists its `routine_run_id` before polling. Apollo is called only when Clay has no usable work email and always disables personal email, phones, and both waterfall flags. Instantly is used only for `/api/v2/email-verification`; a persisted `pending` result resumes with GET and never repeats POST. Missing email never removes a useful contact.

The Exa People USD ceiling, Clay submitted-contact cap, Apollo credit cap, and Instantly verification-call cap are independent. Known budget exhaustion publishes the best partial artifacts. Unknown paid in-flight outcomes fail closed instead of being blindly replayed.

M2 and M4 use one `PaidOperationLifecycle` boundary for paid dispatches: replayed budget admission happens before the call, intent is checkpointed before dispatch, usage is appended to the authoritative ledger, and only a known result can clear the replay barrier. A healthy completed rerun repairs a stale derived usage summary without re-calling providers or rewriting durable state.

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

M4 adds five separate files without mutating the M2/M3 ledgers:

```text
contacts.jsonl
leads.csv
contact_usage_events.jsonl
contact_usage.json
contact_checkpoint.json
```

`contacts.jsonl` is the canonical atomic M4 contact snapshot. `leads.csv` is the primary human-review artifact and includes company score, contact identity/title/rank, work email and verification status, profile URLs, and email source. Ordering is company score descending, decision rank ascending, normalized contact name, then contact ID. Existing CSV formula-injection protection is preserved.

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

M4 adds contact discovery and work-email verification only. The project still does not add a database, frontend, CRM integration, autonomous SDR, phones, personal emails, lead creation in Instantly, campaigns, sequences, SuperSearch jobs, or outreach. Development and automated validation must use $0 provider spend.

## Production canary

Production execution does not depend on a local computer. The production entry point is the manual `Production lead canary` GitHub Actions workflow on a standard GitHub-hosted `ubuntu-latest` runner. Provider credentials live only in GitHub Actions repository secrets.

The canary is deliberately a credentialed smoke test, not the normal batch entry point. Its command surface exposes only run identity/data location and hard-codes one candidate, one evaluation, and one paid contact. Market/search criteria and batch cardinality belong to the normal `run` + `enrich` flow above; the canary cannot be widened into that configuration.

The workflow has no safety-limit inputs: the application fixes the canary at one company, one paid contact, tiny provider quotas, and tiny spend/storage ceilings. Paid-operation barriers are written durably before dispatch so a runner restart cannot silently repeat an unresolved potentially billed operation.

Only `leads.csv` and `contacts.jsonl` are published to the dedicated `generated-leads` Git branch. Checkpoints, usage ledgers, provider payloads, credentials, temporary files, and debug state are not published as branch files or Actions artifacts.

A real credentialed one-company workflow run is the final external acceptance gate. Automated CI and development remain offline and do not prove live provider compatibility.
