# pipi-test

皮皮 AI 陪伴玩偶测试评测系统。用于生成测试用例、执行多轮对话测试、评测 AI 回复质量、产出测试报告与 Jira Bug 单。

## 文档导航

- [系统架构文档](ARCHITECTURE.md) — 12 节：概览 / 架构图 / 后端区段 / API 调用层 / 前端 / 数据库 / 外部集成 / 部署 / 关键流程 / 陷阱 / 本地开发
- [测试全流程 Workflow](WORKFLOW.md) — 12 阶段：准备 / 用例生成 / 执行 / 评测 / 纠正 / 统计 / 报告 / Jira / 红队 / 预约 / 成长模拟 / 事实记忆

## 架构

```
本地开发 (当前目录)                     服务器部署 (<SERVER_IP>)
├── web_admin.py  ─── git bundle ────► /opt/pipi-test/web/
├── pipi_api.py
├── index.html
├── interface_profiles/                ├── .env (DB密码/API Key/OIDC)
│   └── pipi.json (接口 prompt 配置)   ├── cert.pem / key.pem (HTTPS)
├── aivs_demo/                          ├── start_web.sh
│   ├── ask.sh (AIVS Java SDK 调用)    └── web_admin.log
│   └── AivsDemo.java
└── start_web.sh
```

| 文件 | 说明 |
|------|------|
| `web_admin.py` | Flask 后端（~8000 行），API 路由、数据库操作、异步任务调度、OIDC 鉴权 |
| `pipi_api.py` | API 调用层（~1500 行），SSE 流解析、LLM 调用、评测/生成/审核 |
| `index.html` | 单文件前端（~5500 行），UI 渲染、图表、交互逻辑 |
| `interface_profiles/pipi.json` | 接口 prompt 配置（generate_test_cases / evaluate_* / persona_messages / 维度 checklist / 硬规则） |
| `aivs_demo/ask.sh` | AIVS Java SDK 调用脚本（5 位置参数） |
| `aivs_demo/AivsDemo.java` | AIVS Java SDK 示例 |
| `test_pipi_batch.py` | CLI 批量测试脚本 |
| `memory_test_runner.py` | 记忆专项测试 |
| `sync_personas.py` | 画像数据同步 |
| `cleanup_forgotten_facts.py` | 清理过期事实 |
| `start_web.sh` | 服务启停脚本（含 API Key 环境变量） |

## 核心功能

- **用户画像管理** — 61 字段画像，LLM 生成 + 手动填写；CRUD + 批量创建
- **聊天测试** — 模拟用户与玩偶实时对话，自动提取用户事实、自动评测打分
- **测试用例生成** — LLM 按维度生成多轮对话用例（`【R1】【R2】` 格式），含质量复核 + 不合格自动重生成
- **异步任务执行** — 后台 worker 模式，通过数据库 status 字段协调，无锁
- **多裁判集成评测** — 3 个 LLM 独立打分取均值，标准差反映一致性
- **Few-shot 纠正注入** — 人工纠正记录自动注入下次评测 prompt
- **测试报告** — HTML / Excel 双格式，含统计 sheet（通过率 / 维度 / 接口 / 红队 / 分数段）
- **Jira 集成** — 评测失败自动创建 Bug 单，按 ref_id 去重
- **红队安全测试** — 5 P0 维度 × 13 类攻击 × 25 条陷阱用例，含多轮陷阱链
- **预约任务 full_flow** — cron 定时触发「生成 → 审核 → 执行 → 评测」四阶段
- **长期成长模拟** — 多日对话 + 记忆形成验证
- **LLM 配置** — 7 个调用场景独立配置 model/temperature/max_tokens/timeout
- **OIDC 单点登录** — SSO 接入，未登录 401 自动跳转

## 快速开始

```bash
# 本地开发（SQLite，LLM 功能不可用）
export USE_MYSQL=0
python3 web_admin.py

# 连接服务器 MySQL（需网络可达）
export USE_MYSQL=1
export MYSQL_HOST=<SERVER_IP>
python3 web_admin.py
```

