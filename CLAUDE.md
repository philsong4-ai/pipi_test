# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

皮皮AI陪伴角色（玩偶）测试评测系统。用于生成测试用例、执行对话测试、评测AI回复质量。

## Architecture

```
本地开发 (当前目录)                    服务器部署 (<SERVER_IP>)
├── web_admin.py  ─── git bundle ────► /opt/pipi-test/web/
├── pipi_api.py
├── index.html
├── start_web.sh
└── ...
```

### web_admin.py — Flask 后端（~8000行）

按功能区段组织，以注释标记：

| 区段 | 核心职责 |
|------|---------|
| 数据库配置/初始化 | MySQL/SQLite 双后端，`execute_query()` 统一接口，启动时 `_ensure_tables()` 自动 ALTER TABLE 补齐缺失列 + 创建新表 |
| 用户画像 (personas) | CRUD + 批量导入，含 device_id |
| 聊天 (chat/chat_history) | 实时对话 + 历史管理 + 事实自动提取 |
| 遗忘机制配置 (memory/config) | 记忆衰减参数 |
| 自动评测 (eval) | 回复评分 + 统计（趋势/分布/扣分原因/按用户）+ 人工纠正 |
| 用户成长 (growth) | 模拟长期对话，验证记忆形成 |
| 测试用例管理 (test_cases) | CRUD + LLM 生成 + 质量复核 |
| 测试任务 (test_tasks) | 异步执行/评测 + 进度追踪 + 人工纠正 |
| 测试报告 (test_report) | HTML/Excel 报告生成 |
| 预约任务 (scheduled_tasks) | 定时执行 + 调度器循环 |
| API 接口配置 (api_endpoints) | 动态管理外部 API URL/Key |
| Jira 集成 | 评测失败 → 创建 Jira Bug |
| 用例质量校验 (review) | LLM 复核用例是否符合维度要求 + 自动重生成不合格用例 |
| LLM 配置 (llm/config) | 7 个调用场景独立配置 model / temperature / max_tokens / timeout |

### pipi_api.py — API 调用层（~1500行）

- **SSE 流解析**: `parse_sse_buffer()` — 兼容流式(`delta.content`)和非流式(`message.content`)
- **玩偶对话**: `call_pipi_stream()` — 调玩偶 API，返回完整回复
- **LLM 调用**: `call_extract_llm()` / `call_llm_simple()` — 用例生成、事实提取、评测评分
- **评测入口**: `evaluate_chat_reply()` / `evaluate_test_case()` — 调用 LLM 打分，均支持 `corrections` 参数注入 few-shot 案例
- **用例生成**: `generate_test_cases()` / `generate_test_cases_with_feedback()` — LLM 生成+反馈修正
- **用例复核**: `review_case_quality()` — 检查用例是否符合测试维度
- **事实提取**: `extract_facts_from_message()` — 从对话中提取用户事实，含 `entity_name` 判断
- **事实格式化**: `_format_facts_grouped()` — 按分类（CATEGORY_NAMES）分组展示事实
- **纠正案例格式化**: `_format_corrections_for_prompt()` — 将人工纠正记录转为 few-shot prompt 片段
- **System Prompt 构建**: `build_system_prompt()` — 拼装 persona 信息

### index.html — 单文件前端（~5200行）

所有 UI 渲染、图表、交互逻辑在一个 HTML 文件中。

### 其他脚本

- `test_pipi_batch.py` — 批量测试 CLI，支持 `--quick` 单轮和 `--batch` Excel 多轮
- `memory_test_runner.py` — 记忆专项测试
- `sync_personas.py` — 画像数据同步
- `cleanup_forgotten_facts.py` — 清理过期事实
- `eb_admin_current.py` — 空文件，预留占位

## Common Commands

