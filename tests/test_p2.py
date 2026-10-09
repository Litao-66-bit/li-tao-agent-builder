"""P2 合规文档测试：NOTICE / SECURITY.md / PRIVACY.md 存在性与关键内容。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestNotice:
    def test_notice_exists(self):
        assert (ROOT / "NOTICE").exists()

    def test_notice_declares_mpl_and_deps(self):
        content = (ROOT / "NOTICE").read_text(encoding="utf-8")
        assert "Mozilla Public License 2.0" in content or "MPL-2.0" in content
        # 许可证声明必须覆盖核心依赖
        for dep in ("langchain-openai", "openai", "pydantic", "python-dotenv", "ruff"):
            assert dep in content, f"NOTICE 缺少依赖 {dep}"
        # 许可证名称必须出现
        for lic in ("MIT", "Apache-2.0", "BSD-3-Clause"):
            assert lic in content, f"NOTICE 缺少许可证 {lic}"
        # 与 lock 的一致性：pyproject 的直接依赖都应钉在 requirements.lock 里；已移除的编排框架
        # （langgraph）不得再被锁定。实测：只改 pyproject 而忘改 lock 时，这条断言会把不一致挡在 CI 前。
        lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        for dep in ("langchain-openai", "pydantic", "fastapi", "uvicorn", "python-dotenv"):
            assert f"{dep}==" in lock, f"requirements.lock 缺少直接依赖 {dep}"
        # 只允许注释里出现历史提及（如 `# via langgraph`），不允许再**锁定** langgraph 包。
        assert "\nlanggraph" not in "\n" + lock, "requirements.lock 仍在锁定 langgraph"


class TestOutputRootPolicy:
    """产物输出根目录策略：**新**交付物落 `outputs/`，且不被 lint / 测试扫描。

    动机（实测）：工作区常常就是本项目仓库根，agent 写出的 `paper_agent.py` 之类直接躺在根目录，
    本地 `ruff check .` 会被它扫到并报错（RUF022），也容易被 pytest 收集。
    策略本身 = 一条约定（写进决策提示词）+ 两处**确定性扫描范围**，这里把后者钉住，
    免得日后被无意改回去。
    """

    def test_常量与约定的目录名一致(self):
        from agent_builder.tools.gatekeeper import OUTPUT_ROOT_DIRNAME

        assert OUTPUT_ROOT_DIRNAME == "outputs"

    def test_output_root_会解析到工作区下并建好目录(self, tmp_path):
        from agent_builder.tools.gatekeeper import current_workspace_dir, output_root

        token = current_workspace_dir.set(tmp_path)
        try:
            target = output_root()
        finally:
            current_workspace_dir.reset(token)

        assert target == tmp_path / "outputs"
        assert target.is_dir()

    def test_ruff_排除产物目录(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r"exclude = \[(.*?)\]", text, re.DOTALL)
        assert match is not None, "pyproject.toml 里找不到 [tool.ruff] exclude"
        assert "outputs" in match.group(1), "ruff 未排除产物目录 outputs"

    def test_pytest_只收集_tests(self):
        """根目录下的 agent 产物（如 test_paper_agent.py）不该被 CI 收集。"""
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r"testpaths = \[(.*?)\]", text, re.DOTALL)
        assert match is not None, "pyproject.toml 里找不到 testpaths"
        assert match.group(1).strip() == '"tests"'

    def test_gitignore_忽略产物目录(self):
        assert "outputs/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


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
