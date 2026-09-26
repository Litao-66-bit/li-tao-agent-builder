"""冒烟测试：验证包可导入、版本号存在。

该测试不调用任何模型、不需要 API Key，CI 与本地均可离线跑通。
"""

import agent_builder


def test_package_importable():
    assert agent_builder is not None


def test_version_exists():
    assert agent_builder.__version__ == "0.1.0"
