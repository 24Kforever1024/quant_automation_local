import unittest

from utils.periods import (
    bgqs_from_period_label,
    disclosure_year_from_period_label,
    infer_market_from_code,
    median_mmdd,
    next_period_label,
    previous_period_label,
    quarter_label_from_date,
)


class PeriodHelperTests(unittest.TestCase):
    def test_market_inference(self) -> None:
        self.assertEqual(infer_market_from_code("1211.HK"), "港股")
        self.assertEqual(infer_market_from_code("601857.SH"), "A股")
        self.assertEqual(infer_market_from_code("AAPL.O"), "美股")

    def test_quarter_labels(self) -> None:
        self.assertEqual(quarter_label_from_date("2025-03-31", "A"), "25Q1A")
        self.assertEqual(next_period_label("25Q4A"), "26Q1E")
        self.assertEqual(previous_period_label("25Q1E"), "24Q4A")
        self.assertEqual(next_period_label("FY25Q4A"), "FY26Q1E")
        self.assertEqual(previous_period_label("FY26Q1E"), "FY25Q4A")

    def test_disclosure_mapping(self) -> None:
        self.assertEqual(bgqs_from_period_label("25Q1E"), "一季报")
        self.assertEqual(bgqs_from_period_label("25Q4E"), "年报")
        self.assertEqual(bgqs_from_period_label("FY26H2E"), "年报")
        self.assertEqual(disclosure_year_from_period_label("25Q4E"), 2026)
        self.assertEqual(disclosure_year_from_period_label("FY26Q3E"), 2026)

    def test_median_mmdd(self) -> None:
        self.assertEqual(median_mmdd(["2024-03-28", "2023-03-30", "2022-03-29"]), (3, 29))


if __name__ == "__main__":
    unittest.main()
