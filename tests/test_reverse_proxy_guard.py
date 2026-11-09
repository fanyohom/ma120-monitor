"""反向代理组合下的 Host / Origin 校验回归测试。

生产拓扑：浏览器 → nginx(:8080，改写 Host/Origin 为 127.0.0.1:8765) → 应用。
nginx 少改写任一个头，都会让 GET 或写操作全站 403。
上 HTTPS 后还要靠 MA120_TRUSTED_ORIGIN 放行 https Origin。
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web_app import DashboardHandler, allowed_request_host, trusted_origins


def mutation_allowed(host, origin):
    """用最小桩直接跑 handler 的判定逻辑，不启 HTTP 服务。"""
    handler = SimpleNamespace(
        headers={"Host": host, **({"Origin": origin} if origin is not None else {})},
        server=SimpleNamespace(server_port=8765),
        _json=lambda code, payload: None,
    )
    return DashboardHandler._mutation_allowed(handler)


class ReverseProxyGuardTests(unittest.TestCase):
    def test_host_must_match_server_port(self):
        self.assertTrue(allowed_request_host("127.0.0.1:8765", 8765))
        self.assertTrue(allowed_request_host("localhost:8765", 8765))
        self.assertFalse(allowed_request_host("127.0.0.1:18765", 8765))
        self.assertFalse(allowed_request_host("159.75.67.46:8080", 8765))

    def test_matrix(self):
        cases = [
            # host, origin, expected
            ("127.0.0.1:8765", "http://127.0.0.1:8765", True),
            ("127.0.0.1:8765", None, True),                       # curl / 无 Origin
            ("127.0.0.1:8765", "http://evil.example", False),     # 跨站
            ("127.0.0.1:18765", "http://127.0.0.1:18765", False),  # 隧道端口错
            ("159.75.67.46:8080", "http://159.75.67.46:8080", False),  # nginx 没改写 Host
        ]
        for host, origin, expected in cases:
            with self.subTest(host=host, origin=origin):
                self.assertEqual(mutation_allowed(host, origin), expected)

    def test_default_trusted_origin_keeps_legacy_behavior(self):
        with patch.dict(os.environ, {"MA120_TRUSTED_ORIGIN": ""}):
            self.assertEqual(trusted_origins("127.0.0.1:8765"),
                             {"http://127.0.0.1:8765"})

    def test_https_origin_via_environment(self):
        with patch.dict(os.environ, {"MA120_TRUSTED_ORIGIN": "https://ma120.example"}):
            self.assertEqual(trusted_origins("127.0.0.1:8765"),
                             {"https://ma120.example"})
            self.assertTrue(mutation_allowed("127.0.0.1:8765", "https://ma120.example"))
            self.assertFalse(mutation_allowed("127.0.0.1:8765", "http://127.0.0.1:8765"))
            self.assertFalse(mutation_allowed("127.0.0.1:8765", "https://evil.example"))

    def test_multiple_trusted_origins(self):
        with patch.dict(os.environ, {"MA120_TRUSTED_ORIGIN":
                                    "https://a.example, https://b.example"}):
            self.assertEqual(trusted_origins("127.0.0.1:8765"),
                             {"https://a.example", "https://b.example"})


if __name__ == "__main__":
    unittest.main()
