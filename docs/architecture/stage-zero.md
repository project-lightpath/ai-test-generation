# Stage-Zero Controls

Stage zero is a control boundary before request-specific repository, model, or
execution work. It has two distinct results:

## Platform readiness

`atg.platform_readiness` describes service-level capability. It can be evaluated
at startup or on demand and does not consume a customer request. A warning is
allowed when the capability is not required by the deployment; required failed
or unevaluable checks make the platform blocked.

## Run admission

`atg.run_admission` describes one request. It validates the request envelope,
authorization, requested capabilities, and applicable policy before downstream
evidence is consumed. Admission is idempotent by request key and payload
digest. A required failed check creates an `admission_blocked` terminal event
and no downstream evidence record.

Target-specific checks such as immutable repository subjects, framework
profiles, and execution environments occur after admission as target
preconditions. They do not turn platform health into customer test evidence.

The local HTTP component uses deterministic checks and SQLite persistence. Live
VPE/VEX, Tekton, signing, and evidence-store adapters remain outside this
development slice.
