"""创作中心评论抓取的回归测试。

覆盖:
- 2026-09 抖音改版:评论管理页 URL 迁移与旧配置自动迁移;
- v1.3.6 多通道抓取:评论接口 URL 宽松判定、任意信封的评论递归挖掘
  (改版常只改外层包裹)、item_id 归因兜底、零抓取证据消息(必须保持
  BUSINESS 分类,不能误伤健康账号)。
"""
import os
import tempfile
import unittest

from app.browser.fetcher import (
    _attach_fallback_aweme,
    _aweme_fallback_from_body,
    _aweme_fallback_from_url,
    _creator_missing_aweme_error,
    _creator_zero_capture_error,
    _dig_comment_list,
    _find_aweme_id_anywhere,
    _is_comment_api_url,
    _is_creator_comment_list_url,
    _looks_like_comment,
    _walk_comment_items,
)
from app.config import (
    CREATOR_COMMENT_URL,
    _CREATOR_COMMENT_URL_LEGACY,
    load_config,
)
from app.platforms.douyin.extract import parse_creator_comment
from app.risk import RiskCategory, classify_platform_error


def _write_config(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class CreatorCommentUrlConfigTests(unittest.TestCase):
    def test_default_is_new_url(self):
        path = _write_config("server:\n  port: 8000\n")
        cfg = load_config(path)
        os.unlink(path)
        self.assertEqual(cfg.engine.creator_comment_url, CREATOR_COMMENT_URL)

    def test_legacy_url_migrated(self):
        path = _write_config(
            "engine:\n"
            f"  creator_comment_url: {_CREATOR_COMMENT_URL_LEGACY}\n")
        cfg = load_config(path)
        os.unlink(path)
        self.assertEqual(cfg.engine.creator_comment_url, CREATOR_COMMENT_URL)

    def test_legacy_url_with_trailing_slash_migrated(self):
        path = _write_config(
            "engine:\n"
            f"  creator_comment_url: {_CREATOR_COMMENT_URL_LEGACY}/\n")
        cfg = load_config(path)
        os.unlink(path)
        self.assertEqual(cfg.engine.creator_comment_url, CREATOR_COMMENT_URL)

    def test_custom_url_preserved(self):
        path = _write_config(
            "engine:\n  creator_comment_url: https://example.com/my-comment-page\n")
        cfg = load_config(path)
        os.unlink(path)
        self.assertEqual(cfg.engine.creator_comment_url,
                         "https://example.com/my-comment-page")


class CommentApiUrlTests(unittest.TestCase):
    def test_new_endpoint_matches(self):
        self.assertTrue(_is_comment_api_url(
            "https://creator.douyin.com/aweme/v1/creator/comment/list/"
            "?item_id=7345000000000000000&count=10"))

    def test_audit_endpoint_matches(self):
        self.assertTrue(_is_comment_api_url(
            "https://creator.douyin.com/aweme/v1/creator/comment/audit/list/?count=10"))

    def test_unknown_future_endpoint_still_matches(self):
        """改版后哪怕端点改名(不含 list),只要在 creator 域 comment 路径下就命中。"""
        self.assertTrue(_is_comment_api_url(
            "https://creator.douyin.com/aweme/v1/creator/comment/search-v2/?count=10"))

    def test_web_comment_list_matches(self):
        self.assertTrue(_is_comment_api_url(
            "https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=1"))

    def test_page_document_url_matches_but_callers_filter_resource_type(self):
        # 页面文档本身路径含 comment;hook 只抓 fetch/XHR 不会碰到,
        # response 通道由调用方按 resource_type 排除。
        self.assertTrue(_is_comment_api_url(
            "https://creator.douyin.com/creator-micro/data/following/comment"))

    def test_unrelated_url_not_matched(self):
        self.assertFalse(_is_comment_api_url("https://www.douyin.com/"))
        self.assertFalse(_is_comment_api_url(
            "https://creator.douyin.com/aweme/v1/creator/data/overview/"))

    def test_old_matcher_still_works(self):
        self.assertTrue(_is_creator_comment_list_url(
            "https://creator.douyin.com/aweme/v1/creator/comment/list/?x=1"))


class CommentShapeTests(unittest.TestCase):
    def test_looks_like_comment(self):
        self.assertTrue(_looks_like_comment({"cid": "1", "text": "好"}))
        self.assertTrue(_looks_like_comment(
            {"comment_id": "2", "user": {"nickname": "x"}}))
        self.assertFalse(_looks_like_comment({"uid": "1", "nickname": "x"}))
        self.assertFalse(_looks_like_comment({"cid": ["1"], "text": "x"}))
        self.assertFalse(_looks_like_comment("nope"))

    def test_walk_top_level_comments(self):
        out = []
        _walk_comment_items({"comments": [{"cid": "1", "text": "a"}]}, out)
        self.assertEqual([c["cid"] for c in out], ["1"])

    def test_walk_nested_envelope(self):
        """新版/未知信封:评论藏在多层 data 下也能挖出。"""
        data = {"data": {"module_list": [
            {"data": {"comment_list_v2": [
                {"cid": "1", "text": "a"}, {"cid": "2", "text": "b"}]}}]}}
        out = []
        _walk_comment_items(data, out)
        self.assertEqual(sorted(c["cid"] for c in out), ["1", "2"])

    def test_walk_replies(self):
        data = {"comments": [{
            "cid": "1", "text": "主评论",
            "reply_comment": {"cid": "1-1", "text": "回复"},
        }]}
        out = []
        _walk_comment_items(data, out)
        self.assertEqual({c["cid"] for c in out}, {"1", "1-1"})

    def test_walk_reject_plain_list(self):
        out = []
        _walk_comment_items([1, 2, 3], out)
        self.assertEqual(out, [])

    def test_walk_mixed_list_threshold(self):
        # 列表中只有个别元素带 cid(如混入广告卡),达不到 1/3 阈值不下钻为评论,
        # 但仍会递归这些元素本身(避免漏掉嵌套数据)。
        data = {"items": [{"cid": "1", "text": "x"}, {"uid": "2"}, {"uid": "3"}]}
        out = []
        _walk_comment_items(data, out)
        self.assertEqual([c["cid"] for c in out], ["1"])

    def test_dig_comment_list_compat(self):
        self.assertEqual(_dig_comment_list({"comments": [{"cid": "1"}]}), [{"cid": "1"}])
        self.assertEqual(_dig_comment_list(None), [])


class FallbackAwemeTests(unittest.TestCase):
    def test_fallback_helper(self):
        self.assertEqual(
            _aweme_fallback_from_url(
                "https://creator.douyin.com/aweme/v1/creator/comment/list/"
                "?item_id=7345000000000000000&count=10"),
            "7345000000000000000")
        self.assertEqual(_aweme_fallback_from_url(
            "https://x/?item_id=abc"), "")
        self.assertEqual(_aweme_fallback_from_url("https://x/?count=10"), "")

    def test_fallback_url_camelcase_and_late_param(self):
        """camelCase 参数名、参数位置靠后(超出旧截断)也能取到。"""
        self.assertEqual(
            _aweme_fallback_from_url("https://x/?itemId=7345000000000000000"),
            "7345000000000000000")
        long_prefix = "a=1&" * 60
        self.assertEqual(
            _aweme_fallback_from_url(
                f"https://x/?{long_prefix}item_id=7345000000000000000"),
            "7345000000000000000")

    def test_fallback_from_request_body(self):
        """POST 接口 item_id 在请求体(JSON 或表单)而非 query。"""
        self.assertEqual(
            _aweme_fallback_from_body('{"item_id":"7345000000000000000","cursor":0}'),
            "7345000000000000000")
        self.assertEqual(
            _aweme_fallback_from_body("count=10&item_id=7345000000000000000"),
            "7345000000000000000")
        self.assertEqual(
            _aweme_fallback_from_body('{"itemId": 7345000000000000000}'),
            "7345000000000000000")
        self.assertEqual(_aweme_fallback_from_body('{"cursor":0}'), "")
        self.assertEqual(_aweme_fallback_from_body(""), "")

    def test_missing_aweme_filled_from_item_id(self):
        comments = [{"cid": "c1", "text": "hi"}]
        _attach_fallback_aweme(
            comments,
            "https://creator.douyin.com/aweme/v1/creator/comment/list/"
            "?item_id=7345000000000000000&count=10")
        parsed = parse_creator_comment(comments[0])
        self.assertEqual(parsed["aweme_id"], "7345000000000000000")

    def test_camelcase_comment_key_normalized(self):
        """评论自带 camelCase 作品id 时归一为 aweme_id,下游解析器可见。"""
        comments = [{"cid": "c1", "text": "hi", "itemId": "7345000000000000000"}]
        _attach_fallback_aweme(comments, "https://x/comment/list/")
        self.assertEqual(comments[0]["aweme_id"], "7345000000000000000")
        parsed = parse_creator_comment(comments[0])
        self.assertEqual(parsed["aweme_id"], "7345000000000000000")

    def test_missing_aweme_filled_from_request_body(self):
        comments = [{"cid": "c1", "text": "hi"}]
        _attach_fallback_aweme(
            comments, "https://creator.douyin.com/aweme/v1/creator/comment/list/",
            "", '{"item_id":"7345000000000000000"}')
        self.assertEqual(comments[0]["aweme_id"], "7345000000000000000")

    def test_missing_aweme_filled_from_context(self):
        """逐作品选择后紧随的响应,用当前选中作品做时间相关性归因。"""
        comments = [{"cid": "c1", "text": "hi"}]
        _attach_fallback_aweme(comments, "https://x/comment/list/", "", "",
                               "7345000000000000000")
        self.assertEqual(comments[0]["aweme_id"], "7345000000000000000")

    def test_existing_aweme_not_overwritten(self):
        comments = [{"cid": "c1", "aweme_id": "111"}]
        _attach_fallback_aweme(
            comments,
            "https://creator.douyin.com/aweme/v1/creator/comment/list/?item_id=2222222222")
        self.assertEqual(comments[0]["aweme_id"], "111")

    def test_encoded_token_not_used_as_aweme(self):
        comments = [{"cid": "c1"}]
        _attach_fallback_aweme(
            comments,
            "https://creator.douyin.com/aweme/v1/creator/comment/list/?item_id=abcDEF123")
        self.assertNotIn("aweme_id", comments[0])

    def test_envelope_aweme_id_used_when_query_token_encoded(self):
        """query 的 item_id 是编码 token 时,从响应信封的作品信息键取作品id。"""
        comments = [{"cid": "c1", "text": "hi"}]
        envelope = {"item": {"aweme_id": "7345000000000000000", "desc": "作品"},
                    "comment_list": comments}
        aid = _find_aweme_id_anywhere(envelope)
        self.assertEqual(aid, "7345000000000000000")
        _attach_fallback_aweme(
            comments,
            "https://creator.douyin.com/aweme/v1/creator/comment/list/?item_id=encToken",
            aid)
        self.assertEqual(comments[0]["aweme_id"], "7345000000000000000")

    def test_envelope_container_id_found(self):
        """信封里 item/aweme 容器只有 id 键(无 item_id)时也能取到作品id。"""
        self.assertEqual(
            _find_aweme_id_anywhere(
                {"data": {"item": {"id": "7345000000000000000"},
                          "comments": []}}),
            "7345000000000000000")
        self.assertEqual(
            _find_aweme_id_anywhere(
                {"aweme": {"itemId": "7345000000000000000"}}),
            "7345000000000000000")

    def test_find_aweme_id_never_mistakes_cid(self):
        """cid 也是长数字,但键名不是作品id 键,绝不能被当成作品id。"""
        self.assertEqual(_find_aweme_id_anywhere(
            {"comments": [{"cid": "7345000000000000999", "text": "x"}]}), "")
        self.assertEqual(_find_aweme_id_anywhere({"group_id": "123"}), "")

    def test_parse_creator_comment_camelcase(self):
        parsed = parse_creator_comment(
            {"cid": "c1", "text": "hi", "awemeId": "7345000000000000000"})
        self.assertEqual(parsed["aweme_id"], "7345000000000000000")


class CreatorZeroCaptureErrorTests(unittest.TestCase):
    def test_no_observed_requests(self):
        msg = _creator_zero_capture_error(
            "https://creator.douyin.com/creator-micro/data/following/comment",
            [], 0, 0, 12, False, 0, 0)
        self.assertIn("未发现评论相关数据请求", msg)
        self.assertIn("12", msg)

    def test_observed_but_no_bodies(self):
        msg = _creator_zero_capture_error(
            "https://x", ["/aweme/v1/creator/comment/search/"], 0, 0, 5, False, 0, 0)
        self.assertIn("creator/comment/search", msg)
        self.assertIn("未取得响应内容", msg)

    def test_bodies_without_comment_structure(self):
        msg = _creator_zero_capture_error(
            "https://x", ["/aweme/v1/creator/comment/list/"], 4, 0, 8, False, 0, 4)
        self.assertIn("4 个响应均无评论结构", msg)

    def test_bodies_parsed_but_no_valid_cid(self):
        msg = _creator_zero_capture_error(
            "https://x", ["/aweme/v1/creator/comment/list/"], 3, 2, 8, True, 5, 2)
        self.assertIn("接口命中 2 次", msg)
        self.assertIn("5 个", msg)

    def test_missing_aweme_error_carries_evidence(self):
        """缺作品id 错误必须带评论字段样例与请求路径——远程排障第一手证据。"""
        collected = {"c1": {"cid": "c1", "text": "hi", "digg_count": 3}}
        msg = _creator_missing_aweme_error(
            collected, ["/aweme/v1/creator/comment/list/"],
            ["/aweme/v1/creator/comment/list/", "/aweme/v1/creator/data/overview/"])
        self.assertIn("拦截到 1 条评论", msg)
        self.assertIn("digg_count", msg)
        self.assertIn("creator/comment/list", msg)
        self.assertIn("data/overview", msg)

    def test_all_messages_stay_business_classified(self):
        """证据消息绝不能被误判为 AUTH/RISK/NETWORK 而惩罚健康账号。"""
        cases = [
            _creator_zero_capture_error("https://creator.douyin.com/a", [], 0, 0, 0,
                                        False, 0, 0),
            _creator_zero_capture_error("https://creator.douyin.com/a",
                                        ["/aweme/v1/creator/comment/list/"], 0, 0, 0,
                                        False, 0, 0),
            _creator_zero_capture_error("https://creator.douyin.com/a",
                                        ["/aweme/v1/creator/comment/list/"], 4, 0, 0,
                                        True, 2, 4),
            _creator_zero_capture_error("https://creator.douyin.com/a",
                                        ["/aweme/v1/creator/comment/list/"], 3, 2, 0,
                                        True, 5, 2),
            _creator_missing_aweme_error(
                {"c1": {"cid": "c1", "text": "x"}},
                ["/aweme/v1/creator/comment/list/"],
                ["/aweme/v1/creator/comment/list/"]),
        ]
        for msg in cases:
            category, _signal = classify_platform_error(msg)
            self.assertEqual(category, RiskCategory.BUSINESS, msg)


if __name__ == "__main__":
    unittest.main()
