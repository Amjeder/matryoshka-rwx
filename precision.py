"""Pinned-source analysis for the precise Blast Radius policy."""

from __future__ import annotations

import argparse
import ast
import gzip
import io
import json
import tokenize
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def parse_edit(path: str, edits: list, sources: ZipFile) -> tuple[ast.Module, ast.Module]:
    raw = sources.read(path)
    old_lines = raw.splitlines()
    new_lines = []
    offset = 0
    for edit in edits:
        start = (edit.first if edit.removed else edit.last) - 1
        end = start + len(edit.removed)
        removed = tuple(line.encode("utf-8") for line in edit.removed)
        if start < offset or start > len(old_lines) or tuple(old_lines[start:end]) != removed:
            raise ValueError("Patch does not match the pinned source")
        new_lines.extend(old_lines[offset:start])
        new_lines.extend(line.encode("utf-8") for line in edit.added)
        offset = end
    new_lines.extend(old_lines[offset:])
    new = b"\n".join(new_lines) + b"\n"

    old_encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
    new_encoding = tokenize.detect_encoding(io.BytesIO(new).readline)[0]
    old_header = raw.split(b"\n", 1)[0] if raw.startswith(b"#!") else None
    new_header = new.partition(b"\n")[0] if new.startswith(b"#!") else None
    if new_encoding != old_encoding or new_header != old_header:
        raise ValueError("Patch changes the source header")
    compile(new, path, "exec", dont_inherit=True)
    return (
        ast.parse(raw, filename=path, type_comments=True),
        ast.parse(new, filename=path, type_comments=True),
    )


def declarations(tree: ast.Module, predicate) -> dict[tuple, list[ast.stmt]]:
    result = defaultdict(list)
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            for member in statement.body:
                key = predicate(member)
                if key is not None:
                    result[statement.name, key].append(member)
        elif isinstance(statement, FUNCTIONS):
            key = predicate(statement)
            if key is not None:
                result[None, key].append(statement)
    return result


def equal_outside(old, new, changes):
    saved = []
    for left, right, field, marker in changes:
        saved.extend(((left, field, getattr(left, field)), (right, field, getattr(right, field))))
        setattr(left, field, marker)
        setattr(right, field, marker)
    try:
        return ast.dump(old) == ast.dump(new)
    finally:
        for node, field, value in saved:
            setattr(node, field, value)


def body_edit_kind(old: ast.Module, new: ast.Module, edits: list) -> str | None:
    """Identify modifications confined to bodies of existing callables."""
    def name(node):
        return node.name if isinstance(node, FUNCTIONS) and not node.name.startswith("__") else None

    prior = declarations(old, name)
    after = declarations(new, name)
    touched = set()
    for edit in edits:
        matches = [
            key for key, entries in prior.items() if len(entries) == 1
            and entries[0].body[0].lineno <= edit.first <= edit.last <= entries[0].end_lineno
        ]
        if len(matches) != 1 or len(after.get(matches[0], ())) != 1:
            return None
        touched.add(matches[0])
    if touched and equal_outside(
        old, new, [(prior[key][0], after[key][0], "body", [ast.Pass()]) for key in touched]
    ):
        return "method" if all(cls is not None for cls, _ in touched) else "function"
    return None


def same_literal_shape(old, new) -> bool:
    if type(old) is not type(new):
        return False
    if isinstance(old, dict):
        return list(old) == list(new) and all(same_literal_shape(old[key], new[key]) for key in old)
    if isinstance(old, (list, tuple)):
        return len(old) == len(new) and all(same_literal_shape(a, b) for a, b in zip(old, new))
    if type(old) is int:
        return -2**31 <= new < 2**31
    return isinstance(old, (bool, float, str, bytes)) or old is None


def literal_field_classes(old: ast.Module, new: ast.Module) -> set[str]:
    """Recognize class-field value changes without executable initializers."""
    def field(node):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            return None
        target = node.targets[0]
        return target.id if isinstance(target, ast.Name) and not target.id.startswith("__") else None

    prior = declarations(old, field)
    after = declarations(new, field)
    changed = []
    classes = set()
    for key in prior.keys() & after.keys():
        if len(prior[key]) != 1 or len(after[key]) != 1:
            continue
        left, right = prior[key][0], after[key][0]
        if ast.dump(left.value) == ast.dump(right.value):
            continue
        try:
            before, after_value = ast.literal_eval(left.value), ast.literal_eval(right.value)
        except (ValueError, TypeError):
            continue
        if same_literal_shape(before, after_value):
            changed.append((left, right, "value", ast.Constant(None)))
            classes.add(key[0])
    return classes if changed and equal_outside(old, new, changed) else set()


def symbol_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def build_class_consumers(sources: ZipFile, evidence: dict) -> dict[str, list[int]]:
    """Join pinned Python class references and subclass uses to baseline test coverage."""
    parsed = {
        path: ast.parse(sources.read(path), type_comments=True)
        for path in sources.namelist() if path.endswith(".py")
    }
    classes = defaultdict(set)
    for tree in parsed.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes[node.name].update(symbol_name(parent) for parent in node.bases)
    subclasses = defaultdict(set)
    for child, parents in classes.items():
        for parent in parents & classes.keys():
            subclasses[parent].add(child)

    tests = defaultdict(set)
    for index, test_id in enumerate(evidence["test_ids"]):
        tests[test_id.split("::", 1)[0]].add(index)

    consumers = defaultdict(set)
    for path, tree in parsed.items():
        lines = evidence["files"].get(path, {}).get("lines", {})
        test_module = tests.get(path, ())
        for node in ast.walk(tree):
            name = symbol_name(node)
            if name not in classes:
                continue
            consumers[name].update(test_module)
            for line in range(node.lineno, node.end_lineno + 1):
                consumers[name].update(lines.get(str(line), ()))

    expanded = {}
    for name in classes:
        descendants = {name}
        pending = [name]
        while pending:
            added = subclasses[pending.pop()] - descendants
            descendants.update(added)
            pending.extend(added)
        indices = set().union(*(consumers[child] for child in descendants))
        if indices:
            expanded[name] = sorted(indices)
    return expanded


def map_observed_class_users(evidence: dict, observation: dict) -> dict[str, list[int]]:
    baseline = {test_id: index for index, test_id in enumerate(evidence["test_ids"])}
    if observation["revision"] != evidence["revision"] or set(observation["test_ids"]) != baseline.keys():
        raise ValueError("Class observations must cover the same pinned test inventory")
    remap = [baseline[test_id] for test_id in observation["test_ids"]]
    return {
        name: sorted({remap[index] for index in indices})
        for name, indices in observation["class_users"].items()
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add class consumers to the pinned coverage map")
    parser.add_argument("coverage", type=Path)
    parser.add_argument("sources", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--class-usage", type=Path)
    args = parser.parse_args()
    evidence = json.loads(gzip.decompress(args.coverage.read_bytes()))
    with ZipFile(args.sources) as sources:
        evidence["class_consumers"] = build_class_consumers(sources, evidence)
    if args.class_usage:
        observations = json.loads(gzip.decompress(args.class_usage.read_bytes()))
        evidence["observed_class_users"] = map_observed_class_users(evidence, observations)
    args.output.write_bytes(gzip.compress(json.dumps(evidence, separators=(",", ":")).encode(), mtime=0))
