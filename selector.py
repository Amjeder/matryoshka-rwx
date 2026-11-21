"""Select and schedule Blast Radius tests using evidence from the pinned source."""

from __future__ import annotations

import argparse
import ast
import gzip
import heapq
import json
import math
import re
import sys
from collections import namedtuple
from pathlib import Path
from zipfile import BadZipFile, ZipFile

MAX_WORKERS = 8
WORKER_COST_MS = 2_000
PINNED_REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
PATH = re.compile(r"^[-+]{3} [ab]/([^\t]+)")
Edit = namedtuple("Edit", ["first", "last", "removed", "added"])


def finish_run(edits: list[Edit], first: int, cursor: int, removed: list, added: list) -> None:
    if not removed and not added:
        return
    if removed:
        edits.append(Edit(first, cursor - 1, tuple(removed), tuple(added)))
    else:
        # Changes inserted between old lines belong to a function only when both
        # sides are within its body. Extra context is safer at scope boundaries.
        edits.append(Edit(max(1, first - 1), first, (), tuple(added)))


def parse_patch(text: str) -> dict[str, list[Edit]] | None:
    changes: dict[str, list[Edit]] = {}
    old_path = path = None
    cursor = old_count = new_count = 0
    first = 0
    removed: list[str] = []
    added: list[str] = []
    in_hunk = False
    for line in text.splitlines():
        match = HUNK.match(line)
        if line.startswith("diff --git ") or match:
            if old_count or new_count:
                return None
            if in_hunk and path:
                finish_run(changes[path], first, cursor, removed, added)
            removed, added = [], []
            if not match:
                in_hunk = False
                old_path = path = None
                continue
            if not path:
                return None
            old_count = 1 if match[2] is None else int(match[2])
            new_count = 1 if match[4] is None else int(match[4])
            cursor = int(match[1]) + (old_count == 0)
            first = cursor
            in_hunk = True
        elif in_hunk:
            if line.startswith("\\ No newline at end of file"):
                continue
            leader = line[:1]
            if leader == " ":
                finish_run(changes[path], first, cursor, removed, added)
                removed, added = [], []
                old_count -= 1
                new_count -= 1
                cursor += 1
                first = cursor
            elif leader == "-":
                removed.append(line[1:])
                old_count -= 1
                cursor += 1
            elif leader == "+":
                added.append(line[1:])
                new_count -= 1
            else:
                return None
            if old_count < 0 or new_count < 0:
                return None
        elif line.startswith("--- "):
            match = PATH.match(line)
            old_path = match[1] if match else None
        elif line.startswith("+++ "):
            match = PATH.match(line)
            if not match or old_path != match[1] or old_path in changes:
                return None
            path = old_path
            changes[path] = []
        elif line.startswith(("new file ", "deleted file ", "rename ", "copy ", "old mode ", "new mode ", "Binary files ")):
            return None
    if old_count or new_count:
        return None
    if in_hunk and path:
        finish_run(changes[path], first, cursor, removed, added)
    return changes if changes and all(changes.values()) else None


def scope_for(file_evidence: dict, edit: Edit) -> list | None:
    # The generated index sorts by width, so the first containing scope has
    # precedence. A class body or function signature needs import-level care.
    for start, body, end, function, tests in file_evidence["scopes"]:
        if start <= edit.first <= edit.last <= end:
            return tests if function and body <= edit.first else None
    return None


def tests_in_affected_suites(file: dict, edits: list[Edit], test_ids: list[str]) -> set[int]:
    scopes = [scope_for(file, edit) for edit in edits]
    if not all(scopes):
        return set()
    readers = {index for scope in scopes for index in scope}
    suites = {test_ids[index].split("::", 1)[0] for index in readers}
    return readers | {
        index for index in file["module"] if test_ids[index].split("::", 1)[0] in suites
    }


def patch_compiles(path: str, edits: list[Edit], sources: ZipFile) -> bool:
    original = sources.read(path).decode("utf-8").splitlines()
    patched = []
    offset = 0
    for edit in edits:
        start = (edit.first if edit.removed else edit.last) - 1
        end = start + len(edit.removed)
        if start < offset or start > len(original) or tuple(original[start:end]) != edit.removed:
            return False
        patched.extend(original[offset:start])
        patched.extend(edit.added)
        offset = end
    patched.extend(original[offset:])
    compile("\n".join(patched) + "\n", path, "exec", dont_inherit=True)
    return True


