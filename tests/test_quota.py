import unittest

from codex_client import AppServer, CodexError, CodexQuota, Connection, QuotaWindow


class QuotaTests(unittest.TestCase):
    def test_standard_bucket_and_explicit_window_durations(self):
        standard = {
            "primary": {"windowDurationMins": 10080, "usedPercent": 48, "resetsAt": 10000},
            "secondary": {"windowDurationMins": 300, "usedPercent": 25, "resetsAt": 800},
        }
        snapshot = CodexQuota.from_response({
            "rateLimits": {"limitId": "codex_other"},
            "rateLimitsByLimitId": {"codex_other": {"primary": standard["primary"]}, "codex": standard},
        })
        self.assertEqual(snapshot.weekly, QuotaWindow(48, 10000))
        self.assertEqual(snapshot.resume_time(100), 860)

    def test_only_weekly_exhaustion_uses_weekly_reset(self):
        quota = CodexQuota(QuotaWindow(34, 300), QuotaWindow(100, 900))
        self.assertEqual(quota.resume_time(100), 960)

    def test_only_five_hour_exhaustion_uses_five_hour_reset(self):
        quota = CodexQuota(QuotaWindow(100, 300), QuotaWindow(34, 900))
        self.assertEqual(quota.resume_time(100), 360)

    def test_missing_reset_for_exhausted_window_and_stale_values_are_rejected(self):
        with self.assertRaises(CodexError):
            CodexQuota(QuotaWindow(100, 300), QuotaWindow(100, None)).resume_time(100)
        with self.assertRaises(CodexError):
            CodexQuota(QuotaWindow(100, 300), QuotaWindow(100, 500)).resume_time(500)
        self.assertEqual(CodexQuota(QuotaWindow(100, 300), QuotaWindow(100, 500)).resume_time(400), 560)

    def test_other_limit_buckets_and_unspecified_restrictions_do_not_provide_preset(self):
        alternate = {"limitId": "codex_other", "primary": {"windowDurationMins": 300, "usedPercent": 10, "resetsAt": 300}}
        with self.assertRaises(CodexError):
            CodexQuota.from_response({"rateLimits": alternate}).resume_time(100)
        with self.assertRaises(CodexError):
            CodexQuota(QuotaWindow(10, 300), None, ordinary_usage_allowed=False).resume_time(100)

    def test_usage_error_notification_is_preserved_when_the_turn_has_no_error(self):
        server = AppServer(Connection("unused"))
        packets = [
            ("error", {"error": {"codexErrorInfo": "usageLimitExceeded"}, "willRetry": False}),
            ("turn/completed", {"turn": {"id": "turn-1", "status": "failed"}}),
        ]
        for method, params in packets:
            server.messages.put({"method": method, "params": {"threadId": "thread-1", "turnId": "turn-1", **params}})
        turn = server.wait_turn("thread-1", "turn-1", timeout=1)
        self.assertEqual(turn["error"]["codexErrorInfo"], "usageLimitExceeded")

    def test_terminal_usage_error_survives_a_generic_turn_error(self):
        server = AppServer(Connection("unused"))
        packets = [
            ("error", {"error": {"codexErrorInfo": "usageLimitExceeded"}, "willRetry": False}),
            ("error", {"error": {"codexErrorInfo": "other"}, "willRetry": False}),
            ("turn/completed", {"turn": {"id": "turn-1", "status": "completed", "error": {"codexErrorInfo": "other"}}}),
        ]
        for method, params in packets:
            server.messages.put({"method": method, "params": {"threadId": "thread-1", "turnId": "turn-1", **params}})
        turn = server.wait_turn("thread-1", "turn-1", timeout=1)
        self.assertEqual(turn["error"]["codexErrorInfo"], "usageLimitExceeded")
