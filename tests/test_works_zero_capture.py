"""抖音主页零抓取错误的证据化消息与分类回归测试。

背景:试跑提示「发现 0 个目标,生成 0 条(可能都已生成过)」时,真实原因
(登录墙/空态/接口未发出)曾被 BUSINESS 分类静默吞掉,前端完全不可见。
"""
import unittest

from app.browser.fetcher import _works_zero_capture_error
from app.risk import RiskCategory, classify_platform_error


class WorksZeroCaptureErrorTests(unittest.TestCase):
    def test_login_wall_mentions_relogin(self):
        msg = _works_zero_capture_error(
            "https://www.douyin.com/user/abc", {"login_wall": True, "body_len": 300},
            [], [])
        self.assertIn("登录提示墙", msg)
        self.assertIn("重新扫码登录", msg)

    def test_redirect_to_passport_detected(self):
        msg = _works_zero_capture_error(
            "https://passport.douyin.com/login?next=/user/abc", {}, [], [])
        self.assertIn("重定向到登录页", msg)

    def test_empty_state_surfaced(self):
        msg = _works_zero_capture_error(
            "https://www.douyin.com/user/abc", {"empty": True}, [], [])
        self.assertIn("暂无作品", msg)

    def test_post_hits_without_data(self):
        msg = _works_zero_capture_error(
            "https://www.douyin.com/user/abc", {}, ["200 status_code=0"], [])
        self.assertIn("1 次", msg)
        self.assertIn("没有返回作品数据", msg)

    def test_no_api_seen(self):
        msg = _works_zero_capture_error(
            "https://www.douyin.com/user/abc", {}, [], ["200 /aweme/v1/web/im"])
        self.assertIn("没有作品列表接口", msg)

    def test_neither_hits_nor_api(self):
        msg = _works_zero_capture_error("https://www.douyin.com/user/abc", {}, [], [])
        self.assertIn("没有发出任何可观测的接口请求", msg)

    def test_all_messages_stay_business_classified(self):
        """证据消息绝不能被误判为 AUTH/RISK/NETWORK 而惩罚健康账号。"""
        cases = [
            _works_zero_capture_error("https://www.douyin.com/user/abc",
                                      {"login_wall": True}, [], []),
            _works_zero_capture_error("https://passport.douyin.com/login", {}, [], []),
            _works_zero_capture_error("https://www.douyin.com/user/abc",
                                      {"empty": True, "risk": True}, [], []),
            _works_zero_capture_error("https://www.douyin.com/user/abc", {},
                                      ["200 x"] * 3, []),
            _works_zero_capture_error("https://www.douyin.com/user/abc", {}, [], []),
        ]
        for msg in cases:
            category, _signal = classify_platform_error(msg)
            self.assertEqual(category, RiskCategory.BUSINESS, msg)


if __name__ == "__main__":
    unittest.main()