### 部署
```bash
# 本地改代码后提交
git add -A && git commit -m "描述改动"

# 通过 git bundle 部署到服务器
git bundle create pipi.bundle --all
scp pipi.bundle root@<SERVER_IP>:/opt/pipi-test/web/pipi.bundle
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && git fetch origin && git reset --hard origin/main && rm pipi.bundle"

# 重启服务（必须用 start_web.sh，包含 LLM 代理环境变量和 API Key）
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && ./start_web.sh restart"
```

### 运维
```bash
# 查看服务状态
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && ./start_web.sh status"

# 查看日志
ssh root@<SERVER_IP> "tail -100 /opt/pipi-test/web/web_admin.log"

# 查看服务器当前版本
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && git log --oneline -5"

# 查询数据库
ssh root@<SERVER_IP> "mysql -u pipi -p pipi_test -e 'SELECT ...'"
```

## Database

MySQL `pipi_test`，用户 `pipi`，密码 `<DB_PASSWORD>`。代码同时兼容 SQLite（通过 `USE_MYSQL` 环境变量切换）。

关键表：
- `personas`: 用户画像（含 device_id）
- `test_cases`: 测试用例（含 dimension_code、input_text、expected_output、failure_flags）
- `test_tasks`: 测试任务（含 status、progress）
- `test_results`: 执行结果（含 actual_output、score、executed_at、deduction_reason、human_score、human_note）
- `test_dimensions`: 测试维度定义
- `scheduled_tasks`: 预约任务（cron 表达式）
- `growth_tasks`: 成长模拟任务
- `jira_config`: Jira 集成配置（jira_url、jira_token）
- `api_endpoints`: 外部 API 配置（base_url、auth_config JSON）
- `auto_evaluation`: 自动评测记录（含 human_score、human_note）
- `eval_corrections`: 人工纠正案例（eval_type、ref_id、dimension_code、user_input、ai_reply、auto_score、human_score、correction_reason），用于 few-shot prompt 注入
- `user_facts`: 用户事实记忆
- `chat_messages`: 聊天历史
- `async_tasks`: 异步任务（生成/执行/评测），含 progress_json 和 result_json
- `toy_persona`: 玩偶人设配置（当前仅「秋秋」）

## Key Patterns

### 异步 Worker 模式
所有耗时操作（任务执行、用例生成、成长模拟）都通过 `threading.Thread(target=_xxx_worker, args=(...))` 启动后台线程。Worker 内部自行更新数据库状态（pending → running → completed/failed）。**任务间完全无锁**，依赖数据库 status 字段协调。

### SQL 占位符
- MySQL: `%s`
- SQLite: `?`
- `execute_query()` 内部自动将 `?` 转为 `%s`，所以写 SQL 时统一用 `?` 即可

### SSE 流结束标志
玩偶 API SSE 流的结束标志是 `data:[DONE]`（JSON 字符串 `"[DONE]"`），收到后需主动 `break`，否则连接会一直等待。

### 多轮对话格式
测试用例 `input_text` 使用 `【Rn】` 标记轮次：
```
【R1】你今天开心吗？
【R2】为什么？
```

### 任务执行
`_execute_task_worker()` 从 `test_tasks` 表获取 device_id，遍历关联的 test_cases，逐条调玩偶 API，结果写入 `test_results`。

### 预约任务全流程 (full_flow)

定时触发的 `full_flow` 任务按顺序执行四个阶段：

1. **生成**: `_generate_cases_worker()` — 逐维度调 `generate_test_cases()`，用例写入 test_cases
2. **审核**: `_wait_for_quality_review()` — 轮询 `quality_status`（最多等 30 分钟，每 10s 检查），不合格自动重生成最多 2 次
3. **执行**: 创建 test_task → `_execute_task_worker()` — 逐条调玩偶 API，结果写入 test_results
4. **评测**: 创建 eval_task → `_evaluate_task_worker()` — 逐条调 `evaluate_test_case()`，分数写入 test_results

