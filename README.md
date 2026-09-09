# Project Paper Recommender

一个面向 Codex 的研究论文推荐 skill。它会从你的全部 Codex 项目中让你选择一个项目，读取项目的研究上下文，推荐 3 篇最相关且互补的论文，并通过 Zotero MCP 自动归档。

## 能做什么

- 动态列出全部 Codex 项目，支持新增和重命名项目。
- 读取所选项目的入口文件、研究计划和近期相关进展，先明确当前研究问题。
- 使用 DOI、PMID 和 arXiv 等稳定标识符核验论文身份。
- 默认排除该项目已经推荐过的论文；不同项目可以复用同一篇论文。
- 将推荐历史、检索记录和 Zotero 归档进度保存在本地 SQLite 数据库中。
- 在 Zotero 个人文献库中复用或创建项目同名顶层集合。
- 保存摘要、项目关联、中文推荐理由和阅读重点为 Zotero 子笔记。
- 归档失败后保留待处理任务，重试时只执行尚未完成的阶段。

## 使用方式

安装并启用 skill 后，直接对 Codex 说：

> 推荐几篇论文

或：

> 给这个项目找 3 篇相关文献

Codex 会依次：

1. 列出全部 Codex 项目，等待你选择。
2. 读取所选项目的有限范围上下文，并用一句话确认推断出的研究问题。
3. 检索、核验并筛选 3 篇论文；每篇说明中文概述、与项目的具体关联、阅读重点和证据依据。
4. 先把推荐和待归档任务写入本地状态，再逐篇处理 Zotero。
5. 重新读取 Zotero 结果，逐篇报告条目、集合和笔记是否成功保存。

这是手动触发流程，不包含每日定时推送，也不会自动下载或附加 PDF。

## 安装

要求：

- Codex Desktop 或支持 skill 的 Codex 环境。
- Python 3.10 或更高版本；状态脚本只使用 Python 标准库。
- 可用的学术检索工具。
- 已连接并具备写入权限的 Zotero MCP。

将仓库克隆到 Codex skill 目录：

```bash
git clone https://github.com/Milleroh/project-paper-recommender.git \
  ~/.codex/skills/project-paper-recommender
```

如果 skill 已经安装，可在其目录更新：

```bash
git -C ~/.codex/skills/project-paper-recommender pull
```

skill 默认允许自然语言自动触发，界面名称为 `Project Paper Recommender`。

## Zotero 归档

归档使用 Zotero MCP，不使用旧的本地导入脚本，也不切换到 Web API 凭据。流程会：

1. 确认个人文献库和项目同名顶层集合。
2. 在整个文献库查重，优先比较 DOI，其次 PMID/arXiv，再比较标题、首位作者、年份和期刊。
3. 有稳定标识符时通过 Zotero 标识符导入，并关闭附件保存。
4. 将已存在的条目加入目标集合，不覆盖用户已有元数据和笔记。
5. 使用固定标记创建幂等的中文推荐子笔记。
6. 每个阶段完成后写入本地状态，并在远端重新读取确认。

Zotero MCP 不可用、网络故障或出现身份/集合歧义时，skill 仍会交付论文推荐，并将未完成的归档任务保留下来。

## 本地状态

推荐历史和归档状态与 skill 文件分离，默认位置为：

```text
macOS:   ~/Library/Application Support/Codex/project-paper-recommender/recommendations.sqlite3
其他系统: ~/.local/state/codex/project-paper-recommender/recommendations.sqlite3
```

可通过环境变量 `CODEX_PROJECT_PAPER_RECOMMENDER_STATE_DIR` 指定状态目录。数据库支持版本迁移，迁移前会创建备份；状态数据库不会提交到本仓库。

状态脚本也可独立使用：

```bash
python3 scripts/recommendation_state.py --help
python3 scripts/recommendation_state.py pending --project-id PROJECT_ID
python3 scripts/recommendation_state.py history --project-id PROJECT_ID
python3 scripts/recommendation_state.py project-map --project-id PROJECT_ID
python3 scripts/recommendation_state.py migrate
```

候选查重、批量记录和 Zotero 分阶段状态的输入格式与操作约束见：

- [`references/state_protocol.md`](references/state_protocol.md)
- [`references/zotero_mcp.md`](references/zotero_mcp.md)

## 开发与测试

运行测试：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/recommendation_state.py tests/test_recommendation_state.py
```

测试覆盖标识符标准化、预印本/正式版身份关联、批量查重、历史排重、项目改名、SQLite 迁移、归档失败恢复、幂等重试和本地归档锁。

模拟测试不等同于真实 Zotero 写入验证。真实验收需要在可用的 Zotero MCP 环境中完成一次完整的三篇论文归档，并重新读取集合和子笔记确认结果。

## 项目结构

```text
project-paper-recommender/
├── SKILL.md                         # Codex skill 入口说明
├── agents/openai.yaml               # Codex 界面元数据与触发策略
├── references/state_protocol.md     # 状态、身份与迁移协议
├── references/zotero_mcp.md         # Zotero MCP 操作参考
├── scripts/recommendation_state.py  # 本地历史与归档状态脚本
└── tests/test_recommendation_state.py
```
