"""pytest 全局环境:测试与授权无关,统一跳过本机授权门控。

开发机上的 license.dat 可能绑定其他机器(machine_mismatch),
不设置该变量时 LicenseGateMiddleware 会让全部 /api 用例得到 403。
"""
import os


def pytest_configure(config):
    os.environ["CREATORHUB_SKIP_LICENSE"] = "1"
