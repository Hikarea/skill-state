"""Deterministic transition laws, not model accuracy or efficiency evidence."""
import copy
import itertools
import unittest

from context_policy import merge_state
from skill_state import merge_patch, validate_state


class StateAlgebraTests(unittest.TestCase):
    def test_identity_idempotence_and_nonmutation(self):
        states = [{}, {"x": None}, {"x": [1]}, {"x": {"old": 1}}]
        patches = [{}, {"x": None}, {"x": [2]}, {"x": {"old": None, "new": 2}}]
        self.assertIs(merge_patch, merge_state)
        for state, patch in itertools.product(states, patches):
            original = copy.deepcopy((state, patch))
            result = merge_state(state, patch)
            self.assertEqual(merge_state(state, {}), state)
            self.assertEqual(merge_state(result, patch), result)
            self.assertEqual((state, patch), original)

    def test_result_does_not_alias_inputs(self):
        state = {"retained": {"values": [1]}}
        patch = {"new": [2]}
        result = merge_state(state, patch)
        result["retained"]["values"].append(3)
        result["new"].append(4)
        self.assertEqual(state, {"retained": {"values": [1]}})
        self.assertEqual(patch, {"new": [2]})

    def test_conflicts_are_order_sensitive_and_patch_composition_is_not_merge(self):
        state, delete, add = {"x": 1}, {"x": None}, {"x": 2}
        self.assertNotEqual(merge_state(merge_state(state, delete), add),
                            merge_state(merge_state(state, add), delete))
        # Merging patches loses a deletion marker; never batch patches this way.
        self.assertNotEqual(merge_state(merge_state(state, add), delete),
                            merge_state(state, merge_state(add, delete)))

    def test_nonfinite_state_and_nonobject_inputs_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.assertRaises(ValueError):
                validate_state({"x": value}, {"type": "object"})
        for state, patch in (([], {}), ({}, [])):
            with self.assertRaises(ValueError):
                merge_state(state, patch)


if __name__ == "__main__":
    unittest.main()
