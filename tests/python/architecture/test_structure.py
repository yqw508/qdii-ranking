import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_ROOT = ROOT / "scripts"
TEST_ROOT = ROOT / "tests"
FILE_LIMIT = 1000
CLASS_LIMIT = 500
FUNCTION_LIMIT = 200


def maintained_files(suffixes: set[str]) -> list[Path]:
    roots = (SCRIPT_ROOT, TEST_ROOT)
    return sorted(
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in suffixes
        and "__pycache__" not in path.parts
        and "fixtures" not in path.parts
    )


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(SCRIPT_ROOT).with_suffix("").parts)


def resolved_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    package = module_name(path).split(".")[:-1]
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                base = package[: max(0, keep)]
                target = ".".join([*base, *(node.module or "").split(".")])
            else:
                target = node.module or ""
            imports.add(target.rstrip("."))
    return imports


class StructureGuardTests(unittest.TestCase):
    def test_source_and_test_files_stay_within_line_budget(self):
        offenders = []
        for path in maintained_files({".py", ".js", ".mjs"}):
            lines = len(path.read_text(encoding="utf-8-sig").splitlines())
            if lines > FILE_LIMIT:
                offenders.append(f"{path.relative_to(ROOT)}: {lines}")
        self.assertEqual([], offenders)

    def test_python_classes_and_functions_stay_within_line_budget(self):
        class_offenders = []
        function_offenders = []
        for path in maintained_files({".py"}):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ) or not hasattr(node, "end_lineno"):
                    continue
                size = node.end_lineno - node.lineno + 1
                label = f"{path.relative_to(ROOT)}:{node.lineno} {node.name} ({size})"
                if isinstance(node, ast.ClassDef) and size > CLASS_LIMIT:
                    class_offenders.append(label)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and size > FUNCTION_LIMIT:
                    function_offenders.append(label)
        self.assertEqual([], class_offenders)
        self.assertEqual([], function_offenders)

    def test_ranking_dependency_direction(self):
        forbidden_for_sources = (
            "qdii_ranking.cache",
            "qdii_ranking.renderers",
            "qdii_ranking.artifacts",
            "qdii_ranking.pipeline",
            "qdii_ranking.cli",
        )
        forbidden_for_cache = (
            "qdii_ranking.cli",
            "qdii_ranking.pipeline",
            "update_qdii_ranking",
        )
        offenders = []
        for path in (SCRIPT_ROOT / "qdii_ranking" / "sources").glob("*.py"):
            for imported in resolved_imports(path):
                if imported.startswith(forbidden_for_sources):
                    offenders.append(f"{path.name} -> {imported}")
        for path in (SCRIPT_ROOT / "qdii_ranking" / "cache").glob("*.py"):
            for imported in resolved_imports(path):
                if imported.startswith(forbidden_for_cache):
                    offenders.append(f"{path.name} -> {imported}")
        for path in (SCRIPT_ROOT / "qdii_ranking" / "pipeline").glob("*.py"):
            for imported in resolved_imports(path):
                if imported.startswith(("qdii_ranking.sources", "qdii_ranking.cache")):
                    offenders.append(f"{path.name} -> {imported}")
        self.assertEqual([], offenders)

    def test_packages_do_not_import_compatibility_facades(self):
        offenders = []
        package_roots = (
            SCRIPT_ROOT / "qdii_ranking",
            SCRIPT_ROOT / "qdii_validation",
            SCRIPT_ROOT / "index_valuation",
        )
        facades = {"update_qdii_ranking", "update_index_valuation", "validate_qdii_ranking"}
        for package in package_roots:
            for path in package.rglob("*.py"):
                for imported in resolved_imports(path):
                    if imported in facades:
                        offenders.append(f"{path.relative_to(SCRIPT_ROOT)} -> {imported}")
        self.assertEqual([], offenders)

    def test_tests_live_outside_scripts(self):
        self.assertEqual([], sorted(SCRIPT_ROOT.glob("test_*.py")))
        self.assertEqual([], sorted(SCRIPT_ROOT.glob("test_*.mjs")))
