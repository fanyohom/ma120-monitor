"""名称检索（加关注池时免输代码）的回归测试。

数据源是腾讯 smartbox，返回形如：

    v_hint="sz~000333~\\u7f8e\\u7684\\u96c6\\u56e2~mdjt~GP-A^hk~00300~...~GP"

要看住三件事：
1. 只留 A 股（港股/基金/债券混在同一条返回里，全要滤掉）
2. 名称是 \\uXXXX 转义，必须还原成中文（用 unicode_escape 会把中文打乱）
3. 检索失败要静默返回空列表，不能把加关注池的主流程拖挂
"""
import unittest
from unittest.mock import patch

import web_app


SAMPLE = (
    'v_hint="sz~000333~\\u7f8e\\u7684\\u96c6\\u56e2~mdjt~GP-A'
    '^sz~000527~\\u7f8e\\u7684\\u7535\\u5668~mddq~GP-A'
    '^hk~00300~\\u7f8e\\u7684\\u96c6\\u56e2~mdjt~GP'
    '^sh~600519~\\u8d35\\u5dde\\u8305\\u53f0~gzmt~GP-A'
    '^of~000333~fund~x~JJ"'
)


def fake_response(text):
    response = type("Response", (), {})()
    response.text = text
    response.encoding = "utf-8"
    return response


class LookupTests(unittest.TestCase):
    def setUp(self):
        web_app._lookup_cache.clear()
        self.addCleanup(web_app._lookup_cache.clear)

    def test_keeps_a_shares_only_and_decodes_unicode(self):
        with patch.object(web_app.requests, "get", return_value=fake_response(SAMPLE)):
            items = web_app.lookup_instruments("美的")
        self.assertEqual([item["code"] for item in items], ["000333", "000527", "600519"])
        self.assertEqual(items[0]["name"], "美的集团")
        self.assertEqual(items[2]["name"], "贵州茅台")
        # 港股 00300 与基金 JJ 都该被滤掉
        self.assertTrue(all(len(item["code"]) == 6 and item["code"].isdigit() for item in items))

    def test_result_is_cached(self):
        with patch.object(web_app.requests, "get", return_value=fake_response(SAMPLE)) as get:
            web_app.lookup_instruments("美的")
            web_app.lookup_instruments("美的")
        self.assertEqual(get.call_count, 1, "同一关键字应命中缓存，不该重复请求")

    def test_blank_keyword_never_hits_network(self):
        with patch.object(web_app.requests, "get") as get:
            self.assertEqual(web_app.lookup_instruments(""), [])
            self.assertEqual(web_app.lookup_instruments("   "), [])
            self.assertEqual(web_app.lookup_instruments("x" * 21), [])
        get.assert_not_called()

    def test_network_failure_returns_empty(self):
        with patch.object(web_app.requests, "get", side_effect=web_app.requests.RequestException("boom")):
            self.assertEqual(web_app.lookup_instruments("美的"), [])

    def test_payload_without_hint_returns_empty(self):
        with patch.object(web_app.requests, "get", return_value=fake_response("v_hint=\"\"")):
            self.assertEqual(web_app.lookup_instruments("不存在的股票"), [])

    def test_duplicate_codes_are_collapsed(self):
        duplicated = 'v_hint="sz~000333~a~x~GP-A^sz~000333~b~x~GP-A"'
        with patch.object(web_app.requests, "get", return_value=fake_response(duplicated)):
            items = web_app.lookup_instruments("aaa")
        self.assertEqual(len(items), 1)


if __name__ == "__main__":
    unittest.main()
