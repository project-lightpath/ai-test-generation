"""Deterministic quality gates for generated native tests."""

from __future__ import annotations

import ast
import re
from typing import Mapping

from atg.ports.generation import PatchCandidate, ValidationResult


_ASSERT_CALL = re.compile(r"\bassert\w*\s*\(", re.IGNORECASE)
_JAVA_TEST_ANNOTATION = re.compile(
    r"@(?:[A-Za-z_$][\w$]*\.)*(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b"
)
_JAVA_ANNOTATION = re.compile(r"@(?:[A-Za-z_$][\w$]*\.)*[A-Za-z_$][\w$]*")
_JAVA_TEST_METHOD = re.compile(
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<parameters>[^{}]*)\)\s*(?:(?:throws\s+"
    r"[A-Za-z_$][\w$.]*(?:\s*,\s*[A-Za-z_$][\w$.]*)*)|"
    r"(?::\s*[A-Za-z_$][\w$.<>?,]*))?\s*$"
)
_JAVA_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_JAVA_TYPE_DECLARATION = re.compile(
    r"\b(?:class|interface|enum|record|object)\s+([A-Za-z_$][\w$]*)\b[^{;]*\{"
)
_JAVA_ASSERT_STATEMENT = re.compile(r"\bassert\s+([^;]+);")
_METHOD_SUMMARY = tuple[int, int, int, set[str]]
_TRIVIAL_JAVA_ASSERTION = re.compile(
    r"\bassert(?:True|False)\s*\(\s*(?:true|false)\s*(?:,\s*(?:LITERAL|[A-Za-z_$][\w$]*|[-+]?\d+(?:\.\d+)?))?\s*\)"
    r"|\bassert(?:NotNull|Null)\s*\(\s*"
    r"(?:LITERAL|true|false|null|[-+]?\d+(?:\.\d+)?[fFdDlL]?)\s*"
    r"(?:,\s*(?:LITERAL|[A-Za-z_$][\w$]*|[-+]?\d+(?:\.\d+)?))?\s*\)"
    r"|\bassert(?:Equals|NotEquals)\s*\(\s*"
    r"(LITERAL|true|false|null|[-+]?\d+(?:\.\d+)?[fFdDlL]?|"
    r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*,\s*\1"
    r"(?:,\s*(?:LITERAL|[A-Za-z_$][\w$]*|[-+]?\d+(?:\.\d+)?))?\s*\)"
    r"|\bassertThat\s*\(\s*(true|false|LITERAL)\s*,\s*"
    r"(?:is|equalTo)\s*\(\s*\2\s*\)\s*\)",
    re.IGNORECASE,
)


def _check(name: str, passed: bool, detail: str | None = None) -> dict[str, str]:
    document = {"name": name, "status": "pass" if passed else "fail"}
    if detail:
        document["detail"] = detail
    return document


def _validation_check(
    name: str, result: bool | None, unsupported: set[str]
) -> dict[str, str]:
    if name in unsupported or result is None:
        return {
            "name": name,
            "status": "not_supported",
            "detail": f"{name} validation is not configured",
        }
    return _check(name, result, f"{name} validation failed" if not result else None)


def _mask_non_code(source: str) -> str:
    """Remove comments and literals while preserving code tokens and newlines."""
    result: list[str] = []
    index = 0
    while index < len(source):
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = len(source) if end < 0 else end
            result.append("\n" if end < len(source) else "")
            index = end + (end < len(source))
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end < 0 else end + 2
            result.append("".join("\n" if char == "\n" else " " for char in source[index:end]))
            index = end
            continue

        quote = source[index]
        if quote in "\"'":
            delimiter = quote * 3 if source.startswith(quote * 3, index) else quote
            end = index + len(delimiter)
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                    continue
                if source.startswith(delimiter, end):
                    end += len(delimiter)
                    break
                end += 1
            literal = source[index:end]
            result.append("LITERAL")
            result.extend("\n" for char in literal if char == "\n")
            index = end
            continue

        result.append(quote)
        index += 1
    return "".join(result)


def _python_trivial_assertion(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        test = node.test
        if isinstance(test, ast.Constant) and isinstance(test.value, bool):
            return True
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], (ast.Eq, ast.Is))
            and ast.dump(test.left, include_attributes=False)
            == ast.dump(test.comparators[0], include_attributes=False)
        ):
            return True
        return False
    if not isinstance(node, ast.Call):
        return False
    name = _python_call_name(node.func).lower()
    if name.endswith(("assertnull", "assertnotnull", "assertisnone", "assertisnotnone")):
        return bool(node.args) and isinstance(node.args[0], ast.Constant)
    if name.endswith(("asserttrue", "assertfalse")):
        return bool(node.args) and isinstance(node.args[0], ast.Constant)
    if name.endswith(("assertequal", "assertequals", "assertnotequal", "assertnotequals")):
        return (
            len(node.args) >= 2
            and ast.dump(node.args[0], include_attributes=False)
            == ast.dump(node.args[1], include_attributes=False)
        )
    if name.endswith("assertthat") and len(node.args) >= 2:
        actual, matcher = node.args[:2]
        if isinstance(actual, ast.Constant) and isinstance(matcher, ast.Call) and matcher.args:
            return ast.dump(actual, include_attributes=False) == ast.dump(
                matcher.args[0], include_attributes=False
            )
    return False


