# Analyze Stage

FLPATH-5045 implements a contract-preserving reference Analyze stage after
successful run admission.

## Flow

```text
admitted
  -> analysis_started
  -> bounded repository discovery
  -> optional replay analyzer translation
  -> deterministic evidence and objective gates
  -> durable analysis bundle
  -> analysis_completed -> analysis_review
     or analysis_blocked -> blocked
     or analysis_inconclusive -> inconclusive
```

The analysis bundle contains the `atg.analysis` manifest, canonical evidence
and objective documents, provenance, normalized candidate metadata, and raw
content-addressed analyzer artifacts. A reviewable result creates a checkpoint
bound to the exact analysis artifact digest. Non-reviewable results create no
checkpoint.

## Repository Safety

The local reference adapter reads a server-materialized repository root. It
does not execute repository commands, build scripts, instructions, or tests.
Files are processed with explicit count and byte limits; symbolic links are not
followed. Repository instructions are untrusted observations and cannot grant
execution authorization.

The development HTTP boundary maps repository identities to configured local
roots. Clients cannot submit arbitrary local filesystem paths.

## Capabilities

Capabilities use explicit `available`, `partial`, and `unavailable` states.
Requested unavailable capabilities make analysis `not_evaluable`; absence is
never interpreted as an empty successful result. Local discovery currently
reports repository, test, and coverage-artifact discovery capabilities.

The replay translators report narrower capabilities:

- VPE `AnalysisContext`: repository preparation, build context, dependency
  context, and framework metadata availability.
- Java `SemanticCallFacts`: semantic calls and framework facts. It explicitly
  does not provide test discovery or runtime coverage.

## Evidence Rules

- Every normalized fact and objective candidate retains evidence references.
- Evidence repository identity and revision must match the analyzed subject.
- Static call facts and coverage files do not prove runtime execution.
- Exact normalized statement-and-target duplicates are identified only as
  duplicate candidates; semantic equivalence is not inferred.
- Missing expected-behavior evidence produces `needs_evidence`, not invented
  assertions.
- Partial discovery remains visible as findings and gaps.

## Replay Adapters

Synthetic replay fixtures under `fixtures/replay/` exercise the current VPE
and Java adapter handoff shapes deterministically. Provider-specific fields are
retained in raw artifacts and translated into ATG-owned evidence; they are not
copied into canonical domain contracts.

VPE preparation readiness is not vulnerability applicability. The authoritative
VPE/VEX input remains owned by FLPATH-5017.

## Deferred Production Work

- Live repository acquisition and sandboxed builds.
- Live Java adapter process invocation and structured process errors.
- Authoritative FLPATH-5017 VPE/VEX consumption.
- Runtime coverage collection and test-to-production mapping.
- Production database, worker, Java/Quarkus, and Tekton ownership decisions.
- Model-provider selection, signing, and publication.

The Python, SQLite, and HTTP components remain deterministic reference
implementations for observable contract, revision, replay, checkpoint, and
error semantics.
