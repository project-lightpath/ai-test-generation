import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.adapters.analysis.java_facts import JavaSemanticFactsReplayAdapter, _digest  # noqa: E402
from atg.adapters.analysis.vpe_context import VPEAnalysisContextReplayAdapter  # noqa: E402
from atg.domain.objective_candidates import derive_objective_candidates  # noqa: E402
from atg.domain.repository_analysis import RepositoryContextResult, RepositoryProfile  # noqa: E402
from atg.domain.repository_analysis import RepositorySubject  # noqa: E402
from atg.domain.schema_validation import validate_contract_document  # noqa: E402


FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures" / "replay"
SUBJECT = RepositorySubject("https://git.example/fixture.git", "abc123")


class AnalysisReplayTests(unittest.TestCase):
    def test_vpe_context_preserves_preparation_identity_and_limitations(self):
        document = json.loads((FIXTURES / "vpe-analysis-context.json").read_text())
        result = VPEAnalysisContextReplayAdapter(document).collect(SUBJECT)

        self.assertEqual(result.evidence[0]["subject"]["version"], "abc123")
        for document in result.evidence:
            validate_contract_document(document, "evidence")
        self.assertTrue(any(fact.fact_id == "fact-vpe-framework" for fact in result.facts))
        self.assertEqual(
            {capability.capability: capability.status for capability in result.capabilities}["framework-metadata"],
            "partial",
        )
        self.assertTrue(any(finding.kind == "incomplete" for finding in result.findings))
        self.assertEqual(result.artifacts[0].media_type, "application/vnd.project-lightpath.vpe-analysis-context+json")

    def test_java_facts_preserve_certainty_and_abstain_from_coverage(self):
        document = json.loads((FIXTURES / "java-semantic-call-facts.json").read_text())
        result = JavaSemanticFactsReplayAdapter(document).collect(SUBJECT)

        self.assertEqual(len(result.facts), 2)
        for evidence in result.evidence:
            validate_contract_document(evidence, "evidence")
        self.assertEqual(result.facts[0].source_path, "src/main/java/com/example/Resource.java")
        self.assertEqual((result.facts[0].certainty, result.facts[0].status), ("definite", "emitted"))
        self.assertEqual((result.facts[1].certainty, result.facts[1].status), ("possible", "emitted"))
        self.assertTrue(any(finding.kind == "ambiguous" for finding in result.findings))
        capabilities = {capability.capability: capability.status for capability in result.capabilities}
        self.assertEqual(capabilities["java-semantic-calls"], "available")
        self.assertEqual(capabilities["runtime-coverage"], "unavailable")
        self.assertNotIn("execution", {evidence["category"] for evidence in result.evidence})

    def test_replay_subject_mismatch_remains_explicit(self):
        other = RepositorySubject("https://git.example/fixture.git", "different")
        for adapter, fixture in (
            (VPEAnalysisContextReplayAdapter, "vpe-analysis-context.json"),
            (JavaSemanticFactsReplayAdapter, "java-semantic-call-facts.json"),
        ):
            with self.subTest(adapter=adapter.__name__):
                document = json.loads((FIXTURES / fixture).read_text())
                result = adapter(document).collect(other)
                self.assertTrue(any(finding.kind == "conflicting" for finding in result.findings))

    def test_unsupported_replay_versions_emit_no_facts_and_unavailable_capabilities(self):
        for adapter, fixture in (
            (VPEAnalysisContextReplayAdapter, "vpe-analysis-context.json"),
            (JavaSemanticFactsReplayAdapter, "java-semantic-call-facts.json"),
        ):
            with self.subTest(adapter=adapter.__name__):
                document = json.loads((FIXTURES / fixture).read_text())
                document["schemaVersion"] = "9.9.9"
                result = adapter(document).collect(SUBJECT)
                self.assertEqual(result.facts, ())
                self.assertEqual(result.evidence, ())
                self.assertTrue(all(capability.status == "unavailable" for capability in result.capabilities))
                self.assertTrue(any(finding.kind == "unsupported" for finding in result.findings))

    def test_replay_rejects_missing_timestamps_nonobjects_and_preserves_abstentions(self):
        document = json.loads((FIXTURES / "java-semantic-call-facts.json").read_text())
        document["runMetadata"].pop("preparedAt")
        result = JavaSemanticFactsReplayAdapter(document).collect(SUBJECT)
        self.assertEqual(result.evidence, ())
        self.assertTrue(any(finding.finding_id.startswith("finding-java-prepared-at") for finding in result.findings))

        document = json.loads((FIXTURES / "java-semantic-call-facts.json").read_text())
        document["providerFacts"].append("not-an-object")
        document["providerFacts"].append({
            "id": "abstained-1", "caller": "a", "callee": "b", "relationshipType": "reflection",
            "certainty": "unresolved", "status": "abstained", "evidence": [],
        })
        result = JavaSemanticFactsReplayAdapter(document).collect(SUBJECT)
        self.assertTrue(any(finding.kind == "incomplete" for finding in result.findings))
        abstained = next(evidence for evidence in result.evidence if "abstained" in evidence["claims"][0])
        validate_contract_document(abstained, "evidence")
        self.assertTrue(any("abstained" in limitation["description"] for limitation in abstained["limitations"]))
        self.assertEqual(
            (next(fact for fact in result.facts if fact.status == "abstained").certainty, "abstained"),
            ("unresolved", "abstained"),
        )

        vpe = json.loads((FIXTURES / "vpe-analysis-context.json").read_text())
        vpe["provenance"].pop("preparedAt")
        result = VPEAnalysisContextReplayAdapter(vpe).collect(SUBJECT)
        self.assertEqual(result.evidence, ())
        self.assertIn("finding-vpe-prepared-at", {finding.finding_id for finding in result.findings})

    def test_missing_java_certainty_marks_evidence_uncertain_and_objective_needs_evidence(self):
        document = json.loads((FIXTURES / "java-semantic-call-facts.json").read_text())
        document["providerFacts"].append({
            "id": "missing-certainty",
            "caller": "com.example.Resource#handle",
            "callee": "com.example.Service#unknown",
            "relationshipType": "reflection",
            "status": "emitted",
            "evidence": [{"kind": "call-site", "path": "src/main/java/com/example/Resource.java", "line": 30}],
        })
        result = JavaSemanticFactsReplayAdapter(document).collect(SUBJECT)
        uncertain_evidence = next(
            evidence for evidence in result.evidence if "unresolved" in evidence["claims"][0]
        )
        validate_contract_document(uncertain_evidence, "evidence")
        self.assertTrue(any(item["code"] == "java-fact-uncertain" for item in uncertain_evidence["limitations"]))

        context = RepositoryContextResult(
            RepositoryProfile(SUBJECT, ("java",), ("maven",), ("pom.xml",), (), (), (), 1, "sha256:" + "a" * 64),
            (), (), (), (),
        )
        candidate_result = derive_objective_candidates(({
            "objective_id": "objective-vulnerability",
            "statement": "Test exposure of the vulnerable behavior",
            "driver_type": "vulnerability",
            "kind": "regression",
            "evidence_refs": [uncertain_evidence["evidence_id"]],
            "target_refs": [],
        },), context, result.evidence)
        self.assertEqual(candidate_result.candidates[0].disposition, "needs_evidence")
        self.assertEqual(candidate_result.candidates[0].expected_behavior_source, "unresolved")

    def test_missing_java_status_is_abstained_and_required_capability_is_not_falsely_available(self):
        document = json.loads((FIXTURES / "java-semantic-call-facts.json").read_text())
        document["providerFacts"] = [{"caller": "a", "callee": "b", "relationshipType": "invokes", "certainty": "definite", "evidence": []}]
        result = JavaSemanticFactsReplayAdapter(document).collect(SUBJECT)
        self.assertEqual(result.facts[0].status, "abstained")
        self.assertEqual(next(capability for capability in result.capabilities if capability.capability == "java-semantic-calls").status, "available")
        self.assertTrue(any(item["code"] == "java-fact-uncertain" for item in result.evidence[0]["limitations"]))

        document["providerFacts"] = []
        result = JavaSemanticFactsReplayAdapter(document).collect(SUBJECT)
        self.assertEqual(next(capability for capability in result.capabilities if capability.capability == "java-semantic-calls").status, "unavailable")

    def test_malformed_vpe_metadata_does_not_advertise_capabilities(self):
        document = json.loads((FIXTURES / "vpe-analysis-context.json").read_text())
        document["buildEvidence"] = {"untrusted": True}
        document["provenance"]["dependencyFingerprint"] = "not-a-digest"
        document["frameworkMetadata"] = [{"kind": "source-annotations", "status": "garbage"}]
        result = VPEAnalysisContextReplayAdapter(document).collect(SUBJECT)
        statuses = {capability.capability: capability.status for capability in result.capabilities}
        self.assertEqual(statuses["build-context"], "unavailable")
        self.assertEqual(statuses["dependency-context"], "unavailable")
        self.assertEqual(statuses["framework-metadata"], "unavailable")
        self.assertTrue(any(finding.kind == "unsupported" for finding in result.findings))

    def test_duplicate_java_facts_are_not_emitted_twice(self):
        document = json.loads((FIXTURES / "java-semantic-call-facts.json").read_text())
        document["providerFacts"].append(dict(document["providerFacts"][0]))
        result = JavaSemanticFactsReplayAdapter(document).collect(SUBJECT)
        self.assertEqual(len(result.facts), 2)
        duplicate_finding = next(
            finding for finding in result.findings if finding.finding_id.startswith("finding-java-duplicate-")
        )
        expected_id = "finding-java-duplicate-" + _digest(document["providerFacts"][0]).removeprefix("sha256:")[:16]
        self.assertEqual(duplicate_finding.finding_id, expected_id)
        self.assertEqual(duplicate_finding.kind, "conflicting")


if __name__ == "__main__":
    unittest.main()
