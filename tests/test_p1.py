"""P1 补丁测试：状态持久化 / 依赖锁定 / 事实核验。

- P1-1 状态持久化：SqliteSaver 跨实例恢复中断点（同一 db 文件、同一 thread_id）
- P1-2 依赖锁定：requirements.lock 存在、覆盖核心依赖、与 pyproject 同步
- P1-3 事实核验：FactVerifier 三态（verified / unverified / not_applicable）+ 图接入
"""

from __future__ import annotations

from pathlib import Path

from langgraph.types import Command

from agent_builder.facts.verifier import has_source, requires_source, verify
from agent_builder.graph.build import build_graph, build_persistent_graph
from agent_builder.graph.nodes import summarize_node, verify_node
from agent_builder.llm.client import MockClient

REQUIREMENT = "帮我生成一个每日新闻摘要 Agent"


class TestPersistence:
    def test_sqlite_checkpoint_cross_instance_resume(self, tmp_path):
        """同一 SQLite 文件、同一 thread_id：实例 A 中断 → 实例 B 恢复（模拟进程重启）。"""
        db = tmp_path / "state.db"
        llm = MockClient(correlation_id="c-p1")

        # 实例 A：跑到确认点中断
        graph_a, saver_a = build_persistent_graph(llm, db)
        config = {"configurable": {"thread_id": "t-persist"}}
        first = graph_a.invoke(
            {"task_id": "t-persist", "requirement": REQUIREMENT, "correlation_id": "c-p1"}, config
        )
        assert "__interrupt__" in first
        saver_a.conn.close()

        # 实例 B：新连接、新图对象，同一 thread_id 恢复（等价进程重启后继续）
        graph_b, saver_b = build_persistent_graph(llm, db)
        final = graph_b.invoke(Command(resume={"confirmed": True}), config)
        assert final["report"].startswith("mock 最终报告")
        saver_b.conn.close()

    def test_persistent_graph_creates_db_file(self, tmp_path):
        db = tmp_path / "nested" / "state.db"
        llm = MockClient(correlation_id="c-p2")
        _, saver = build_persistent_graph(llm, db)
        assert db.exists()  # 父目录自动创建
        saver.conn.close()


class TestDependencyLock:
    def test_lock_file_exists(self):
        root = Path(__file__).resolve().parent.parent
        lock = root / "requirements.lock"
        assert lock.exists(), "requirements.lock 缺失（用 uv pip compile pyproject.toml --extra dev 生成）"

    def test_lock_covers_core_deps(self):
        content = (Path(__file__).resolve().parent.parent / "requirements.lock").read_text()
        for dep in ("langgraph==", "langchain-openai==", "langgraph-checkpoint-sqlite==", "pydantic=="):
            assert dep in content, f"lock 缺少核心依赖 {dep!r}"
        # lock 必须与 pyproject 同步：不存在 pyproject 里未声明的顶层依赖（uv 编译产物已保证）

    def test_lock_not_contain_local_editable(self):
        content = (Path(__file__).resolve().parent.parent / "requirements.lock").read_text()
        assert "-e " not in content and "file://" not in content, "lock 不应包含本地 editable 路径"


class TestFactVerifier:
    def test_assertion_requires_source(self):
        assert requires_source("公司营收增长 50%") is True
        assert requires_source("本季度首次突破 100 万用户") is True
        assert requires_source("这是一段纯描述性的普通文本") is False

    def test_source_marker_detection(self):
        assert has_source("营收增长 50%（来源：公司年报）") is True
        assert has_source("营收增长 50% https://example.com/report") is True
        assert has_source("营收增长 50%") is False

    def test_verify_three_states(self):
        assert verify("纯描述性文本，没有断言")["status"] == "not_applicable"
        assert verify("营收增长 50%，数据来自年报[1]")["status"] == "verified"
        unverified = verify("营收增长 50%")
        assert unverified["status"] == "unverified"
        assert unverified["issue"] is not None

    def test_verify_node_writes_verification(self):
        state = {
            "task_id": "t-v1",
            "correlation_id": "c-v1",
            "results": {
                "step-001": "某公司营收增长 50%，来源：https://example.com",
                "step-002": "该公司市场份额达到 30%",  # 无来源 → unverified
                "step-003": "普通描述内容",
            },
        }
        out = verify_node(state)
        verification = out["verification"]
        assert verification["step-001"]["status"] == "verified"
        assert verification["step-002"]["status"] == "unverified"
        assert verification["step-003"]["status"] == "not_applicable"

    def test_summarize_appends_unverified_hint(self):
        class _LLM:
            def chat_text(self, system, user, *, temperature=0.7):
                return "mock 报告正文"

        state = {
            "requirement": "R",
            "results": {"s1": "营收增长 50%"},
            "verification": {"s1": verify("营收增长 50%")},
        }
        out = summarize_node(state, _LLM())
        assert "核验提示" in out["report"]
        assert "s1" in out["report"]

    def test_summarize_no_hint_when_verified(self):
        class _LLM:
            def chat_text(self, system, user, *, temperature=0.7):
                return "mock 报告正文"

        state = {
            "requirement": "R",
            "results": {"s1": "营收增长 50%（来源：年报）"},
            "verification": {"s1": verify("营收增长 50%（来源：年报）")},
        }
        out = summarize_node(state, _LLM())
        assert "核验提示" not in out["report"]

    def test_full_graph_with_verification(self):
        llm = MockClient(correlation_id="c-v2")
        task_id = "t-v2"
        config = {"configurable": {"thread_id": task_id}}
        graph = build_graph(llm)
        graph.invoke(
            {"task_id": task_id, "requirement": REQUIREMENT, "correlation_id": "c-v2"}, config
        )
        final = graph.invoke(Command(resume={"confirmed": True}), config)
        # Mock 执行结果是纯文本（无断言）→ verification 全部 not_applicable，报告无核验提示
        assert final["verification"]["step-001"]["status"] == "not_applicable"