def select_indices(
    changes: dict[str, list[Edit]], evidence: dict, strategy: str, sources: ZipFile | None = None
) -> set[int] | None:
    selected: set[int] = set()
    dialect_consumers = evidence.get("dialect_consumers", {})
    for path, edits in changes.items():
        if sources is not None and path.endswith(".py"):
            try:
                if not patch_compiles(path, edits, sources):
                    return None
            except (BadZipFile, KeyError, SyntaxError, UnicodeError, ValueError):
                # A syntax error can break importing test modules globally,
                # even if the modified function rarely executes.
                return None
        if path in evidence["fixtures"]:
            selected.update(evidence["fixtures"][path])
            continue
        if path in evidence.get("test_modules", {}):
            selected.update(evidence["test_modules"][path])
            continue
        file = evidence["files"].get(path)
        if file is None:
            return None
        if strategy in ("hybrid", "scope-suites", "dialect-guard"):
            if path not in dialect_consumers:
                if not file["module"]:
                    return None
                narrowed = (
                    tests_in_affected_suites(file, edits, evidence["test_ids"])
                    if strategy == "scope-suites"
                    else set()
                )
                selected.update(narrowed or file["module"])
                continue
            selected.update(dialect_consumers[path])
            if strategy == "dialect-guard":
                # These consumers include import-time and cross-module use
                # that a function's observed execution alone can miss.
                selected.update(file["module"])
        for edit in edits:
            scope = scope_for(file, edit)
            if not scope:
                if not file["module"]:
                    return None
                selected.update(file["module"])
                continue
            if strategy == "line" and edit.removed:
                # Experimental precision mode; default uses whole scope for
                # newly reachable branches and missed per-line observations.
                lines = set()
                for line in range(edit.first, edit.last + 1):
                    lines.update(file["lines"].get(str(line), ()))
                selected.update(lines or scope)
            else:
                selected.update(scope)
    return selected or None


def select_precise(changes: dict, evidence: dict, sources: ZipFile) -> set[int] | None:
    from precision import body_edit_kind, literal_field_classes, parse_edit

    selected = set()
    files = evidence["files"]
    dialects = evidence["dialect_consumers"]
    class_consumers = evidence.get("class_consumers", {})
    observed_classes = evidence.get("observed_class_users", {})
    test_ids = evidence["test_ids"]
    for path, edits in changes.items():
        record = files.get(path)
        narrowed = set()
        if record is not None:
            try:
                old, new = parse_edit(path, edits, sources)
            except (BadZipFile, KeyError, SyntaxError, UnicodeError, ValueError, RecursionError):
                return None
            if ast.dump(old) == ast.dump(new):
                continue
            fields = literal_field_classes(old, new)
            body = body_edit_kind(old, new, edits)
            module = path.removesuffix(".py").replace("/", ".")
            class_users = [observed_classes.get(f"{module}:{name}") for name in fields]
            if fields and all(class_users):
                narrowed = set().union(*class_users)
                if path in dialects:
                    narrowed.update(set(dialects[path]).union(*record["lines"].values()))
                else:
                    narrowed.update(set().union(*(class_consumers.get(name, ()) for name in fields)))
            elif path not in dialects and body == "method":
                scopes = [scope_for(record, edit) for edit in edits]
                if all(scopes):
                    narrowed = set().union(*scopes)
            elif path not in dialects and body == "function":
                narrowed = set().union(*record["lines"].values())
                suites = {test_ids[index].split("::", 1)[0] for index in narrowed}
                narrowed.update(
                    index for index in record["module"]
                    if test_ids[index].split("::", 1)[0] in suites
                )

        if not narrowed:
            broad = select_indices({path: edits}, evidence, "hybrid", sources)
            if broad is None:
                return None
            narrowed = broad
        selected.update(narrowed)
    return selected


