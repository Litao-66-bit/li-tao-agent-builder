# Agent Builder

> 一个能“制作 Agent 的 Agent”（Meta Agent）：输入一句自然语言需求，自动规划、生成、验证并交付一个可运行的 Agent。

![CI](https://github.com/Litao-66-bit/li-tao-agent-builder/actions/workflows/ci.yml/badge.svg)
![License: MPL-2.0](https://img.shields.io/badge/License-MPL--2.0-blue.svg)


## 它做什么

传统方式下，开发一个 Agent 需要你手写提示词、选框架、接模型、配工具、写测试。Agent Builder 把这个过程自动化：

```
用户需求（自然语言）
      │
      ▼
┌─ Meta Agent ─────────────────────────┐
│ ① 规划：识别 Agent 类型/工具/模型      │
│ ② 生成：产出 Agent 代码与配置          │
│ ③ 验证：在沙箱中运行冒烟测试           │
│ ④ 交付：输出可运行的 Agent             │
└───────────────────────────────────────┘
      │
      ▼
可运行的 Agent（可直接投入业务）
```

## 特性

- **需求驱动**：只用一句话描述“我想要一个 XX 的 Agent”，剩下的交给 Meta Agent
- **框架可插拔**：默认基于 LangGraph，可扩展 Agno / CrewAI 等
- **模型多路线**：API 托管（DeepSeek / 豆包 / 通义 / OpenAI）或本地开源模型（Ollama）
- **开箱即用的工程基线**：Lint（Ruff）+ 测试（pytest）+ CI（GitHub Actions）
- **文件级开源**：MPL-2.0，允许与闭源代码混合使用（详见[许可证](#许可证)）

## 快速开始

### 环境要求

- Python 3.10+
- Git
- 一个模型 API Key（推荐 DeepSeek，性价比高；也可用豆包、通义、OpenAI）
- 可选：本地 GPU ≥12GB 显存（仅当使用本地模型路线）

### 安装

```bash
git clone https://github.com/Litao-66-bit/li-tao-agent-builder.git
cd li-tao-agent-builder

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 推荐：按锁定版本安装（与 CI 一致，可复现）
pip install -r requirements.lock
pip install -e . --no-deps

# 或：按范围安装开发版（会解析最新兼容版本）
pip install -e ".[dev]"
```

### 配置密钥

```bash
cp .env.example .env
# 编辑 .env，填入你的 API Key
```

`.env` 已被 `.gitignore` 忽略，**不要把密钥提交到仓库**。

### 运行

```bash
# 冒烟测试（不调用任何模型，CI 同款）
pytest

# 入口示例（需要 .env 中已配置密钥）
python -m agent_builder "帮我生成一个每日汇总新闻摘要的 Agent"
```

### 使用本地模型（可选）

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b
# 在 .env 中设置 MODEL_PROVIDER=ollama / MODEL_NAME=qwen2.5:7b
```

## 支持的模型路线

| 路线 | 提供商 | 适用场景 |
| --- | --- | --- |
| API 托管（推荐） | DeepSeek / 豆包（火山方舟）/ 通义 / OpenAI | 快速迭代、零运维 |
| 本地开源 | Ollama（Qwen2.5 / DeepSeek-R1-Distill / Llama3.1） | 隐私敏感、零 API 成本 |
| 混合 | 本地推理 + API 兜底 | 上线后的生产形态 |

## 项目结构

```
.
├── agent_builder/            # 核心包
│   └── __init__.py
├── tests/                    # pytest 测试（CI 会执行）
│   └── test_smoke.py
├── .github/workflows/ci.yml  # GitHub Actions 持续集成
├── .env.example              # 密钥模板（真实密钥放 .env，不入库）
├── pyproject.toml            # 项目元数据与依赖
├── LICENSE                   # MPL-2.0
└── README.md
```

## 持续集成（CI）

`.github/workflows/ci.yml` 在每次 `push` 和 `pull_request` 时自动运行：

- Python 3.10 / 3.11 / 3.12 三版本矩阵
- `pip install -e ".[dev]"` 安装依赖
- `ruff check .` 代码规范检查
- `pytest` 测试套件（无需 API Key，CI 可离线跑通）

首次推送后到仓库 **Actions** 页面即可看到流水线；徽章默认显示 `main` 分支结果。

## Roadmap

- [ ] 规划模块：Agent 类型与工具选型
- [ ] 生成模块：代码 + 配置 + 提示词模板
- [ ] 沙箱验证：隔离运行生成物
- [ ] Web 控制台（可选，Node 前端）
- [ ] 模板市场：共享/复用 Agent 模板

## 贡献

欢迎 Issue 与 PR。提交前请先本地跑通 `ruff check .` 与 `pytest`。

## 许可证

[Mozilla Public License 2.0](https://mozilla.org/MPL/2.0/)（`LICENSE`）。

MPL-2.0 是**文件级弱 copyleft** 许可证：修改或新增的 Covered Software 源码文件必须以 MPL-2.0 保持开源；但与这些文件分离开的代码（如你的闭源业务模块）可以自由选择许可证。它允许与 GPL/LGPL/AGPL 生态以外的商业项目混用，是开源 Agent 项目常见的折中选择。

## 合规与安全

- [NOTICE](NOTICE)：三方依赖许可证声明与兼容性说明
- [SECURITY.md](SECURITY.md)：安全政策与漏洞报告渠道
- [PRIVACY.md](PRIVACY.md)：运行时数据处理与隐私说明
