"""Build selector evidence from the pinned SQLGlot checkout and pytest execution."""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import coverage
import pytest

PINNED_REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
FUNCTIONS = (ast.AsyncFunctionDef, ast.FunctionDef)
DECLARATIONS = (*FUNCTIONS, ast.ClassDef)
DIALECT_DIRECTIVE = re.compile(
    r"^\s*(?:\#|--|//)\s*(?:dialect|read|write)\s*:\s*([a-z][\w]*)", re.I | re.M
)


class Capture:
    def __init__(self, root: Path, tracer: coverage.Coverage) -> None:
        self.root = root
        self.tracer = tracer
        self.context = ""
        self.fixture_contexts: dict[str, set[str]] = defaultdict(set)
        self.fixture_root = os.fspath(root / "tests") + os.sep
        self.test_ids: list[str] = []
        sys.addaudithook(self.capture_open)

    def capture_open(self, event: str, args: tuple) -> None:
        if event != "open" or not self.context or not isinstance(args[0], (str, bytes)):
            return
        path = os.fsdecode(args[0])
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        if path.startswith(self.fixture_root) and not path.endswith((".py", ".pyc")):
            self.fixture_contexts[os.path.relpath(path, self.root)].add(self.context)

    def switch(self, context: str) -> str:
        previous = self.context
        self.context = context
        self.tracer.switch_context(context)
        return previous

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_make_collect_report(self, collector):
        if isinstance(collector, pytest.Module):
            previous = self.switch(f"module:{collector.nodeid}")
            try:
                yield
            finally:
                self.switch(previous)
        else:
            yield

    def pytest_collection_finish(self, session) -> None:
        self.test_ids = sorted(item.nodeid for item in session.items)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        previous = self.switch(item.nodeid)
        try:
            yield
        finally:
            self.switch(previous)


def node_span(node: ast.AST) -> tuple[int, int, int]:
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    body = node.body[0].lineno
    return start, body, node.end_lineno


def module_name(relative_path: str) -> str:
    return relative_path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def import_candidates(tree: ast.Module, path: str) -> set[str]:
    package = module_name(path).split(".")
    if not path.endswith("/__init__.py"):
        package.pop()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parents = package[: len(package) - node.level + 1]
                parent = ".".join([*parents, *([node.module] if node.module else [])])
            else:
                parent = node.module or ""
            names.add(parent)
            names.update(f"{parent}.{alias.name}" for alias in node.names)
    return names


def build_source_archive(root: Path, output: Path) -> None:
    """Capture only pinned Python to detect broken imports before narrowing."""
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(
            [*root.glob("sqlglot/**/*.py"), *root.glob("tests/**/*.py"), root / "LICENSE"]
        ):
            entry = ZipInfo(path.relative_to(root).as_posix())
            archive.writestr(entry, path.read_bytes(), ZIP_DEFLATED, 6)


def add_dialect_evidence(root: Path, evidence: dict) -> None:
    dialect_names = {path.stem for path in (root / "sqlglot/dialects").glob("*.py")}
    readers: dict[str, set[int]] = defaultdict(set)
    for path, tests in evidence["fixtures"].items():
        if not path.endswith((".sql", ".txt")):
            continue
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        for dialect in {match.lower() for match in DIALECT_DIRECTIVE.findall(text)} & dialect_names:
            readers[dialect].update(tests)
    families = {"dialects", "generators", "parsers", "typing"}
    evidence["dialect_consumers"] = {
        path: sorted(readers[Path(path).stem])
        for path in evidence["files"]
        if Path(path).parent.name in families
        and Path(path).stem in dialect_names - {"__init__", "dialect"}
    }


def build_index(root: Path, capture: Capture) -> dict:
    source = capture.tracer.get_data()
    by_id = {test_id: index for index, test_id in enumerate(capture.test_ids)}
    by_module: dict[str, set[int]] = defaultdict(set)
    for test_id, index in by_id.items():
        by_module[test_id.split("::", 1)[0]].add(index)

    def indices(contexts) -> set[int]:
        result: set[int] = set()
        for context in contexts:
            if context.startswith("module:"):
                result.update(by_module.get(context.removeprefix("module:"), ()))
            elif context in by_id:
                result.add(by_id[context])
        return result

    paths = sorted([*root.glob("sqlglot/**/*.py"), *root.glob("tests/**/*.py")])
    trees = {path.relative_to(root).as_posix(): ast.parse(path.read_bytes()) for path in paths}
    paths_by_module = {module_name(path): path for path in trees}
    reverse: dict[str, set[str]] = defaultdict(set)
    for path, tree in trees.items():
        for imported in import_candidates(tree, path):
            if imported in paths_by_module and paths_by_module[imported] != path:
                reverse[paths_by_module[imported]].add(path)

    def importers(path: str) -> set[int]:
        pending = [path]
        reached = {path}
        tests: set[int] = set()
        while pending:
            current = pending.pop()
            tests.update(by_module.get(current, ()))
            for user in reverse[current] - reached:
                reached.add(user)
                pending.append(user)
        return tests

    files = {}
    for path in sorted(trees):
        if not path.startswith("sqlglot/"):
            continue
        contexts = source.contexts_by_lineno(str(root / path))
        # Keep collection separate so lazy imports during collection don't pollute
        # coverage precision for a changed function body.
        runtime = {
            line: indices(c for c in names if not c.startswith("module:"))
            for line, names in contexts.items()
        }
        runtime = {line: covered for line, covered in runtime.items() if covered}
        collected = set().union(*(indices(names) for names in contexts.values()))
        direct_importers = importers(path)

        spans = []
        for node in ast.walk(trees[path]):
            if not isinstance(node, DECLARATIONS):
                continue
            start, first_body, end = node_span(node)
            covered = set().union(
                *(tests for line, tests in runtime.items() if first_body <= line <= end)
            )
            spans.append([start, first_body, end, isinstance(node, FUNCTIONS), sorted(covered)])
        spans.sort(key=lambda item: (item[2] - item[0], item[0]))
        files[path] = {
            "module": sorted(collected | direct_importers),
            "scopes": spans,
            "lines": {line: sorted(tests) for line, tests in sorted(runtime.items())},
        }

    fixtures = {
        path: sorted(indices(contexts))
        for path, contexts in sorted(capture.fixture_contexts.items())
    }
    evidence = {
        "revision": PINNED_REVISION,
        "test_ids": capture.test_ids,
        "files": files,
        "fixtures": fixtures,
        "test_modules": {
            path: sorted(importers(path)) for path in sorted(trees) if path.startswith("tests/")
        },
    }
    add_dialect_evidence(root, evidence)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sources", type=Path)
    args = parser.parse_args()
    root, output = args.checkout.resolve(), args.output.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    tracer = coverage.Coverage(source=[str(root / "sqlglot")], config_file=False, data_file=None)
    capture = Capture(root, tracer)
    tracer.start()
    try:
        result = pytest.main(["-q", "-p", "no:cacheprovider", "tests"], plugins=[capture])
    finally:
        tracer.stop()
    if result != pytest.ExitCode.OK:
        raise SystemExit(f"Could not build coverage because the pinned tests exited {int(result)}")
    evidence = build_index(root, capture)
    output.write_bytes(gzip.compress(json.dumps(evidence, separators=(",", ":")).encode(), mtime=0))
    if args.sources:
        build_source_archive(root, args.sources.resolve())
    print(f"Captured {len(capture.test_ids)} tests, {len(evidence['files'])} source files, "
          f"and {len(evidence['fixtures'])} fixtures in {output.stat().st_size:,} bytes.")


if __name__ == "__main__":
    main()