def split_lpt(tests: list[tuple[int, int]], worker_count: int) -> tuple[int, list[list[int]]]:
    initial = tests[:worker_count]
    # Seed every worker, including when a test has zero recorded duration.
    workers = [[number] for _, number in initial]
    loads = [(duration, worker) for worker, (duration, _) in enumerate(initial)]
    heapq.heapify(loads)
    for duration, number in tests[worker_count:]:
        load, worker = loads[0]
        heapq.heapreplace(loads, (load + duration, worker))
        workers[worker].append(number)
    return max(load for load, _ in loads), workers


def schedule(tests: list[tuple[int, int]], workers: int | None = None) -> list[list[int]]:
    if not tests:
        return []
    ranked = sorted(tests, key=lambda pair: (-pair[0], pair[1]))
    if workers:
        return split_lpt(ranked, min(workers, len(ranked)))[1]
    total = sum(duration for duration, _ in ranked)
    busiest = ranked[0][0]
    candidates = sorted(
        (max(busiest, (total + k - 1) // k) + WORKER_COST_MS * k, k)
        for k in range(1, min(MAX_WORKERS, len(ranked)) + 1)
    )
    best_cost = math.inf
    best_workers = []
    for lower_bound, count in candidates:
        if lower_bound >= best_cost:
            break
        largest, partition = split_lpt(ranked, count)
        cost = largest + WORKER_COST_MS * count
        if cost < best_cost:
            best_cost, best_workers = cost, partition
    return best_workers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    parser.add_argument("--evidence", type=Path, default=Path(__file__).with_name("coverage.json.gz"))
    parser.add_argument(
        "--strategy",
        choices=["hybrid", "scope-suites", "dialect-guard", "precision", "scope", "line", "all"],
        default="hybrid",
    )
    parser.add_argument("--sources", type=Path, default=Path(__file__).with_name("sources.zip"))
    parser.add_argument("--workers", type=int, choices=range(1, MAX_WORKERS + 1))
    args = parser.parse_args()

    tests = json.loads((args.data_dir / "tests.json").read_text(encoding="utf-8"))["tests"]
    by_number = {
        test["number"]: (math.floor(test["seconds"] * 1_000 + 0.5), test["number"])
        for test in tests
    }
    by_name = {test["id"]: test["number"] for test in tests}
    all_numbers = tuple(sorted(by_number))
    evidence = None
    sources = None
    if args.strategy != "all":
        try:
            evidence = json.loads(gzip.decompress(args.evidence.read_bytes()))
            if evidence["revision"] != PINNED_REVISION:
                evidence = None
        except (OSError, ValueError, KeyError):
            evidence = None
        if evidence is not None:
            try:
                sources = ZipFile(args.sources)
            except (OSError, BadZipFile):
                evidence = None
    if evidence is not None:
        evidence_numbers = [by_name.get(test) for test in evidence["test_ids"]]
        uncovered_numbers = set(by_number) - set(evidence_numbers)
    cache: dict[tuple[int, ...], list[list[int]]] = {}
    selected_count = 0
    total_commits = 0
    fallback_count = 0
    for patch in sorted((args.data_dir / "commits").glob("*.patch")):
        numbers = all_numbers
        if evidence is not None:
            try:
                changes = parse_patch(patch.read_text(encoding="utf-8"))
                if not changes:
                    indices = None
                elif args.strategy == "precision":
                    indices = select_precise(changes, evidence, sources)
                else:
                    indices = select_indices(changes, evidence, args.strategy, sources)
            except (UnicodeError, ValueError):
                indices = None
            if indices is not None:
                covered = {evidence_numbers[index] for index in indices}
                covered.discard(None)
                numbers = tuple(sorted(covered | uncovered_numbers))
            else:
                fallback_count += 1
        if numbers not in cache:
            cache[numbers] = schedule([by_number[number] for number in numbers], args.workers)
        print(json.dumps({"commit": patch.stem, "workers": cache[numbers]}, separators=(",", ":")))
        selected_count += len(numbers)
        total_commits += 1

    print(
        f"{args.strategy}: {selected_count} selected tests across {total_commits} commits; "
        f"{fallback_count} conservative fallbacks",
        file=sys.stderr,
    )
    if sources is not None:
        sources.close()


if __name__ == "__main__":
    main()
