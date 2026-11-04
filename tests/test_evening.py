"""Regression checks for the bounded evening fallback. Network and AI are mocked."""
import datetime as dt
import threading
import unittest
from unittest.mock import patch
import test_behavior as support
import core
import tasks


class EveningTests(unittest.TestCase):
    setUp = support.BehaviorTests.setUp
    tearDown = support.BehaviorTests.tearDown
    row = support.BehaviorTests.row
    save = support.BehaviorTests.save

    def configure(self, hour=20, **values):
        core.now.return_value = dt.datetime(2026, 11, 4, hour, 0, tzinfo=core.TZ)
        cfg = core.settings()
        cfg.update(auto=True, ai_after_fetch=True, time="20:00", **values)
        core.atomic_json(core.safe_path("config", "settings.json"), cfg)
        return core.manual_periods()

    def save_scope(self, completed=False):
        targets = core.manual_periods()
        for index, spec in enumerate(targets):
            core.save_report(spec, [self.row(f"2611.{index:05}")], 1)
        if completed:
            with core.db() as con:
                con.execute("UPDATE papers SET ai_state='done',translation='已有译文',intro='已有简介'")
        return targets

    def test_before_twenty_no_network_key_read_or_nightly_claim(self):
        self.configure(hour=19)
        with patch.object(core, "fetch_report") as fetch, patch.object(core, "get_key") as key:
            self.assertFalse(tasks.run("auto"))
            tasks.start("auto")
            fetch.assert_not_called()
            key.assert_not_called()
        with core.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM evening_runs").fetchone()[0], 0)

    def test_manual_scope_complete_skips_both_network_and_ai(self):
        self.configure()
        self.save_scope(completed=True)
        with patch.object(core, "fetch_report") as fetch, patch.object(core, "ai_read") as ai, patch.object(core, "get_key") as key:
            self.assertTrue(tasks.run("auto"))
            fetch.assert_not_called()
            ai.assert_not_called()
            key.assert_not_called()
            self.assertFalse(tasks.run("auto"))

    def test_manual_fetch_only_evening_completes_ai_once(self):
        self.configure()
        self.save_scope()
        seen = []
        def reading(pid, cfg, deadline):
            seen.append(pid)
            with core.db() as con:
                con.execute("UPDATE papers SET ai_state='done' WHERE id=?", (pid,))
        with patch.object(core, "fetch_report") as fetch, patch.object(core, "get_key", return_value="fake"), patch.object(core, "ai_read", side_effect=reading):
            tasks.run("auto")
            tasks.run("auto")
            fetch.assert_not_called()
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

    def test_missing_scope_fetches_exactly_five_targets_and_no_homepage(self):
        targets = self.configure()
        with patch.object(core, "fetch_report", return_value=([self.row()], 1)) as fetch, patch.object(core, "fetch_latest_daily") as home, patch.object(core, "get_key", return_value=None):
            tasks.run("auto")
        self.assertEqual([call.args[0]["id"] for call in fetch.call_args_list], [s["id"] for s in targets])
        home.assert_not_called()

    def test_manual_network_failure_gets_one_evening_attempt_then_stops(self):
        targets = self.configure()
        tasks.attempt(targets[0], "failed", "offline during manual run")
        with patch.object(core, "fetch_report", side_effect=RuntimeError("offline")) as fetch:
            tasks.run("auto")
            tasks.run("auto")
        fetch.assert_called_once()

    def test_day_unpublished_during_manual_check_can_be_fetched_in_evening(self):
        targets = self.configure()
        self.save_scope(completed=True)
        with core.db() as con:
            con.execute("DELETE FROM reports WHERE id=?", (targets[0]["id"],))
        tasks.attempt(targets[0], "unavailable", "source has no matching date")
        with patch.object(core, "fetch_report", return_value=([self.row()], 1)) as fetch, patch.object(core, "get_key", return_value=None):
            tasks.run("auto")
            tasks.run("auto")
            fetch.assert_called_once()

    def test_previous_week_month_snapshot_is_refreshed_in_evening(self):
        self.configure(hour=19)
        core.now.return_value -= dt.timedelta(days=1)
        self.save_scope(completed=True)
        targets = self.configure()
        with patch.object(core, "fetch_report", return_value=([self.row()], 1)) as fetch, patch.object(core, "get_key", return_value=None):
            tasks.run("auto")
        self.assertEqual([c.args[0]["id"] for c in fetch.call_args_list], [targets[0]["id"], targets[3]["id"], targets[4]["id"]])

    def test_evening_reads_more_than_one_manual_batch_but_obeys_budget(self):
        self.configure(daily_ai_limit=21)
        targets = core.manual_periods()
        for group, spec in enumerate(targets):
            core.save_report(spec, [self.row(f"2611.{group * 100 + i:05}") for i in range(core.paper_limit(spec["kind"]))], 30)
        def reading(pid, cfg, deadline):
            with core.db() as con:
                con.execute("INSERT INTO usage(day,calls) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1", (core.now().date().isoformat(),))
                con.execute("UPDATE papers SET ai_state='done' WHERE id=?", (pid,))
        with patch.object(core, "get_key", return_value="fake"), patch.object(core, "ai_read", side_effect=reading) as ai:
            tasks.run("auto")
        self.assertEqual(ai.call_count, 21)
        self.assertEqual(core.stats()["usage"]["calls"], 21)
        self.assertIn("上限", tasks.lane_status("ai")["errors"][0])

    def test_overlapping_evening_workers_claim_day_once(self):
        self.configure()
        entered = threading.Event()
        release = threading.Event()
        def fetching(spec, deadline):
            entered.set()
            release.wait(3)
            return [self.row()], 1
        with patch.object(core, "fetch_report", side_effect=fetching) as fetch, patch.object(core, "get_key", return_value=None):
            first = threading.Thread(target=tasks.run, args=("auto",))
            second = threading.Thread(target=tasks.run, args=("auto",))
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            release.set()
            first.join(4)
            second.join(4)
            self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(fetch.call_count, 5)

    def test_stop_ai_while_fetching_prevents_evening_handoff(self):
        self.configure()
        def fetching(spec, deadline):
            tasks.stop("ai")
            return [self.row()], 1
        with patch.object(core, "fetch_report", side_effect=fetching), patch.object(core, "ai_read") as ai:
            tasks.run("auto")
            ai.assert_not_called()


if __name__ == "__main__":
    unittest.main()
