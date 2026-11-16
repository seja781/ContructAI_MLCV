import unittest

import numpy as np
import pandas as pd

from house_price.train import LocalityRateBaseline, _validation_groups, scores


class LocalityRateBaselineTests(unittest.TestCase):
    def setUp(self):
        self.train = pd.DataFrame({
            "locality": ["A", "A", "B", "B"],
            "locality_group": ["Other"] * 4,
            "bedrooms": [2] * 4,
            "property_type": ["Apartment"] * 4,
            "log_area": np.log([1000] * 4),
        })
        self.target = pd.Series(np.log([100, 100, 200, 200]))
        self.model = LocalityRateBaseline().fit(self.train, self.target)

    def test_comparables_use_exact_locality_not_shared_other_group(self):
        predicted = np.exp(self.model.predict(self.train.iloc[[0, 2]]))
        np.testing.assert_allclose(predicted, [100, 200], rtol=1e-6)

    def test_unknown_locality_falls_back_to_city_rate(self):
        query = self.train.iloc[[0]].copy()
        query["locality"] = "Unknown"
        predicted = np.exp(self.model.predict(query))[0]
        self.assertAlmostEqual(predicted, 150, places=5)

    def test_zero_weight_and_zero_prior_fall_back_to_locality_rate(self):
        query = self.train.iloc[[0]].copy()
        query["log_area"] += 100
        model = LocalityRateBaseline(area_bandwidth=0.02, prior_strength=0).fit(self.train, self.target)
        prediction = model.predict(query)[0]
        self.assertAlmostEqual(prediction, np.log(0.1) + query.log_area.iloc[0], places=5)

    def test_grouped_validation_keeps_matching_listing_signatures_together(self):
        x = self.train.iloc[[0, 1, 2]].copy()
        groups = _validation_groups(x)
        self.assertEqual(groups[0], groups[1])
        self.assertNotEqual(groups[0], groups[2])

    def test_scores_include_real_price_rmse(self):
        result = scores(np.log([100, 200]), np.log([110, 180]))
        self.assertAlmostEqual(result["RMSE (lakh)"], np.sqrt(250), places=5)


if __name__ == "__main__":
    unittest.main()