def _python_strong_assertion(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        if isinstance(node.test, ast.Compare):
            if (
                len(node.test.ops) == 1
                and isinstance(node.test.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq))
                and any(
                    isinstance(value, ast.Constant) and value.value is None
                    for value in (node.test.left, *node.test.comparators)
                )
            ):
                return False
            return True
        return isinstance(node.test, ast.Call)
    if _python_exception_assertion(node):
        return True
    if not isinstance(node, ast.Call):
        return False
    name = _python_call_name(node.func).lower()
    if name.endswith((
        "assertequal", "assertequals", "assertnotequal", "assertnotequals",
        "assertarrayequal", "assertthrows", "assertin", "assertnotin",
        "assertgreater", "assertgreaterequal", "assertless", "assertlessequal",
        "assertregex", "assertmatch",
    )):
        return True
    if name.endswith(("asserttrue", "assertfalse")) and node.args:
        if isinstance(node.args[0], ast.Compare):
            return _python_strong_assertion(ast.Assert(test=node.args[0]))
        return isinstance(node.args[0], ast.Call)
    if name.endswith("assertthat") and len(node.args) > 1:
        matcher = node.args[1]
        return isinstance(matcher, ast.Call) and _python_call_name(matcher.func).lower() in {
            "equalto", "sameinstance", "greaterthan", "lessthan", "contains",
        }
    return False


def _python_call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _python_exception_assertion(node: ast.AST) -> bool:
    if not isinstance(node, (ast.With, ast.AsyncWith)):
        return False
    for item in node.items:
        context = item.context_expr
        if (
            isinstance(context, ast.Call)
            and _python_call_name(context.func).lower() == "raises"
            and context.args
        ):
            return True
    return False


def _python_executable_nodes(root: ast.AST):
    pending = [root]
    visited: set[int] = set()
    while pending:
        scope = pending.pop()
        if id(scope) in visited:
            continue
        visited.add(id(scope))

        helpers: dict[str, ast.AST] = {}
        declarations = list(getattr(scope, "body", ()))
        while declarations:
            declaration = declarations.pop()
            if isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef)):
                helpers[declaration.name] = declaration
            elif not isinstance(declaration, ast.ClassDef):
                declarations.extend(ast.iter_child_nodes(declaration))

        nodes = [scope]
        while nodes:
            node = nodes.pop()
            yield node
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                helper = helpers.get(node.func.id)
                if helper is not None:
                    pending.append(helper)
            for child in reversed(list(ast.iter_child_nodes(node))):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                nodes.append(child)