生成的 task_id 格式为 `gen:{id},task:{id},eval:{id}`，生命周期通过 `async_tasks` 表的 `progress_json` 追踪。

### LLM 配置结构

每个调用场景独立配置 4 个参数（`model`、`temperature`、`max_tokens`、`timeout`），通过 `get_llm_config()` 读取并按 key 解包：

```python
llm_config = get_llm_config()
cases = pipi_api.generate_test_cases(
    dimension=dim, ..., **llm_config["case_gen"]
)
```

7 个 key: `case_gen`, `case_regenerate`, `fact_extract`, `eval_batch`, `eval_case`, `eval_realtime`, `case_review`。

### 用例生成重试

`generate_test_cases()` 内部最多 3 次尝试（间隔 3s），JSON 解析失败或异常时重试。worker 在收到空结果后还会补重试 1 次（间隔 5s）。总共最多 6 次 LLM 调用。

### 自动重生成 (AUTO REGEN)

质量审核 `async_review_cases()` 完成后，`failed` 状态的用例按维度分组，调用 `_regenerate_dimension_with_feedback()` 带问题反馈重新生成，最多重试 2 次。注意读 `toy_persona`（单数），不是 `toy_personas`。

### Python scoping trap

如果 `import re` 写在函数内的条件分支（`if`/`for`/`try`）中，Python 会把 `re` 标记为局部变量。当条件不满足时 `re` 未赋值，后续语句 `re.match()` 直接 `UnboundLocalError`。方案：`import re` 放在函数顶部或模块顶部。

### 人工纠正 (human correction)

`auto_evaluation` 和 `test_results` 表均有 `human_score` INT 和 `human_note` TEXT 列。

- `POST /api/eval/<message_id>/correct` — 纠正聊天评测分数
- `POST /api/test_results/<result_id>/correct` — 纠正测试结果分数
- 统计 API（trend / by_user）使用 `COALESCE(human_score, total_score)` 优先人工分
- 前端评测详情弹窗有「人工纠正」按钮，人工纠正后 badge 显示 ✏️ 标记+橙色标识
- `_save_correction()`: `human_note` 为空时不写入 `eval_corrections`；按 `ref_id` 去重（同 item 多次纠正只保留最新一条）

### Few-shot 纠正注入

每次评测调用前，从 `eval_corrections` 表加载最近的纠正案例注入到 LLM system prompt 末尾。

- `_load_recent_corrections(eval_type, dimension_code, limit)` — 按类型（chat / test_case）和维度加载纠正记录
- `_format_corrections_for_prompt(corrections, eval_type)` — 格式化为「历史纠正案例」prompt 片段
- `evaluate_chat_reply()` 和 `evaluate_test_case()` 均接受 `corrections: List[Dict]` 参数（默认 None），非空时拼接到 system_prompt
- 所有评测 worker（实时/批量/任务/重评/独立用例）均在调用评测前加载 corrections 传入

## External Integrations

- **玩偶 API**: `https://<DOLL_API_DOMAIN>/toy/v1/chat/completions`（SSE 流式）
- **LLM 代理**: `https://<LLM_PROXY_DOMAIN>/v1/chat/completions`（用例生成/评测/复核）
- **Jira Server**: `https://<JIRA_DOMAIN>/`（Bearer Token 认证）

## Local Development

本地可直接运行 Flask 开发服务器，但需设置环境变量连接服务器数据库或本地 SQLite：

```bash
# 使用本地 SQLite
export USE_MYSQL=0
python3 web_admin.py

# 或连接远程 MySQL（需网络可达）
export USE_MYSQL=1
export MYSQL_HOST=<SERVER_IP>
python3 web_admin.py
```

**注意**: 本地代码没有 `start_web.sh` 中的 API Key 环境变量，LLM 相关功能（用例生成、评测）在本地无法正常工作，只能测试 CRUD 类接口。

## Naming Convention

AI 陪伴角色统一称"玩偶"，不用具体产品名。