> **注意**：本地无 `start_web.sh` 中的 API Key 环境变量，LLM 相关功能（用例生成、评测）在本地无法工作，只能测试 CRUD 类接口。

## 部署

```bash
# 本地改代码后提交
git add -A && git commit -m "描述"

# 通过 git bundle 部署到服务器
git bundle create pipi.bundle --all
scp pipi.bundle root@<SERVER_IP>:/opt/pipi-test/web/pipi.bundle
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && \
  git fetch origin && git reset --hard origin/main && rm pipi.bundle && \
  ./start_web.sh restart"

# 查看状态/日志
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && ./start_web.sh status"
ssh root@<SERVER_IP> "tail -100 /opt/pipi-test/web/web_admin.log"
```

## 服务进程

gunicorn 8 worker × 2 端口（HTTP 8080 + HTTPS 443），timeout 300s。

```bash
# start_web.sh 关键内容
nohup gunicorn -w 8 -b 0.0.0.0:8080 --timeout 300 web_admin:app >> web_admin.log 2>&1 &
nohup gunicorn -w 8 -b 0.0.0.0:443 --certfile=cert.pem --keyfile=key.pem --timeout 300 web_admin:app >> web_admin_https.log 2>&1 &
```

## 数据库

MySQL `pipi_test`（用户 `pipi`），代码同时兼容 SQLite（通过 `USE_MYSQL` 环境变量切换）。

关键表：

| 表 | 用途 |
|----|------|
| `personas` | 用户画像（61 字段 + device_id + target_api） |
| `test_cases` | 测试用例（含 is_redteam / redteam_trap_type） |
| `test_tasks` | 测试任务 |
| `test_results` | 执行结果（score / human_score / dialog_ids / ttfb_ms） |
| `test_dimensions` | 测试维度定义 |
| `scheduled_tasks` | 预约任务（cron + full_flow） |
| `user_facts` | 用户事实记忆（is_active / emotion_tag） |
| `chat_messages` | 聊天历史 |
| `async_tasks` | 异步任务（progress_json） |
| `auto_evaluation` | 自动评测记录 |
| `eval_corrections` | 人工纠正案例（few-shot 注入） |
| `eval_config` | 评测开关（auto_eval_enabled / inject_corrections） |
| `llm_config` | LLM 7 场景参数 |
| `api_endpoints` | 玩偶 API 配置 |
| `concurrency_slots` | 并发槽位（aivs/api/llm） |
| `toy_persona` | 玩偶人设 |
| `jira_config` | Jira 集成配置 |
| `growth_tasks` | 成长模拟任务 |

### SQL 占位符

MySQL 用 `%s`，SQLite 用 `?`。`execute_query()` 自动 `?` → `%s`，写 SQL 统一用 `?`。

## 外部集成

| 集成 | 用途 |
|------|------|
| 玩偶 API | SSE 流式对话 |
| AIVS Java SDK | AIVS 文本模式（通过 ask.sh 子进程） |
| LLM 代理 | 用例生成 / 评测 / 复核 / 事实抽取 |
| Jira Server | 评测失败 → Bug 单 |
| SSO OIDC | 单点登录 |

## 关键陷阱（详见 ARCHITECTURE.md §10）

- **Python scoping** — `import re` 不要写在函数内条件分支
- **AIVS 孤儿进程** — `ask.sh` 用 `disown`，gunicorn SIGKILL 时 Java 变孤儿占槽，启动时 `_ensure_tables()` 自动重置
- **Decimal 序列化** — MySQL `SUM(...)` 返回 Decimal，自定义 JSONEncoder 兜底
- **datetime 时区** — Flask 默认 RFC 822 + GMT 让浏览器误判 UTC +8，自定义 JSONEncoder 输出本地字符串
- **SSE 结束标志** — `data:[DONE]` 须主动 `break`

## 命名约定

AI 陪伴角色统一称「玩偶」，不用具体产品名。
