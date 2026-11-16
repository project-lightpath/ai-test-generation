"""Optional SSC/Tekton compatibility projection for canonical ATG results."""

from __future__ import annotations

from atg.domain.digests import digests_equal
from atg.ports.generation import MaterializationResult, SscTektonCompatibilityResult


def to_ssc_tekton_compatibility(
    generation_record,
    materialization: MaterializationResult | None,
    *,
    pr_url: str | None = None,
    run_ref: str | None = None,
    evidence_ref: str | None = None,
) -> SscTektonCompatibilityResult:
    """Project optional legacy fields without changing the canonical record."""
    manifest = generation_record.manifest
    status = manifest.get("status")
    if status not in {"generated", "verified", "rejected", "inconclusive"}:
        raise ValueError("unsupported generation terminal status")
    tests_added: tuple[str, ...] = ()
    if status in {"generated", "verified"}:
        if materialization is None:
            raise ValueError("successful compatibility result requires materialization")
        if (
            not digests_equal(materialization.patch_digest, manifest.get("patch_digest"))
            or not digests_equal(
                materialization.materialized_test_digest,
                manifest.get("materialized_test_digest"),
            )
        ):
            raise ValueError("compatibility materialization differs from canonical generation")
        tests_added = materialization.changed_files
    for name, value in (("PR_URL", pr_url), ("RUN_REF", run_ref), ("EVIDENCE_REF", evidence_ref)):
        if value is not None and (not isinstance(value, str) or len(value) > 2048):
            raise ValueError(f"{name} must be a bounded string")
    return SscTektonCompatibilityResult(
        tests_added=tests_added,
        pr_url=pr_url,
        run_ref=run_ref,
        evidence_ref=evidence_ref,
        terminal_status=str(status),
    )