def _python_test_methods(source: str) -> dict[str, _METHOD_SUMMARY]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    methods: dict[str, tuple[int, int, set[str]]] = {}

    def visit(statements: list[ast.stmt], prefix: tuple[str, ...] = ()) -> None:
        for statement in statements:
            if isinstance(statement, ast.ClassDef):
                visit(statement.body, (*prefix, statement.name))
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if statement.name.startswith("test"):
                    assertions = [
                        node for node in _python_executable_nodes(statement)
                        if isinstance(node, (ast.Assert, ast.With, ast.AsyncWith))
                        and (
                            isinstance(node, ast.Assert)
                            or _python_exception_assertion(node)
                        )
                        or (
                            isinstance(node, ast.Call)
                            and _python_call_name(node.func).lower().startswith("assert")
                        )
                    ]
                    meaningful = sum(not _python_trivial_assertion(node) for node in assertions)
                    strong = sum(_python_strong_assertion(node) for node in assertions)
                    symbols: set[str] = set()
                    for node in _python_executable_nodes(statement):
                        if isinstance(node, ast.Name):
                            symbols.add(node.id)
                        elif isinstance(node, ast.Attribute):
                            symbols.add(node.attr)
                        elif isinstance(node, ast.arg):
                            symbols.add(node.arg)
                    key = ".".join((*prefix, statement.name))
                    methods[key] = (meaningful, len(assertions) - meaningful, strong, symbols)

    visit(tree.body)
    return methods


def _java_call_arguments(source: str, match: re.Match[str]) -> str:
    opening = match.end() - 1
    depth = 1
    end = opening + 1
    while end < len(source) and depth:
        depth += (source[end] == "(") - (source[end] == ")")
        end += 1
    return source[opening + 1:end - 1] if depth == 0 else source[opening + 1:]


def _java_call_end(source: str, opening: int) -> int:
    depth = 1
    end = opening + 1
    while end < len(source) and depth:
        depth += (source[end] == "(") - (source[end] == ")")
        end += 1
    return end if depth == 0 else -1


def _java_assertj_terminal(source: str, match: re.Match[str]) -> str | None:
    end = _java_call_end(source, match.end() - 1)
    if end < 0:
        return None
    terminal = None
    while end < len(source):
        suffix = re.match(r"\s*\.\s*([A-Za-z_$][\w$]*)\s*\(", source[end:])
        if suffix is None:
            break
        method = suffix.group(1)
        opening = end + suffix.end() - 1
        end = _java_call_end(source, opening)
        if end < 0:
            break
        lowered = method.lower()
        if (
            lowered.startswith(("is", "has", "contains", "doesnot"))
            or lowered in {"startsWith".lower(), "endsWith".lower(), "matches", "satisfies"}
        ):
            terminal = method
    return terminal


def _java_strong_assertion(source: str, match: re.Match[str]) -> bool:
    name = re.match(r"assert\w+", match.group(), re.IGNORECASE)
    if name is None:
        return False
    method = name.group().lower()
    if method.endswith((
        "assertequals", "assertnotequals", "assertarrayequals", "assertthrows",
        "assertdoesnotthrow", "assertin", "assertnotin",
    )):
        return True
    arguments = _java_call_arguments(source, match)
    if method.endswith("assertthat"):
        terminal = _java_assertj_terminal(source, match)
        if terminal is None:
            return False
        return terminal.lower().startswith((
            "isequalto", "isnotequalto", "isgreaterthan", "islessthan", "contains",
            "startswith", "endswith", "matches", "satisfies",
        ))
    if method.endswith(("asserttrue", "assertfalse")):
        if re.search(r"(?:==|!=|\bis\s+)(?:\s*null\b)|(?:\bnull\b)\s*(?:==|!=|\bis\b)", arguments):
            return False
        return bool(re.search(
            r"(?:==|!=|>=|<=|(?<![=!<>])[<>](?![=>])|\.equals\s*\(|\b(?:is|has|contains|can)[A-Z]\w*\s*\()",
            arguments,
        ))
    return False


def _find_matching_brace(source: str, opening: int) -> int:
    depth = 1
    end = opening + 1
    while end < len(source) and depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return end if depth == 0 else -1


