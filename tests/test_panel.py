from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from codex_client import CodexError
from jobs import JobStore
from panel.panel_service import PanelService, native_thread_id


class PanelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = JobStore(temp.name)
        self.panel = PanelService(self.store)
        scheduler = patch("jobs.scheduler")
        target = patch("panel.panel_service.check_target")
        self.scheduler = scheduler.start()
        self.target = target.start()
        reader = patch("panel.panel_service.read_local_target", self.target)
        reader.start()
        self.addCleanup(scheduler.stop)
        self.addCleanup(target.stop)
        self.addCleanup(reader.stop)
        self.target.side_effect = lambda connection, thread: {"id": thread, "cwd": temp.name, "name": "对话 " + thread}
        self.when = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")

    def bind(self, name):
        key = self.panel.open({"threadId": name})["binding"]
        self.assertEqual(self.panel.bind(key)["id"], name)
        return key

    def test_metadata_aliases_and_manual_link(self):
        self.assertEqual(native_thread_id({"threadId": "abc", "thread_id": "abc"}), "abc")
        self.assertIsNone(native_thread_id({"threadId": "abc", "thread_id": "def"}))
        self.assertIsNone(native_thread_id({"openai/session": "abc"}))
        self.assertIsNone(native_thread_id({"threadId": True}))
        key = self.panel.open({})["binding"]
        self.assertEqual(self.panel.bind(key)["state"], "unbound")
        self.assertEqual(self.panel.bind(key, "codex://threads/example")['id'], "example")
        with self.assertRaises(ValueError):
            self.panel.bind(key, "codex://threads/other")

    def test_failed_native_validation_stays_unbound(self):
        key = self.panel.open({"threadId": "unavailable"})["binding"]
        self.target.side_effect = CodexError("目录不可用")
        self.assertEqual(self.panel.bind(key)["state"], "unbound")
        with self.assertRaises(ValueError):
            self.panel.jobs(key)

    def test_conversations_are_isolated_and_existing_jobs_are_visible(self):
        a, b = self.bind("a"), self.bind("b")
        job = self.store.schedule(self.panel.connection, self.panel.target(a), "old", datetime.fromisoformat(self.when).timestamp(), False)
        self.assertEqual(self.panel.jobs(a)["jobs"][0]["id"], job)
        self.assertEqual(self.panel.jobs(b)["jobs"], [])
        with self.assertRaises(ValueError):
            self.panel.stop(b, job, "cancel")
        with self.assertRaises(ValueError):
            self.panel.events(b, job)
        self.assertEqual(self.panel.stop(a, job, "cancel")["state"], "cancelled")

    def test_concurrent_retries_register_one_task_and_preserve_request_identity(self):
        key = self.bind("a")
        request = uuid4().hex
        args = (key, request, "manual", self.when, "go", False, True)
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.panel.create, *args)
            second = pool.submit(self.panel.create, *args)
            self.assertEqual(first.result(), second.result())
        self.scheduler.assert_called_once()
        self.assertEqual(len(self.store.all()), 1)
        with self.assertRaises(ValueError):
            self.panel.create(key, request, "manual", self.when, "other", False, True)

    def test_new_chat_and_model_choices_are_saved_on_the_confirming_panel(self):
        a, b = self.bind("a"), self.bind("b")
        with patch.object(self.panel, "models", return_value={"models": [{"id": "test-model", "efforts": ["high"]}]}):
            with self.assertRaisesRegex(ValueError, "当前不可用"):
                self.panel.create(a, uuid4().hex, "manual", self.when, "new", False, True, True, "test-model", "low")
            self.scheduler.assert_not_called()
            job = self.panel.create(a, uuid4().hex, "manual", self.when, "new", False, True, True, "test-model", "high")["job"]
        self.assertTrue(job["newChat"])
        self.assertEqual(job["delivery"], "app_server")
        self.assertEqual((job["model"], job["effort"]), ("test-model", "high"))
        self.assertEqual(self.panel.jobs(a)["jobs"], [job])
        self.assertEqual(self.panel.jobs(b)["jobs"], [])

    def test_quota_change_requires_confirmation_and_retry_after_commit_uses_original_result(self):
        key = self.bind("a")
        timestamp = int(datetime.fromisoformat(self.when).timestamp())
        quota = {"suggestion": {"epoch": timestamp, "local": self.when}}
        request = uuid4().hex
        with patch.object(self.panel, "quota", return_value=quota) as read:
            result = self.panel.create(key, request, "quota", timestamp - 60, "go", False, False)
            self.assertEqual(result["state"], "quota_changed")
            self.scheduler.assert_not_called()
            args = (key, request, "quota", timestamp, "go", False, False)
            first = self.panel.create(*args)
            self.assertEqual(first["job"]["delivery"], "queue")
            read.return_value = {"suggestion": None}
            self.assertEqual(first, self.panel.create(*args))
            self.scheduler.assert_called_once()
