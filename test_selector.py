from itertools import product
from io import BytesIO
import unittest
from zipfile import ZipFile

from selector import parse_patch, patch_compiles, schedule, select_indices


def cost(weights, partition):
    return max((sum(weights[number] for number in worker) for worker in partition), default=0) + 2000 * len(partition)


class SchedulerTests(unittest.TestCase):
    def test_reports_each_test_exactly_once_in_nonempty_workers(self):
        weights = [0, 0, 0, 1000, 2000]
        partition = schedule([(ms, index) for index, ms in enumerate(weights)], workers=5)
        self.assertEqual(sorted(number for worker in partition for number in worker), list(range(5)))
        self.assertTrue(all(partition))

    def test_chooses_worker_cost_aware_partition(self):
        weights = [8000, 7000, 2000, 1000, 0]
        partition = schedule([(ms, index) for index, ms in enumerate(weights)])
        # For this small case, compare to every possible partition.
        optimal = float("inf")
        for count in range(1, len(weights) + 1):
            for assignment in product(range(count), repeat=len(weights)):
                loads = [0] * count
                occupied = set(assignment)
                for index, worker in enumerate(assignment):
                    loads[worker] += weights[index]
                optimal = min(optimal, max(loads) + 2000 * len(occupied))
        self.assertEqual(cost(weights, partition), optimal)


class PatchTests(unittest.TestCase):
    def test_parses_insertion_replacement_and_more_than_one_file(self):
        changes = parse_patch("""diff --git a/sqlglot/a.py b/sqlglot/a.py
--- a/sqlglot/a.py
+++ b/sqlglot/a.py
@@ -10,3 +10,4 @@ function
     a = 1
+    # shifted
-    b = 3
+    b = 4
     return a + b
diff --git a/tests/fixture.sql b/tests/fixture.sql
--- a/tests/fixture.sql
+++ b/tests/fixture.sql
@@ -1 +1 @@
-select 1
+select 2
""")
        self.assertEqual(changes["sqlglot/a.py"][0].first, 11)
        self.assertEqual(changes["sqlglot/a.py"][0].added, ("    # shifted", "    b = 4"))
        self.assertEqual(changes["tests/fixture.sql"][0].last, 1)

    def test_rejects_partial_hunk_and_new_file(self):
        self.assertIsNone(parse_patch("""--- a/sqlglot/a.py
+++ b/sqlglot/a.py
@@ -2,2 +2,2 @@
-foo
+bar
"""))
        self.assertIsNone(parse_patch("""diff --git a/sqlglot/a.py b/sqlglot/a.py
new file mode 100644
--- /dev/null
+++ b/sqlglot/a.py
@@ -0,0 +1 @@
+pass
"""))

    def test_unions_sources_and_source_derived_fixtures(self):
        changes = parse_patch("""diff --git a/sqlglot/a.py b/sqlglot/a.py
--- a/sqlglot/a.py
+++ b/sqlglot/a.py
@@ -3 +3 @@
-    pass
+    return 1
diff --git a/tests/fixture.sql b/tests/fixture.sql
--- a/tests/fixture.sql
+++ b/tests/fixture.sql
@@ -1 +1 @@
-select 1
+select 2
""")
        mapping = {
            "files": {
                "sqlglot/a.py": {"module": [0, 1, 2, 3, 4], "scopes": [[1, 2, 6, True, [0, 2]]],
                                  "lines": {"3": [0]}}
            },
            "fixtures": {"tests/fixture.sql": [1]},
        }
        self.assertEqual(select_indices(changes, mapping, "scope"), {0, 1, 2})
        self.assertEqual(select_indices(changes, mapping, "line"), {0, 1})
        self.assertIsNone(select_indices({**changes, "unknown": []}, mapping, "scope"))

    def test_zero_context_insertion_and_invalid_syntax(self):
        patch = """--- a/sqlglot/a.py
+++ b/sqlglot/a.py
@@ -2,0 +3 @@
+    {line}
"""
        data = BytesIO()
        with ZipFile(data, "w") as sources:
            sources.writestr("sqlglot/a.py", "def foo():\n    out = 1\n    return out\n")
            good = parse_patch(patch.format(line="out = 2"))["sqlglot/a.py"]
            self.assertEqual((good[0].first, good[0].last), (2, 3))
            self.assertTrue(patch_compiles("sqlglot/a.py", good, sources))
            bad = parse_patch(patch.format(line="return +"))["sqlglot/a.py"]
            with self.assertRaises(SyntaxError):
                patch_compiles("sqlglot/a.py", bad, sources)


if __name__ == "__main__":
    unittest.main()
