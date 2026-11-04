"""Coverage for current HF periods and different per-period paper limits."""
import datetime as dt
import unittest
from unittest.mock import patch
import test_behavior as support
import core
import tasks


class PeriodTests(unittest.TestCase):
    setUp = support.BehaviorTests.setUp
    tearDown = support.BehaviorTests.tearDown
    row = support.BehaviorTests.row
    save = support.BehaviorTests.save

    def test_scope_has_three_calendar_days_and_current_week_month(self):
        specs = core.manual_periods(dt.date(2026, 11, 4))
        self.assertEqual([s["period"] for s in specs], ["2026-11-04", "2026-11-03", "2026-11-02", "2026-W45", "2026-11"])
        self.assertEqual(specs[3]["start"], "2026-11-01")

    def test_calendar_boundary_keeps_exactly_three_days(self):
        specs = core.manual_periods(dt.date(2027, 1, 1))
        self.assertEqual([s["period"] for s in specs[:3]], ["2027-01-01", "2026-12-31", "2026-12-30"])
        sunday = core.current_periods(dt.date(2026, 11, 1))[1]
        self.assertEqual(sunday["start"], "2026-11-01")

    def test_daily_ten_weekly_monthly_twenty_and_short_lists(self):
        for kind, value, label, limit in [("daily", "2026-10-30", "day", 10), ("weekly", "2026-W44", "week", 20), ("monthly", "2026-10", "month", 20)]:
            spec = core.period(kind, value)
            for size in [4, 30]:
                with self.subTest(kind=kind, size=size):
                    props = {"periodType": label, "dateString": spec["start"], "dailyPapers": [{"paper": {"id": f"2610.{i:05}", "title": "Title", "summary": "Abstract"}} for i in range(size)]}
                    rows, total = core.parse_report(props, spec)
                    self.assertEqual(len(rows), min(limit, size))
                    self.assertEqual(total, size)

    def test_current_week_and_month_are_accepted_without_future_entry_dates(self):
        for spec in core.current_periods()[1:]:
            with patch.object(core, "fetch_report", return_value=([self.row()], 1)) as fetch:
                self.assertTrue(tasks.run("manual", spec))
                fetch.assert_called_once()
        papers = tasks.list_data({})["papers"]
        self.assertEqual(papers[0]["selected_date"], core.now().date().isoformat())
        self.assertEqual(tasks.list_data({"start": "2026-11-01"})["papers"], [])

    def test_old_twenty_paper_snapshot_and_translations_survive_new_ten_limit(self):
        spec = self.save("daily", "2026-10-30", [self.row(f"2610.{i:05}") for i in range(20)])
        with core.db() as con:
            con.execute("UPDATE papers SET ai_state='done',translation='已有译文',intro='已有简介'")
        with patch.object(core, "fetch_report") as fetch:
            tasks.run("manual", spec)
            fetch.assert_not_called()
        self.assertEqual(core.stats()["papers"], 20)
        with core.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM papers WHERE translation='已有译文'").fetchone()[0], 20)


if __name__ == "__main__":
    unittest.main()
