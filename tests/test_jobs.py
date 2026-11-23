import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import ANY, patch

from codex_client import CodexError, CodexQuota, Connection, QuotaWindow, RequestError
from jobs import JobStore, run_job, task_name


class JobTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = JobStore(temp.name)
        self.thread = {"id": "thread-1", "cwd": str(Path(temp.name)), "name": "中文会话"}
        self.connection = Connection(temp.name)
        patcher = patch("jobs.scheduler")
        self.scheduler = patcher.start()
        self.addCleanup(patcher.stop)

    def schedule(self, due=True, continuous=False):
        job = self.store.schedule(self.connection, self.thread, "继续任务\n保留换行", time.time() + 300, True, continuous)
        if due:
            with self.store.db() as db:
                db.execute("UPDATE jobs SET scheduled_at = ? WHERE id = ?", (time.time() - 1, job))
        return job

    def fake_server(self):
        target_patcher = patch("jobs.read_local_target", return_value=self.thread)
        server_patcher = patch("jobs.AppServer")
        target = target_patcher.start()
        server_cls = server_patcher.start()
        self.addCleanup(target_patcher.stop)
        self.addCleanup(server_patcher.stop)
        server = server_cls.return_value.__enter__.return_value
        server.request.return_value = {"thread": {"id": self.thread["id"], "status": {"type": "idle"}}}
        server.start_turn.return_value = {"id": "turn-1"}
        server.wait_turn.return_value = {"status": "completed"}
        server.read_goal.return_value = None
        return target, server

    def test_register_failure_rolls_back(self):
        self.scheduler.side_effect = RuntimeError("scheduler failure")
        with self.assertRaises(RuntimeError):
            self.schedule()
        self.assertEqual(self.store.all(), [])

    def test_cancel_prevents_submission_even_if_windows_delete_fails(self):
        job = self.schedule()
        self.scheduler.side_effect = RuntimeError("delete failed")
        with self.assertRaises(RuntimeError):
            self.store.cancel(job)
        self.scheduler.assert_called_with("remove", name=task_name(job))
        with patch("jobs.send_job") as send:
            self.assertEqual(run_job(self.store, job), 0)
            send.assert_not_called()
        self.assertEqual(self.store.get(job)["state"], "cancelled")

    def test_duplicate_trigger_sends_only_once(self):
        job = self.schedule()
        target, server = self.fake_server()
        self.assertEqual(run_job(self.store, job), 0)
        self.assertEqual(run_job(self.store, job), 0)
        target.assert_called_once_with(self.connection, self.thread["id"])
        server.start_turn.assert_called_once_with(self.thread["id"], "继续任务\n保留换行")
        server.wait_turn.assert_called_once_with(self.thread["id"], "turn-1", timeout=ANY)
        self.assertEqual(self.store.get(job)["state"], "completed")

    def test_disconnect_after_submission_is_unknown_and_is_not_retried(self):
        job = self.schedule()
        _, server = self.fake_server()
        server.start_turn.side_effect = CodexError("lost connection")
        self.assertEqual(run_job(self.store, job), 2)
        self.assertEqual(run_job(self.store, job), 0)
        self.assertEqual(self.store.get(job)["state"], "unknown")
        server.start_turn.assert_called_once()

    def test_explicit_rejection_is_recorded_as_failure(self):
        job = self.schedule()
        _, server = self.fake_server()
        server.start_turn.side_effect = RequestError("turn/start", {"code": -32602, "message": "rejected"})
        self.assertEqual(run_job(self.store, job), 2)
        self.assertEqual(self.store.get(job)["state"], "failed")

    def test_active_thread_is_exclusive_and_orphaned_submit_is_unknown(self):
        first, second = self.schedule(), self.schedule()
        self.store.transition(first, "scheduled", "submitting", "test submit")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.transition(second, "scheduled", "preparing", "test duplicate thread")
        with self.store.worker_lock(first) as acquired:
            self.assertTrue(acquired)
            self.store.reconcile_workers()
            self.assertEqual(self.store.get(first)["state"], "submitting")
        self.store.reconcile_workers()
        self.assertEqual(self.store.get(first)["state"], "unknown")

    def test_continuation_uses_later_exhausted_window_and_next_round_stops_on_done(self):
        job = self.schedule(continuous=True)
        _, server = self.fake_server()
        short_reset, week_reset = int(time.time()) + 600, int(time.time()) + 7200
        server.read_quota.return_value = CodexQuota(QuotaWindow(100, short_reset), QuotaWindow(100, week_reset))
        server.wait_turn.return_value = {"status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}
        self.assertEqual(run_job(self.store, job), 0)
        child, parent = self.store.all()
        self.assertEqual(parent["state"], "rescheduled")
        self.assertEqual(child["parent_id"], job)
        self.assertEqual(child["scheduled_at"], week_reset + 60)
        self.assertEqual(child["message"], parent["message"])
        self.scheduler.assert_called_with("register", name=task_name(child["id"]), wake=True, when=ANY,
                                          executable=ANY, arguments=ANY, cwd=ANY)
        self.assertEqual(run_job(self.store, job), 0)
        self.assertEqual(len(self.store.all()), 2)
        child_id = child["id"]
        with self.store.db() as db:
            db.execute("UPDATE jobs SET scheduled_at = ? WHERE id = ?", (time.time() - 1, child_id))
        server.wait_turn.return_value = {"status": "completed"}
        server.last_agent_message = f"任务已完成。\nAUTO_RESUME_{child_id}: DONE\n"
        self.assertEqual(run_job(self.store, child_id), 0)
        self.assertEqual(self.store.get(child_id)["state"], "completed")
        self.assertEqual(len(self.store.all()), 2)

    def test_unmarked_success_does_not_schedule_again(self):
        job = self.schedule(continuous=True)
        _, server = self.fake_server()
        server.last_agent_message = "还有任务，但缺少明确状态行。"
        self.assertEqual(run_job(self.store, job), 2)
        self.assertEqual(self.store.get(job)["state"], "review")
        server.read_quota.assert_not_called()
        self.assertEqual(len(self.store.all()), 1)

    def test_native_goal_completion_overrides_quota_failure(self):
        job = self.schedule(continuous=True)
        _, server = self.fake_server()
        goal = {"objective": "Finish the test", "createdAt": 123, "status": "active"}
        server.read_goal.side_effect = [goal, {**goal, "status": "complete"}]
        server.wait_turn.return_value = {"status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}
        self.assertEqual(run_job(self.store, job), 0)
        server.start_turn.assert_called_once_with(self.thread["id"], "继续任务\n保留换行")
        self.assertEqual(self.store.get(job)["state"], "completed")
        server.read_quota.assert_not_called()

    def test_stop_during_active_run_prevents_followup(self):
        job = self.schedule(continuous=True)
        _, server = self.fake_server()

        def wait(*args, **kwargs):
            self.store.stop_followups(job)
            server.last_agent_message = f"AUTO_RESUME_{job}: CONTINUE"
            return {"status": "completed"}

        server.wait_turn.side_effect = wait
        self.assertEqual(run_job(self.store, job), 0)
        self.assertEqual(self.store.get(job)["state"], "stopped")
        self.assertEqual(len(self.store.all()), 1)
        server.read_quota.assert_not_called()

    def test_failed_followup_registration_does_not_leave_partial_chain(self):
        job = self.schedule(continuous=True)
        _, server = self.fake_server()
        server.last_agent_message = f"AUTO_RESUME_{job}: CONTINUE"
        server.read_quota.return_value = CodexQuota(QuotaWindow(50, int(time.time()) + 600), None)
        self.scheduler.side_effect = RuntimeError("scheduler failure")
        self.assertEqual(run_job(self.store, job), 2)
        self.assertEqual(self.store.get(job)["state"], "review")
        self.assertEqual(len(self.store.all()), 1)

    def test_second_active_chain_for_same_thread_is_rejected(self):
        self.schedule(continuous=True)
        with self.assertRaises(ValueError):
            self.schedule(continuous=True)
        self.assertEqual(len(self.store.all()), 1)

    def test_existing_one_shot_job_survives_schema_upgrade(self):
        job_id = self.schedule(due=False)
        with self.store.db() as db:
            db.execute("DROP TABLE continuations")
        saved = JobStore(self.store.root).get(job_id)
        self.assertEqual(saved["state"], "scheduled")
        self.assertFalse(saved["continuous"])


if __name__ == "__main__":
    unittest.main()
