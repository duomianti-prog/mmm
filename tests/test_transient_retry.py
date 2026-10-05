"""v1.6.6 瞬时失败自动重试的分类与排期规则(纯逻辑,无浏览器)。"""
import unittest
from datetime import datetime
from types import SimpleNamespace

from app.config import Config
from app.engine.monitor import MonitorEngine


def _engine():
    eng = MonitorEngine.__new__(MonitorEngine)
    eng.cfg = Config()
    return eng


def _row(error, *, retry_count=0, scheduled_at=None):
    return SimpleNamespace(
        retry_count=retry_count, status="failed",
        scheduled_at=scheduled_at, done_at=datetime.utcnow(),
        next_allowed_at=None, error=error,
        blocked_reason=error, blocked_signal="NETWORK",
        blocked_operation="comment", blocked_at=datetime.utcnow())


class TransientFailureClassifyTests(unittest.TestCase):
    def setUp(self):
        self.engine = _engine()

    def test_browser_and_network_glitches_are_transient(self):
        for err in (
                "TimeoutError: locator click timed out after 30000ms",
                "等待选择器超时",
                "Target page, context or browser has been closed",
                "net::ERR_CONNECTION_RESET at https://...",
                "error while loading page: ECONNRESET",
                "代理连接失败,网络异常,请检查翻墙",
                "page crashed; 页面无响应",
                "frame was detached while waiting"):
            self.assertTrue(self.engine._is_transient_failure(err), err)

    def test_business_hard_errors_are_never_retried(self):
        for err in (
                "已找到目标评论,但未找到该评论的回复入口(候选 0 个)",
                "未找到该评论的回复入口",
                "操作频繁,已达每小时上限,请稍后再试",
                "该账号每日评论上限(50)已达",
                "未登录或登录态已失效,请扫码登录",
                "作品不存在或已下架(笔记已删除)",
                "评论输入框未出现:selector_diag group=comment.editor",
                "发送按钮未激活(评论框为空)"):
            self.assertFalse(self.engine._is_transient_failure(err), err)

    def test_backoff_schedule_then_give_up_at_max(self):
        delay = self.engine._transient_retry_delay
        self.assertEqual(delay(0), 300)
        self.assertEqual(delay(1), 900)
        self.assertEqual(delay(2), 2400)
        # retry_count == max:不再排期
        self.assertIsNone(delay(3))

    def test_automatic_transient_failure_is_requed_with_backoff(self):
        row = _row("TargetClosedError: target closed")
        ok = self.engine._schedule_transient_retry(row, row.error, manual=False)
        self.assertTrue(ok)
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.retry_count, 1)
        self.assertIsNotNone(row.next_allowed_at)
        self.assertGreater(row.next_allowed_at, datetime.utcnow())
        self.assertEqual(row.blocked_reason, "")
        self.assertIn("自动重试 1/3", row.error)

    def test_manual_failure_never_auto_retries(self):
        row = _row("navigation failed: net::ERR_TIMED_OUT")
        ok = self.engine._schedule_transient_retry(row, row.error, manual=True)
        self.assertFalse(ok)
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.retry_count, 0)

    def test_retry_stops_after_max_attempts(self):
        row = _row("timeout 超时", retry_count=3)
        ok = self.engine._schedule_transient_retry(row, row.error, manual=False)
        self.assertFalse(ok)
        self.assertEqual(row.status, "failed")

    def test_publish_retry_keeps_user_appointment(self):
        appointment = datetime(2030, 1, 1, 12, 0, 0)
        row = _row("browser has been closed before upload",
                   scheduled_at=appointment)
        ok = self.engine._schedule_transient_retry(
            row, row.error, manual=False, keep_schedule=True)
        self.assertTrue(ok)
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.scheduled_at, appointment)
        self.assertIsNotNone(row.next_allowed_at)


if __name__ == "__main__":
    unittest.main()
