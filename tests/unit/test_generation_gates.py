import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "src"))

from atg.domain.generation import canonical_patch
from atg.gates.generation import evaluate_generation
from atg.ports.generation import PatchCandidate, ValidationResult


VALIDATION = ValidationResult(True, True, True, 1)


def evaluate(content, *, path="tests/test_generated.py", base_files=None, targets=("target-handler",)):
    candidate = PatchCandidate(
        canonical_patch(base_files or {}, {path: content}),
        {path: content},
        base_files or {},
    )
    status, _, checks = evaluate_generation(candidate, VALIDATION, targets)
    return status, {check["name"]: check["status"] for check in checks}


class GenerationGateTests(unittest.TestCase):
    def test_comments_and_string_literals_do_not_supply_assertions_or_targets(self):
        source = '''def test_unrelated():
    """assertTrue(True) target_handler"""
    # assertTrue(True) target_handler
    return "assertTrue(True) target_handler"
'''
        status, checks = evaluate(source)
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")
        self.assertEqual(checks["target"], "fail")

    def test_python_constant_assertion_is_not_meaningful(self):
        status, checks = evaluate(
            "def test_target_handler():\n    assert target_handler() is not None\n    assert True\n"
        )
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")
        self.assertEqual(checks["target"], "pass")

    def test_test_method_name_alone_does_not_satisfy_target_gate(self):
        status, checks = evaluate(
            "def test_target_handler():\n    assert actual == expected\n"
        )
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "pass")
        self.assertEqual(checks["target"], "fail")

    def test_java_test_scope_and_literals_are_checked(self):
        source = '''class GeneratedTest {
    // @Test void test_target_handler() { assertTrue(subject.valid()); }
    String example = "@Test void test_target_handler() { assertTrue(true); }";
}
'''
        status, checks = evaluate(source, path="tests/GeneratedTest.java")
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")
        self.assertEqual(checks["target"], "fail")

    def test_java_test_methods_with_throws_and_language_assertions_are_supported(self):
        source = '''class GeneratedTest {
    @org.junit.jupiter.api.Test void test_target_handler() throws Exception {
        assert subject.target_handler() == 1;
    }
}
'''
        status, checks = evaluate(source, path="tests/GeneratedTest.java")
        self.assertEqual(status, "verified")
        self.assertEqual(checks["assertion"], "pass")
        self.assertEqual(checks["target"], "pass")

    def test_parameterized_annotation_arguments_do_not_hide_java_test_method(self):
        source = '''class GeneratedTests {
    @ParameterizedTest
    @CsvSource({"first, 1", "second, 2"})
    void targetBehavior(String input, int expected) {
        assertEquals(expected, subject.target_handler(input));
    }
}
'''
        status, checks = evaluate(source, path="tests/GeneratedTests.java")
        self.assertEqual(status, "verified")
        self.assertEqual(checks["assertion"], "pass")
        self.assertEqual(checks["target"], "pass")

    def test_java_method_identity_ignores_intervening_parameterized_annotations(self):
        before = '''class GeneratedTests {
    @ParameterizedTest
    @CsvSource({"one, 1", "two, 2"})
    @Timeout(1)
    void returnsExpected(String input, int expected) {
        assertEquals(expected, subject.target_handler(input));
    }
}
'''
        after = before.replace("@Timeout(1)", "@Timeout(2)").replace(
            "assertEquals(expected, subject.target_handler(input));",
            "assertTrue(subject.target_handler(input) == expected);",
        )
        status, checks = evaluate(
            after,
            path="tests/GeneratedTests.java",
            base_files={"tests/GeneratedTests.java": before},
        )
        self.assertEqual(status, "verified")
        self.assertEqual(checks["assertion"], "pass")
        self.assertEqual(checks["target"], "pass")

    def test_nested_java_tests_with_same_name_are_checked_independently(self):
        before = '''class GeneratedTests {
    @Nested class First {
        @Test void returnsExpected() {
            subject.target_handler();
            assertEquals(1, actual);
        }
    }
    @Nested class Second {
        @Test void returnsExpected() {
            subject.target_handler();
            assertTrue(subject.isValid());
        }
    }
}
'''
        after = '''class GeneratedTests {
    @Nested class First {
        @Test void returnsExpected() {
            subject.target_handler();
        }
    }
    @Nested class Second {
        @Test void returnsExpected() {
            subject.target_handler();
            assertTrue(subject.isValid());
        }
    }
}
'''
        status, checks = evaluate(
            after,
            path="tests/GeneratedTests.java",
            base_files={"tests/GeneratedTests.java": before},
        )
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")

    def test_unknown_language_reports_inconclusive_gate_support(self):
        status, checks = evaluate(
            'test("target_handler", () => expect(target_handler()).toBe(1));',
            path="tests/test_generated.ts",
        )
        self.assertEqual(status, "inconclusive")
        self.assertEqual(checks["assertion"], "not_supported")
        self.assertEqual(checks["target"], "not_supported")

    def test_java_assertions_in_test_method_must_be_nontrivial_and_targeted(self):
        source = '''class GeneratedTest {
    @Test void target_handler() {
        subject.target_handler();
        assertTrue(subject.isValid());
        assertEquals("same", "same");
    }
}
'''
        status, checks = evaluate(source, path="tests/GeneratedTest.java")
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")
        self.assertEqual(checks["target"], "pass")

    def test_existing_assertion_count_cannot_be_weakened(self):
        before = "def test_target_handler():\n    assert first == 1\n    assert second == 2\n"
        after = "def test_target_handler():\n    assert first == 1\n"
        status, checks = evaluate(after, base_files={"tests/test_generated.py": before})
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")

    def test_java_equality_assertion_cannot_be_replaced_by_null_check(self):
        before = '''class GeneratedTest {
    @Test void test_target_handler() {
        assertEquals("expected", subject.target_handler());
    }
}
'''
        after = '''class GeneratedTest {
    @Test void test_target_handler() {
        assertNotNull(subject.target_handler());
    }
}
'''
        status, checks = evaluate(
            after,
            path="tests/GeneratedTest.java",
            base_files={"tests/GeneratedTest.java": before},
        )
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")

    def test_assertj_builder_without_terminal_assertion_does_not_count(self):
        status, checks = evaluate(
            "class GeneratedTest {\n"
            "    @Test void checksTarget() {\n"
            "        assertThat(subject.target_handler());\n"
            "    }\n"
            "}\n",
            path="tests/GeneratedTest.java",
        )
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")
        self.assertEqual(checks["target"], "pass")

    def test_assertj_terminal_semantics_cannot_be_weakened(self):
        before = '''class GeneratedTest {
    @Test void checksTarget() {
        assertThat(subject.target_handler()).isEqualTo(42);
    }
}
'''
        after = before.replace(".isEqualTo(42)", ".isNotNull()")
        status, checks = evaluate(
            after,
            path="tests/GeneratedTest.java",
            base_files={"tests/GeneratedTest.java": before},
        )
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")
        self.assertEqual(checks["target"], "pass")

    def test_python_nested_helper_assertion_and_target_require_invocation(self):
        before = '''def test_generated_behavior():
    def exercise_target():
        assert target_handler() == 42
    exercise_target()
'''
        after = before.replace("    exercise_target()\n", "")
        status, checks = evaluate(after, base_files={"tests/test_generated.py": before})
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")
        self.assertEqual(checks["target"], "fail")

    def test_python_pytest_raises_is_a_meaningful_targeted_assertion(self):
        status, checks = evaluate(
            "import pytest\n"
            "def test_target_handler_raises():\n"
            "    with pytest.raises(ValueError):\n"
            "        target_handler()\n"
        )
        self.assertEqual(status, "verified")
        self.assertEqual(checks["assertion"], "pass")
        self.assertEqual(checks["target"], "pass")

    def test_kotlin_backtick_expression_body_is_inconclusive_not_rejected(self):
        status, checks = evaluate(
            "class GeneratedTest {\n"
            "    @Test\n"
            "    fun `target handler returns expected`() = "
            "assertEquals(1, target_handler())\n"
            "}\n",
            path="tests/GeneratedTest.kt",
        )
        self.assertEqual(status, "inconclusive")
        self.assertEqual(checks["assertion"], "not_supported")
        self.assertEqual(checks["target"], "not_supported")

    def test_new_test_without_an_assertion_is_rejected_even_if_file_has_one(self):
        before = "def test_existing():\n    assert existing_value == 1\n"
        after = before + "\ndef test_target_handler():\n    target_handler()\n"
        status, checks = evaluate(after, base_files={"tests/test_generated.py": before})
        self.assertEqual(status, "rejected")
        self.assertEqual(checks["assertion"], "fail")


if __name__ == "__main__":
    unittest.main()