def _find_test_body_opening(source: str, start: int) -> int:
    parentheses = brackets = nested_braces = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == "(":
            parentheses += 1
        elif char == ")":
            parentheses = max(0, parentheses - 1)
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets = max(0, brackets - 1)
        elif char == "{":
            if parentheses or brackets or nested_braces:
                nested_braces += 1
            else:
                return index
        elif char == "}" and nested_braces:
            nested_braces -= 1
    return -1


def _without_java_annotations(source: str) -> str:
    result: list[str] = []
    cursor = 0
    for annotation in _JAVA_ANNOTATION.finditer(source):
        if annotation.start() < cursor:
            continue
        result.append(source[cursor:annotation.start()])
        end = annotation.end()
        opening = end
        while opening < len(source) and source[opening].isspace():
            opening += 1
        if opening < len(source) and source[opening] == "(":
            annotation_end = _java_call_end(source, opening)
            if annotation_end >= 0:
                end = annotation_end
        result.append(" " * (end - annotation.start()))
        cursor = end
    result.append(source[cursor:])
    return "".join(result)


def _java_test_methods(source: str) -> dict[str, _METHOD_SUMMARY]:
    code = _mask_non_code(source)
    type_scopes = []
    for declaration in _JAVA_TYPE_DECLARATION.finditer(code):
        opening = declaration.end() - 1
        end = _find_matching_brace(code, opening)
        if end >= 0:
            type_scopes.append((opening, end, declaration.group(1)))

    ranges: list[tuple[str, int, int, int]] = []
    for annotation in _JAVA_TEST_ANNOTATION.finditer(code):
        opening = _find_test_body_opening(code, annotation.end())
        if opening < 0:
            continue
        signature = _without_java_annotations(code[annotation.end():opening])
        method = _JAVA_TEST_METHOD.search(signature)
        if method is None:
            continue
        end = _find_matching_brace(code, opening)
        if end < 0:
            continue
        enclosing_types = [
            (scope_start, name)
            for scope_start, scope_end, name in type_scopes
            if scope_start < annotation.start() < scope_end
        ]
        type_name = ".".join(name for _, name in sorted(enclosing_types))
        parameters = " ".join(method.group("parameters").split())
        identity = f"{type_name}#{method.group('name')}({parameters})"
        ranges.append((identity, annotation.start(), end, opening + 1))

    methods: dict[str, _METHOD_SUMMARY] = {}
    for name, start, end, body_start in ranges:
        region = code[start:end]
        body = code[body_start:end - 1]
        assertions = [
            match for match in _ASSERT_CALL.finditer(region)
            if not match.group().lower().startswith("assertthat")
            or _java_assertj_terminal(region, match) is not None
        ]
        java_assertions = list(_JAVA_ASSERT_STATEMENT.finditer(region))
        trivial = sum(
            1
            for match in _TRIVIAL_JAVA_ASSERTION.finditer(region)
        ) + sum(_java_assertion_statement_is_trivial(match.group(1)) for match in java_assertions)
        strong = sum(_java_strong_assertion(region, match) for match in assertions)
        strong += sum(_java_assertion_statement_is_strong(match.group(1)) for match in java_assertions)
        methods[name] = (
            len(assertions) + len(java_assertions) - min(trivial, len(assertions) + len(java_assertions)),
            min(trivial, len(assertions) + len(java_assertions)),
            strong,
            set(_JAVA_IDENTIFIER.findall(body)) - {"LITERAL"},
        )
    return methods


def _java_assertion_statement_is_trivial(expression: str) -> bool:
    expression = expression.split(":", 1)[0].strip()
    if expression.lower() in {"true", "false"}:
        return True
    match = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*(?:==|!=)\s*\1", expression)
    return match is not None


def _java_assertion_statement_is_strong(expression: str) -> bool:
    expression = expression.split(":", 1)[0]
    if re.search(r"(?:==|!=)\s*null\b|\bnull\s*(?:==|!=)", expression):
        return False
    return bool(re.search(
        r"(?:==|!=|>=|<=|(?<![=!<>])[<>](?![=>])|\.equals\s*\(|\b(?:is|has|contains|can)[A-Z]\w*\s*\()",
        expression,
    ))


