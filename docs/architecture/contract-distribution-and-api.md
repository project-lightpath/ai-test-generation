# Contract Distribution And API Boundary

## HTTP API Decision

An HTTP API is not required merely because ATG uses JSON contracts. A
Tekton-only implementation can exchange contract artifacts through an
approved workspace/object store and append state through a worker or job.

An ATG coordinator API becomes justified when the system needs:

- Human review clients to read and decide checkpoints.
- Multiple independent workers updating one run.
- Concurrent revision compare-and-swap writes.
- Run status, retry, cancellation, and recovery from outside Tekton.
- Central authorization, audit, and evidence publication.

The local HTTP component is deliberately a reference boundary for these tests;
it is not a commitment to a public API. If the coordinator service is adopted,
OpenAPI should describe that API while the JSON Schemas remain the canonical
document payload definitions.

## Schema Publication Options

### GitHub Pages

Publish versioned schema files at the `$id` URLs, for example:

```text
https://project-lightpath.github.io/ai-test-generation/contracts/v1/request.schema.json
```

This is convenient for human review and standards tooling. Publish only from a
protected branch or release tag, keep versioned paths immutable, and validate
all schemas and examples before deployment. Runtime consumers should still
bundle or register schemas locally rather than require live network access.

### GitHub Release Assets

Attach a versioned schema bundle to a release. This is simpler than maintaining
a documentation site and provides release-level immutability, but the `$id`
URLs should not point at a mutable `main` or `raw` URL.

### Internal Artifact Or OCI Registry

Publish a schema bundle as an internal Maven/PyPI/npm package or OCI artifact.
This is preferable when schemas contain internal details or production systems
must consume approved, access-controlled artifacts. It is less convenient as a
public `$id` resolver and should usually be paired with a local registry.

## Recommendation

During early development, publish the current schemas from every `main` commit
to GitHub Pages. The repository workflow supports a manual override profile for
destructive experiments under `contracts/overrides/`. Once a real consumer
exists, stop publishing destructive changes under the same path and move to
versioned release paths.

Use release-tagged bundles or an internal artifact registry as the operational
distribution mechanism after consumer adoption. Keep the runtime resolver local
and pin the schema bundle version.

The Pages artifact name includes the workflow run ID and attempt number. This is
intentional: rerunning a failed publication can leave the previous artifact in
the same workflow run, and `actions/deploy-pages` rejects multiple artifacts
with the same name.

The current `$id` namespace is valid as a future publication namespace, but it
is not evidence that the URLs are already reachable.

## Signed Differential Direction

DSSE provides a generic signed envelope containing payload bytes, a payload type,
and one or more signatures. It does not define the meaning of an ATG result.
in-toto provides the semantic attestation model: subjects/materials and a
typed predicate describing what happened. An ATG differential could eventually
be an in-toto predicate carried in a DSSE envelope, with a payload type such as:

```text
https://project-lightpath.github.io/ai-test-generation/attestations/signed-differential/v1
```

The predicate would identify the base/candidate execution subjects, quality
result, provenance, comparisons, and outcome. The current custom signature
object is therefore provisional. Cryptographic implementation can remain
deferred, but the contract should reserve the payload type, signer identity,
and verification result rather than inventing a private signature protocol.

Canonical JSON is not required to decide the contract shape. If the payload is
later signed as JSON, RFC 8785 JCS is a candidate canonicalization scheme; the
choice should be made together with the signer/verifier implementation.
