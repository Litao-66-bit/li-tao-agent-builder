"""P2 合规文档测试：NOTICE / SECURITY.md / PRIVACY.md 存在性与关键内容。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestNotice:
    def test_notice_exists(self):
        assert (ROOT / "NOTICE").exists()

    def test_notice_declares_mpl_and_deps(self):
        content = (ROOT / "NOTICE").read_text(encoding="utf-8")
        assert "Mozilla Public License 2.0" in content or "MPL-2.0" in content
        # 许可证声明必须覆盖核心依赖
        for dep in ("langgraph", "langchain-openai", "openai", "pydantic", "python-dotenv", "ruff"):
            assert dep in content, f"NOTICE 缺少依赖 {dep}"
        # 许可证名称必须出现
        for lic in ("MIT", "Apache-2.0", "BSD-3-Clause"):
            assert lic in content, f"NOTICE 缺少许可证 {lic}"
        # 与 lock 的版本一致性：NOTICE 引用的版本应来自 requirements.lock
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        assert "langgraph==1.2.12" in lock


class TestSecurity:
    def test_security_md_exists(self):
        assert (ROOT / "SECURITY.md").exists()

    def test_reporting_channel_and_policy(self):
        content = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        assert "Security Advisory" in content or "私密" in content
        assert ("请勿" in content or "不要" in content) and "公开" in content  # 禁止公开披露
        assert "7 天" in content or "7天内" in content  # 响应承诺
        # 安全基线要点覆盖
        for key in ("密钥", "沙箱", "审批", "E_COST", "超时", "注入"):
            assert key in content, f"SECURITY.md 缺少安全基线项 {key}"


class TestPrivacy:
    def test_privacy_md_exists(self):
        assert (ROOT / "PRIVACY.md").exists()

    def test_core_privacy_claims(self):
        content = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
        # 核心承诺：默认不收集/不上传/不遥测
        assert "不收集" in content
        assert "遥测" in content
        # 唯一外发点：模型 API
        assert "DeepSeek" in content
        assert "HTTPS" in content or "https" in content.lower()
        # 用户权利与删除方式
        assert "删除" in content
        assert "--no-persist" in content or "no-persist" in content
        # 合规提示
        assert "个人信息保护法" in content or "GDPR" in content