def _test_methods(path: str, source: str) -> dict[str, _METHOD_SUMMARY]:
    if path.endswith(".py"):
        return _python_test_methods(source)
    if path.endswith(".java"):
        return _java_test_methods(source)
    return {}


def _target_symbol(reference: str) -> str:
    tail = re.split(r"::|[/#:]", reference)[-1]
    tail = re.sub(r"\.(?:java|py|kt|ts|js|go)$", "", tail, flags=re.IGNORECASE)
    tail = tail.rsplit(".", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_$]+", "_", tail).strip("_")


def _assertion_gate(candidate: PatchCandidate) -> bool:
    current: dict[str, _METHOD_SUMMARY] = {}
    previous: dict[str, _METHOD_SUMMARY] = {}
    for path, content in candidate.materialized_files.items():
        current.update({f"{path}:{name}": result for name, result in _test_methods(path, content).items()})
    for path, content in candidate.base_files.items():
        previous.update({f"{path}:{name}": result for name, result in _test_methods(path, content).items()})

    if not current or not any(assertions for assertions, _, _, _ in current.values()):
        return False
    for name, (assertions, trivial, strong, _) in current.items():
        old_assertions, old_trivial, old_strong, _ = previous.get(name, (0, 0, 0, set()))
        if assertions < old_assertions or trivial > old_trivial or strong < old_strong:
            return False
        if name not in previous and assertions == 0:
            return False
    for name, (old_assertions, _, _, _) in previous.items():
        if name not in current and old_assertions:
            return False
    return True


def evaluate_generation(
    candidate: PatchCandidate,
    validation: ValidationResult,
    target_refs: tuple[str, ...],
    path_error: str | None = None,
) -> tuple[str, tuple[str, ...], tuple[Mapping[str, str], ...]]:
    """Evaluate one untrusted candidate and return attributable gate checks."""
    methods = {
        f"{path}:{name}": result
        for path, content in candidate.materialized_files.items()
        for name, result in _test_methods(path, content).items()
    }
    symbols = set().union(*(result[3] for result in methods.values())) if methods else set()
    target_symbols = tuple(filter(None, (_target_symbol(value) for value in target_refs)))
    target_ok = bool(target_symbols) and any(symbol in symbols for symbol in target_symbols)
    unsupported_source = any(
        not path.endswith((".py", ".java"))
        for path in candidate.materialized_files
    )
    unsupported = set(validation.unsupported)
    assertion_ok = _assertion_gate(candidate)
    checks = [
        _check("approved_path", path_error is None, path_error),
        _validation_check("syntax", validation.syntax_ok, unsupported),
        _validation_check("format", validation.format_ok, unsupported),
        _validation_check("compile", validation.compile_ok, unsupported),
        (
            {"name": "collection", "status": "not_supported", "detail": "collection validation is not configured"}
            if "collection" in unsupported or validation.collected is None
            else _check("collection", validation.collected > 0, "no tests collected" if validation.collected < 1 else None)
        ),
        (
            {"name": "assertion", "status": "not_supported", "detail": "source-aware assertion validation is not configured for this language"}
            if unsupported_source
            else _check("assertion", assertion_ok, "missing or weakened assertion" if not assertion_ok else None)
        ),
        (
            {"name": "target", "status": "not_supported", "detail": "source-aware target validation is unavailable"}
            if not target_refs or unsupported_source
            else _check("target", target_ok, "wrong target" if not target_ok else None)
        ),
    ]
    findings = tuple(
        check.get("detail", f"{check['name']} check failed")
        for check in checks
        if check["status"] != "pass"
    )
    status = (
        "rejected" if any(check["status"] == "fail" for check in checks)
        else "inconclusive" if any(check["status"] == "not_supported" for check in checks)
        else "verified"
    )
    return status, findings, tuple(checks)
