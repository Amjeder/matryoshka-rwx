import ast
import unittest

from precision import body_edit_kind, literal_field_classes
from selector import Edit


class PrecisionTests(unittest.TestCase):
    def test_confines_method_edits_to_existing_body(self):
        old = ast.parse("class Example:\n    def run(self):\n        return 1\n")
        new = ast.parse("class Example:\n    def run(self):\n        return 2\n")
        edit = Edit(3, 3, ("        return 1",), ("        return 2",))
        self.assertEqual(body_edit_kind(old, new, [edit]), "method")

        new.body.append(ast.parse("registered = Example()").body[0])
        self.assertIsNone(body_edit_kind(old, new, [edit]))

    def test_class_metadata_preserves_shape(self):
        old = ast.parse("class Example:\n    config = {'active': True}\n")
        new = ast.parse("class Example:\n    config = {'active': False}\n")
        self.assertEqual(literal_field_classes(old, new), {"Example"})
        added = ast.parse("class Example:\n    config = {'active': True, 'ready': False}\n")
        self.assertEqual(literal_field_classes(old, added), set())

    def test_special_class_fields_use_broad_policy(self):
        old = ast.parse("class Example:\n    __slots__ = ('old',)\n")
        new = ast.parse("class Example:\n    __slots__ = ('new',)\n")
        self.assertEqual(literal_field_classes(old, new), set())


if __name__ == "__main__":
    unittest.main()
