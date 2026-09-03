import unittest

import benchmark_campaign
import skill_state


class RuntimeTests(unittest.TestCase):
    def test_runtime_invariants(self):
        skill_state.self_test()

    def test_campaign_order_is_balanced(self):
        orders = [benchmark_campaign.balanced_order(index) for index in range(20)]
        self.assertEqual(orders.count("vanilla-first"), 10)
        for modulus in (4, 5):
            for value in range(modulus):
                group = [order for index, order in enumerate(orders) if index % modulus == value]
                self.assertLessEqual(abs(group.count("vanilla-first") - group.count("state-first")), 1)


if __name__ == "__main__":
    unittest.main()
