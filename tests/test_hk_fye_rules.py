import unittest

from data_processors import hk_watchlist_engine as engine


class HKFyeRuleTests(unittest.TestCase):
    def test_build_target_name_short_respects_fye(self) -> None:
        self.assertEqual(engine.build_target_name_short(2025, "Annual", "季度披露", 12), "25Q4")
        self.assertEqual(engine.build_target_name_short(2026, "Q3", "季度披露", 3), "25Q3")

    def test_detect_notice_span_months_ignores_date_month(self) -> None:
        self.assertEqual(engine._detect_notice_span_months("截至2025年12月31日止三个月"), 3)
        self.assertEqual(engine._detect_notice_span_months("截至2025年9月30日止三个月和六个月"), 6)
        self.assertEqual(engine._detect_notice_span_months("截至2025年12月31日止年度业绩"), 12)

    def test_notice_pair_infers_fye_and_q3_for_march_fye(self) -> None:
        notice1_xxxx = "三个月"
        notice2_xxxx = "三个月和六个月"

        notice1_period = engine.derive_notice1_period(notice1_xxxx, "")
        self.assertEqual(notice1_period, "")

        notice2_period = engine.derive_notice1_period(notice2_xxxx, "")
        self.assertEqual(notice2_period, "Interim")

        notice1_fye = engine.derive_fye_from_notice(
            notice1_year=2025,
            notice1_month=12,
            notice1_xxxx=notice1_xxxx,
            notice1_three_month_period_hint="",
            fye_hint=12,
        )
        notice2_fye = engine.derive_fye_from_notice(
            notice1_year=2025,
            notice1_month=9,
            notice1_xxxx=notice2_xxxx,
            notice1_three_month_period_hint="",
            fye_hint=notice1_fye,
        )
        self.assertEqual(notice2_fye, 3)

        corrected_notice1_fye = notice2_fye
        notice1_fy = engine.derive_fy(2025, 12, corrected_notice1_fye)
        notice2_fy = engine.derive_fy(2025, 9, notice2_fye)
        self.assertEqual(notice1_fy, 2026)
        self.assertEqual(notice2_fy, 2026)

        inferred_period = engine.infer_three_month_period_from_notice_pair(
            notice1_xxxx=notice1_xxxx,
            notice2_period=notice2_period,
            notice1_fy=notice1_fy,
            notice2_fy=notice2_fy,
        )
        self.assertEqual(inferred_period, "Q3")

    def test_pure_three_month_pair_overrides_wrong_llm_hint_for_alibaba_style_notice(self) -> None:
        notice1_year = 2025
        notice1_month = 12
        notice1_xxxx = "截至2025年12月31日止三个月的未经审核业绩"
        wrong_llm_hint = "Q1"

        notice2_year = 2025
        notice2_month = 9
        notice2_xxxx = "截至2025年9月30日止三个月和六个月的未经审核业绩"

        notice1_period = engine.derive_notice1_period(notice1_xxxx, wrong_llm_hint)
        self.assertEqual(notice1_period, "Q1")

        notice2_period = engine.derive_notice1_period(notice2_xxxx, "")
        notice2_fye = engine.derive_fye_from_notice(
            notice1_year=notice2_year,
            notice1_month=notice2_month,
            notice1_xxxx=notice2_xxxx,
            notice1_three_month_period_hint="",
            fye_hint=3,
        )
        self.assertEqual(notice2_period, "Interim")
        self.assertEqual(notice2_fye, 3)

        pair_notice1_fy = engine.derive_fy(notice1_year, notice1_month, notice2_fye)
        notice2_fy = engine.derive_fy(notice2_year, notice2_month, notice2_fye)
        pair_inferred_period = engine.infer_three_month_period_from_notice_pair(
            notice1_xxxx=notice1_xxxx,
            notice2_period=notice2_period,
            notice1_fy=pair_notice1_fy,
            notice2_fy=notice2_fy,
        )

        self.assertEqual(pair_notice1_fy, 2026)
        self.assertEqual(notice2_fy, 2026)
        self.assertEqual(pair_inferred_period, "Q3")
        self.assertEqual(engine.build_target_name_short(pair_notice1_fy, pair_inferred_period, "季度披露", notice2_fye), "25Q3")

    def test_pure_three_month_uses_fye_hint_to_correct_wrong_q1_hint(self) -> None:
        notice1_xxxx = "截至2025年12月31日止三个月的未经审核业绩"
        derived_fye = engine.derive_fye_from_notice(
            notice1_year=2025,
            notice1_month=12,
            notice1_xxxx=notice1_xxxx,
            notice1_three_month_period_hint="Q1",
            fye_hint=3,
        )
        inferred_period = engine.infer_three_month_period_from_fye_and_end_month(12, derived_fye)

        self.assertEqual(derived_fye, 3)
        self.assertEqual(inferred_period, "Q3")


if __name__ == "__main__":
    unittest.main()
