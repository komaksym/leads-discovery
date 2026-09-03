# Architecture and Safety Contract

This document is the durable source of truth for the integrated M1–M4 production architecture. Historical design specs remain useful rationale, but completed implementation plans are not normative.

## Product flow

```text
search criteria
  -> bounded company discovery
  -> deterministic deduplication with provenance
  -> bounded research evidence
  -> candidate fact extraction
  -> proposition-aware evidence support
  -> deterministic company evaluation
  -> accepted companies only
  -> deterministic contact shortlist
  -> bounded work-email enrichment and verification
  -> runner-local contacts.jsonl + leads.csv
```

The normal product is a batch discovery pipeline. The one-company production canary is a separate credentialed smoke test and must not become the normal batch entry point.

## Paid-operation lifecycle

Every paid M2 and M4 dispatch crosses one shared safety boundary:

1. Validate configured provider budget/quota and reserve worst-case next-operation cost where applicable.
2. Persist durable operation intent before dispatch.
3. Dispatch at most once for that admitted operation identity.
4. Record known usage in the append-only authoritative ledger.
5. Mark a known result, or leave an unresolved unknown outcome that freezes later paid work.
6. Rebuild derived usage summaries from the authoritative ledger; summaries are not accounting authority.

A completed healthy rerun must perform zero provider calls. An ambiguous potentially billed outcome must never be treated as fresh work.

## Evidence support

Provider/LLM extraction proposes candidate facts; it does not directly create domain truth. A deterministic support boundary validates cited evidence before facts can affect scoring or hard rejection.

Unsupported facts become unknown. In particular, unrelated negation such as "we do not manufacture pipe" cannot independently establish that a company is not PVF-relevant when the same evidence positively describes PVF distribution.

M3 scoring remains deterministic and consumes canonicalized facts.

## M4 authorization and selection

Only the current accepted-company set authorizes contact work. Persisted stale contacts cannot authorize new paid work.

Contact ranking/deduplication is deterministic. Paid enrichment is limited to the configured top eligible contacts. Provider-specific contact transport remains behind the existing M4 orchestration boundary.

## Transport and persistence safety

Provider adapters own explicit request timeouts and bounded streamed response reads. Oversized declared bodies are rejected before consumption when possible, and chunked/no-length responses stop at the first chunk crossing the configured ceiling.

Persisted run state must stay inside the configured run root, reject unsafe path/symlink escapes, bound replay/storage, and preserve append-only authoritative usage history.

## Publication and canary boundaries

Local/dry commands do not authorize live provider work. Public CI must not publish prospect/contact artifacts. The credentialed production canary is manual-only, fixed to intentionally tiny ceilings, and is run only after the offline safety gate is green.

## Permanent verification contract

The integrated tree must keep behavioral coverage for:

- exact/above-budget admission and per-request reservations;
- durable intent, replay barriers, global unknown-outcome freeze, and completed-run idempotence;
- accepted-only M4 authorization, stale-contact rejection, deterministic selection, and malformed persisted state;
- proposition-aware negative-evidence handling;
- streamed response byte bounds, explicit timeouts, bounded retry behavior, and secret-safe errors;
- authoritative usage replay/summary repair, path containment, symlink rejection, and storage/replay bounds;
- manual-only workflow security, canary ceilings, and offline no-network behavior.

Tests should assert observable behavior at public orchestration/provider seams. Source-inspection assertions about helper placement are not product requirements.

## Historical rationale

The retained design specs under `docs/superpowers/specs/` document milestone-specific rationale. They are historical design context; this document and the executable behavioral tests describe the current integrated contract.
