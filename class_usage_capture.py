"""Collect class consumers from pinned SQLGlot tests and their spawned work."""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import gzip
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

REVISION = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
_ACTIVE: dict[int, type] | None = None
_NAME = ''
_INSTALLED = False


def _record(cls: type) -> None:
    if _ACTIVE is not None:
        _ACTIVE.setdefault(id(cls), cls)


def _symbols(types: dict[int, type]) -> set[str]:
    return {
        f'{base.__module__}:{base.__qualname__}'
        for cls in types.values()
        for base in cls.__mro__
        if base.__module__.startswith('sqlglot.')
    }


def _worker(name, destination, function, args, kwargs):
    global _ACTIVE, _NAME
    _install()
    _NAME = name
    _ACTIVE = {}
    try:
        return function(*args, **kwargs)
    finally:
        keys = _symbols(_ACTIVE)
        _ACTIVE = None
        _NAME = ''
        with Path(destination, f'{os.getpid()}.jsonl').open('a') as stream:
            stream.write(json.dumps((name, sorted(keys)), separators=(',', ':')) + '\n')


def _install(destination=None):
    global _INSTALLED
    if _INSTALLED:
        return
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr

    expr_init = Expr.__init__
    dialect_init = Dialect.__init__
    resolve_dialect = Dialect.get_or_raise.__func__

    def expr(self, *args, **kwargs):
        _record(type(self))
        return expr_init(self, *args, **kwargs)

    def dialect(self, *args, **kwargs):
        _record(type(self))
        return dialect_init(self, *args, **kwargs)

    def resolve(cls, dialect):
        result = resolve_dialect(cls, dialect)
        _record(type(result))
        return result

    Expr.__init__ = expr
    Dialect.__init__ = dialect
    Dialect.get_or_raise = classmethod(resolve)
    if destination:
        submit = ProcessPoolExecutor.submit
        pool_init = ProcessPoolExecutor.__init__

        def limited_pool(executor, max_workers=None, *args, **kwargs):
            return pool_init(executor, min(max_workers or 2, 2), *args, **kwargs)

        ProcessPoolExecutor.__init__ = limited_pool

        def contextual_submit(executor, function, /, *args, **kwargs):
            if not _NAME:
                return submit(executor, function, *args, **kwargs)
            return submit(executor, _worker, _NAME, destination, function, args, kwargs)

        ProcessPoolExecutor.submit = contextual_submit
    _INSTALLED = True


def collect(root, output):
    import pytest
    import class_usage_capture as capture

    os.chdir(root)
    sys.path.insert(0, str(root))
    by_context: dict[str, set[str]] = defaultdict(set)
    tests = []

    class Observer:
        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            capture._ACTIVE, capture._NAME = {}, item.nodeid
            try:
                yield
            finally:
                by_context[item.nodeid].update(_symbols(capture._ACTIVE))
                capture._ACTIVE, capture._NAME = None, ''

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_make_collect_report(self, collector):
            if isinstance(collector, pytest.Module):
                context = f'module:{collector.nodeid}'
                capture._ACTIVE, capture._NAME = {}, context
                try:
                    yield
                finally:
                    by_context[context].update(_symbols(capture._ACTIVE))
                    capture._ACTIVE, capture._NAME = None, ''
            else:
                yield

        def pytest_collection_finish(self, session):
            tests.extend(sorted(item.nodeid for item in session.items))

    with TemporaryDirectory(dir=output.parent, prefix='class-workers-') as temporary:
        capture._install(temporary)
        result = pytest.main(['-q', '-p', 'no:cacheprovider', 'tests'], plugins=[Observer()])
        if result != pytest.ExitCode.OK:
            raise SystemExit(f'Pinned suite with class instrumentation returned {int(result)}')
        worker_files = list(Path(temporary).glob('*.jsonl'))
        worker_contexts = set()
        for path in worker_files:
            for line in path.read_text().splitlines():
                context, keys = json.loads(line)
                worker_contexts.add(context)
                by_context[context].update(keys)

    by_test = {name: index for index, name in enumerate(tests)}
    by_module = defaultdict(set)
    for test_id, index in by_test.items():
        by_module[test_id.split('::', 1)[0]].add(index)
    by_class = defaultdict(set)
    for context, keys in by_context.items():
        users = by_module[context.removeprefix('module:')] if context.startswith('module:') else {by_test[context]}
        for name in keys:
            by_class[name].update(users)
    evidence = {
        'revision': REVISION,
        'test_ids': tests,
        'class_users': {name: sorted(users) for name, users in sorted(by_class.items())},
        'spawned_test_contexts': sorted(worker_contexts),
    }
    output.write_bytes(gzip.compress(json.dumps(evidence, separators=(',', ':')).encode(), mtime=0))
    print(f'Class evidence covers {len(by_class)} types across {len(tests)} tests and {len(worker_contexts)} spawned test contexts.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pinned', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    collect(args.pinned.resolve(), args.output.resolve())
