# ATG Service Ports

The persistence port in `run_state.py` defines the behavior required by the
ATG service: atomic event application, revision compare-and-swap,
event-id/payload-digest idempotency, snapshot loading, and journal replay.

The initial implementation may evolve from Python to Java/Quarkus or another
selected runtime without changing the JSON contracts or observable semantics.
