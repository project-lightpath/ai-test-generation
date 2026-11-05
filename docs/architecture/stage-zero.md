# Stage-Zero Controls

Stage zero is a control boundary before request-specific repository, model, or
execution work. It has two distinct results:

## Platform readiness

`atg.platform_readiness` describes service-level capability. It can be evaluated
at startup or on demand and does not consume a customer request. A warning is
allowed when the capability is not required by the deployment; required failed
or unevaluable checks make the platform blocked. Each check records its
timestamp, component identity/version, capability, and limitations. A degraded
capability blocks only requests that select that capability.

## Run admission

`atg.run_admission` describes one request. It validates the request envelope,
authorization, requested capabilities, and applicable policy before downstream
evidence is consumed. The HTTP boundary carries the transport retry identity in
the standard `Idempotency-Key` header; it is not part of `atg.request` or its
payload digest. Admission is also bound to durable request and run identities.
A required failed check atomically persists an `admission_blocked` event,
admission result, and `atg.run_terminal` record. The guarded evidence boundary
rejects publication for that run.

Target-specific checks such as immutable repository subjects, framework
profiles, and execution environments occur after admission as target
preconditions. They do not turn platform health into customer test evidence.

The local HTTP component uses deterministic checks and SQLite persistence. Live
VPE/VEX, Tekton, signing, and evidence-store adapters remain outside this
development slice.

FLPATH-5044 owns this admission and terminal-control boundary. Repository
analysis, planning, generation, execution, quality/differential evidence, and
final publication belong to TG-3 through TG-7 and REL-1.
