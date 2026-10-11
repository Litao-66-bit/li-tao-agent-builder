"""卡 48 取证后的针对性修复：工具链可用性（`code_search` / `file_list` / `file_edit`）。

本文件守的是**实测发现的三个具体缺陷**，不是泛泛的功能测试：

1. `file_edit`：模型把**带行号的输出**（`file_read` 带行号视图 / `code_search` 的
   `file:line:content`）当 `old_string` 抄过来时，必然失败且不可重试 → 白烧一轮。
   现在做**严格校验**的行号剥除；剥除必须无损（不能把 `2: ` 写进文件），
   也**不能误伤普通代码**（实测宽松版会把 `{'a': 1,` 剥成 `X`）。
2. `file_edit`：失败信息只写"找不到"，模型看不出差在哪 → 原样重发 → 撞空转闸门。
   现在给出**最接近的一行 + 相似度**。
3. `file_list` / `code_search`：大输出的关键信息（共多少条目 / 命中多少条）写在末尾，
   会被观察窗口（只保头部 2000 字）**跟着尾巴一起丢掉** → 现在放第一行；
   目录还带**条目数**，模型能判断值不值得下钻。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.tools.impl.code_search import _count_header, search_code
from agent_builder.tools.impl.file_edit import edit_file
from agent_builder.tools.impl.file_list import list_dir

BASELINE = "def greet(name):\n    msg = 'hi'\n    return msg\n"


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = tmp_path / "t.py"
    path.write_text(BASELINE, encoding="utf-8", newline="")
    return path


class TestLineNumberStripping:
    def test_单行带行号可以改(self, target: Path) -> None:
        """`2:     msg = 'hi'` → 等价于 `    msg = 'hi'`。"""
        out = edit_file(str(target), "2:     msg = 'hi'", "2:     msg = 'hello'")

        assert "替换 1 处" in out
        # 关键：**不能**把 "2: " 写进文件（第一版修 bug 时踩到过）
        assert target.read_text(encoding="utf-8") == (
            "def greet(name):\n    msg = 'hello'\n    return msg\n"
        )

    def test_多行都带行号可以改(self, target: Path) -> None:
        out = edit_file(
            str(target),
            "1: def greet(name):\n2:     msg = 'hi'",
            "1: def greet(name: str):\n2:     msg = 'hi'",
        )

        assert "替换 1 处" in out
        assert target.read_text(encoding="utf-8") == (
            "def greet(name: str):\n    msg = 'hi'\n    return msg\n"
        )

    def test_行号不连续时如实拒绝(self, target: Path) -> None:
        """号码不递增 → 不是"带行号的这一份"，宁可拒绝也不猜。"""
        with pytest.raises(AgentError) as exc:
            edit_file(str(target), "1: def greet(name):\n5:     msg = 'hi'", "X")

        assert "找不到 old_string" in str(exc.value)

    def test_部分行带行号时如实拒绝(self, target: Path) -> None:
        with pytest.raises(AgentError):
            edit_file(str(target), "1: def greet(name):\n    msg = 'hi'", "X")

    def test_行号与文件实际行号对不上时拒绝(self, target: Path) -> None:
        """抄的是第 2 行，却声称是第 1 行 → 不是同一份内容，不许改。"""
        with pytest.raises(AgentError):
            edit_file(str(target), "1:     msg = 'hi'", "    msg = 'hello'")

    def test_不误伤字典字面量(self, tmp_path: Path) -> None:
        """回归：宽松版逐行剥前缀会把 `{'a': 1,` 剥坏（实测把内容删成了 `X`）。"""
        path = tmp_path / "d.py"
        src = "CFG = {\n    'a': 1,\n    'b': 2,\n}\n"
        path.write_text(src, encoding="utf-8", newline="")

        out = edit_file(str(path), "    'a': 1,", "    'a': 42,")

        assert "替换 1 处" in out
        assert path.read_text(encoding="utf-8") == "CFG = {\n    'a': 42,\n    'b': 2,\n}\n"

    def test_单行数字开头的代码不被剥(self, tmp_path: Path) -> None:
        """`1: x` 是**文件里真实存在**的一行时，字面匹配先命中，不该走剥除路径。"""
        path = tmp_path / "s.py"
        path.write_text("1: x\n2: y\n", encoding="utf-8", newline="")

        out = edit_file(str(path), "1: x", "1: z")

        assert "替换 1 处" in out
        assert path.read_text(encoding="utf-8") == "1: z\n2: y\n"


class TestActionableError:
    def test_找不到时给出最接近的一行(self, target: Path) -> None:
        with pytest.raises(AgentError) as exc:
            edit_file(str(target), "    return None", "    return 0")

        message = str(exc.value)
        assert "最接近的一行是" in message
        assert "return msg" in message  # 真给内容，而不是只说"找不到"

    def test_提示里点名不要带行号(self, target: Path) -> None:
        """实测最常踩的坑要写进错误信息里，模型才知道怎么改。"""
        with pytest.raises(AgentError) as exc:
            edit_file(str(target), "no_such_fragment_xyz", "x")

        assert "不要带行号前缀" in str(exc.value)


class TestKeyInfoAtHead:
    def test_命中条数在第一行(self, tmp_path: Path) -> None:
        """观察窗口只保头部：条数写在末尾等于没写（会被一起裁掉）。"""
        (tmp_path / "a.py").write_text("x = 1\ny = 1\n", encoding="utf-8")

        out = search_code(str(tmp_path), "= 1")

        assert out.splitlines()[0].startswith("命中 ")
        assert "如需更多请缩小 pattern" in out.splitlines()[0]

    def test_无命中时不给头部(self) -> None:
        assert _count_header(0) == ""

    def test_目录条目数在第一行给出(self, tmp_path: Path) -> None:
        """只有 `[DIR] pkg/` 时模型不知道里面多大，只能盲钻或直接跳过。"""
        (tmp_path / "pkg").mkdir()
        for name in ("a.py", "b.py", "c.py"):
            (tmp_path / "pkg" / name).write_text("x", encoding="utf-8")
        (tmp_path / "empty").mkdir()

        out = list_dir(str(tmp_path))

        assert "[DIR]  pkg/  (3 项)" in out
        assert "[DIR]  empty/  (0 项)" in out

    def test_超大目录的截断提示在最前面(self, tmp_path: Path) -> None:
        for i in range(510):
            (tmp_path / f"f{i:04d}.txt").write_text("x", encoding="utf-8")

        out = list_dir(str(tmp_path))

        assert out.splitlines()[0].startswith("…[共 ")
        assert "仅显示前" in out.splitlines()[0]
