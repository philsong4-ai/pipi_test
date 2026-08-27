# 皮皮AI 陪伴玩偶对话测试评测系统 — 架构文档

## 1. 概览

用于对 AI 陪伴玩偶（玩偶）对话质量进行**测试用例生成 → 批量执行 → 自动评测 → 报告产出**的全链路自动化。同时支持实时人工对话、用户事实记忆管理、预约任务调度、红队安全测试等。

- **技术栈**：Python 3.6 / Flask / MySQL 8 / 单文件前端（HTML+JS）
- **部署**：单机 gunicorn + nginx，自签 HTTPS
- **集成**：玩偶 API（SSE 流式）、LLM 代理（多模型）、Jira（缺陷跟踪）、SSO OIDC

## 2. 整体架构

```
┌────────────────────────────────────────────────────────────────────┐
│                          浏览器 (index.html)                        │
│  ┌──────────┬──────────┬──────────┬──────────┬──────────┬──────┐ │
│  │ 用户画像 │ 实时对话 │ 测试用例 │ 测试任务 │ 评测统计 │ 报告 │ │
│  └──────────┴──────────┴──────────┴──────────┴──────────┴──────┘ │
└──────────────────────────┬─────────────────────────────────────────┘
                           │ fetch /api/*（OIDC Bearer）
┌──────────────────────────▼─────────────────────────────────────────┐
│                       nginx (HTTPS 443)                             │
└──────────────────────────┬─────────────────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────────────────┐
│       gunicorn (8 workers, timeout 300s) → web_admin:app           │
│                                                                     │
│  ┌──────────────┬──────────────┬──────────────┬────────────────┐  │
│  │ 鉴权/SSO     │ CRUD 路由    │ 异步 Worker  │ 调度器循环     │  │
│  │ before_req   │ /api/...     │ threading.T  │ scheduled_tasks │  │
│  └──────────────┴──────────────┴──────────────┴────────────────┘  │
│                          │                                          │
│       ┌──────────────────┴───────────────────┐                    │
│       ▼                                      ▼                    │
│  pipi_api.py                            MySQL (pipi_test)         │
│  - SSE 解析                              personas / test_cases    │
│  - LLM 调用                              test_tasks / test_results│
│  - 评测/生成                              user_facts / async_tasks│
│                                          eval_corrections         │
└──────────┬──────────────────────────┬──────────┬──────────────────┘
            │                          │          │
            ▼                          ▼          ▼
   ┌────────────────┐         ┌──────────────┐  ┌─────────────┐
   │ 玩偶 API (SSE) │         │ LLM 代理      │  │ Jira Server  │
   │ <DOLL_API>    │         │ <LLM_PROXY>   │  │ <JIRA_DOMAIN>│
   └────────────────┘         └──────────────┘  └─────────────┘
```

## 3. 后端 web_admin.py（~8000 行）

按功能区段组织，每段以注释标记：

| 区段 | 核心职责 |
|------|---------|
| 数据库配置/初始化 | MySQL/SQLite 双后端，`execute_query()` 统一接口；`_ensure_tables()` 启动时自动 ALTER TABLE 补齐新列 + 创建新表 |
| JSON 序列化 | 自定义 `_LocalJSONEncoder`：datetime → 本地时间字符串（无时区后缀）、Decimal → int/float |
| OIDC 鉴权 | `before_request` 全局拦截 `/api/*`，白名单放行 `/api/auth/*`；未登录 401 + `login_url` |
| 用户画像 (personas) | CRUD + 批量创建，含 device_id、target_api；含 61 字段 LLM 画像生成 |
| 聊天 (chat/chat_history) | 实时对话 + 历史拉取 + 事实自动抽取 |
| 遗忘机制配置 (memory/config) | 记忆衰减参数（半衰期、保留阈值） |
| 自动评测 (eval) | 多裁判集成打分 + 统计（趋势/分布/扣分原因/按用户）+ 人工纠正 |
| 用户成长 (growth) | 模拟长期对话，验证记忆形成 |
| 测试用例 (test_cases) | CRUD + LLM 生成 + 质量复核 |
| 测试任务 (test_tasks) | 异步执行/评测 + 进度追踪 + 人工纠正 |
| 测试报告 (test_report) | HTML/Excel 报告生成 |
| 预约任务 (scheduled_tasks) | cron 表达式 + 调度器循环 |
| API 接口配置 (api_endpoints) | 动态管理玩偶 API 的 URL/Key/Headers/Protocol |
| Jira 集成 | 评测失败 → 创建 Jira Bug |
| 用例质量校验 (review) | LLM 复核用例是否符合测试维度 + 自动重生成不合格用例 |
| LLM 配置 (llm/config) | 7 个调用场景独立配置 model/temperature/max_tokens/timeout |

### 异步 Worker 模式

所有耗时操作（任务执行、用例生成、成长模拟、实时评测）通过 `threading.Thread(target=_xxx_worker, args=(...))` 启动后台线程。Worker 内自行更新数据库状态（pending → running → completed/failed）。**任务间无锁**，依赖数据库 status 字段协调。

### 预约任务全流程 (full_flow)

定时触发的 `full_flow` 依次执行四个阶段：

1. **生成** `_generate_cases_worker()` — 逐维度调 `generate_test_cases()`，写入 test_cases
2. **审核** `_wait_for_quality_review()` — 轮询 `quality_status`（最多 30 分钟，每 10s 检查），不合格自动重生成最多 2 次
3. **执行** 创建 test_task → `_execute_task_worker()` — 逐条调玩偶 API，写入 test_results
4. **评测** 创建 eval_task → `_evaluate_task_worker()` — 逐条调 `evaluate_test_case()`，分数写入 test_results

task_id 格式 `gen:{id},task:{id},eval:{id}`，通过 `async_tasks.progress_json` 追踪生命周期。

## 4. API 调用层 pipi_api.py（~1500 行）

| 函数 | 职责 |
|------|------|
| `parse_sse_buffer()` | SSE 流解析，兼容流式(`delta.content`)和非流式(`message.content`) |
| `call_pipi_stream()` | 调玩偶 API，返回完整回复 + 首字 token 耗时 + dialog_id |
| `call_aivs_stream()` | 调 AIVS Java SDK 子进程模式（文本模式） |
| `call_extract_llm()` / `call_llm_simple()` | LLM 通用调用（用例生成、事实提取、评测） |
| `evaluate_chat_reply()` / `evaluate_test_case()` | 评测打分，支持 `corrections` 参数注入 few-shot |
| `generate_test_cases()` / `generate_test_cases_with_feedback()` | LLM 生成 + 反馈修正 |
| `review_case_quality()` | 检查用例是否符合测试维度 |
| `extract_facts_from_message()` | 从对话提取用户事实，含 `entity_name` 判断 |
| `build_system_prompt()` | 拼装 persona 信息 + 玩偶人设 |

### SSE 流结束标志

玩偶 API SSE 流的结束标志是 `data:[DONE]`（JSON 字符串 `"[DONE]"`），收到后需主动 `break`，否则连接会一直等待。

### LLM 配置结构

每个调用场景独立配置 4 个参数（`model`、`temperature`、`max_tokens`、`timeout`），通过 `get_llm_config()` 读取并按 key 解包：

```python
llm_config = get_llm_config()
cases = pipi_api.generate_test_cases(
    dimension=dim, ..., **llm_config["case_gen"]
)
```

7 个 key：`case_gen` / `case_regenerate` / `fact_extract` / `eval_batch` / `eval_case` / `eval_realtime` / `case_review`。

### 用例生成重试

`generate_test_cases()` 内部最多 3 次尝试（间隔 3s），JSON 解析失败或异常时重试。Worker 在收到空结果后还会补重试 1 次（间隔 5s）。**总共最多 6 次 LLM 调用**。

### 多裁判集成评测

实时/批量评测均使用 3 个 LLM 模型独立打分，取均值，标准差反映一致性。各场景的模型在 `eval_config` 表配置。

### Few-shot 纠正注入

每次评测调用前，从 `eval_corrections` 表加载最近人工纠正案例注入到 LLM system prompt 末尾：

- `_load_recent_corrections(eval_type, dimension_code, limit)` — 按类型/维度加载
- `_format_corrections_for_prompt(corrections, eval_type)` — 格式化为 prompt 片段
- 所有评测 worker 在调用评测前均加载 corrections 传入
- 可通过 `eval_config.inject_corrections` 开关关闭

## 5. 前端 index.html（~5200 行）

单文件包含所有 UI、图表、交互逻辑。关键模块：

| 模块 | 功能 |
|------|------|
| 用户画像面板 | 卡片列表 + 分页 + 搜索 + 61 字段编辑表单 |
| 实时对话面板 | 用户切换、SSE 流式接收、消息气泡、dialog_id 展示、关键词搜索、自动评测分数显示 |
| 测试用例面板 | 维度筛选、批量导入、生成进度追踪、用例复核 |
| 测试任务面板 | 任务创建、进度条、结果列表、人工纠正入口 |
| 评测统计面板 | 趋势图、分布图、扣分原因聚类、按用户汇总 |
| 红队测试面板 | 5 P0 维度 × 13 类攻击 × 25 条陷阱用例 |
| 预约任务面板 | cron 表达式配置 + full_flow 链路可视化 |
| 成长模拟面板 | 长期对话任务 + 记忆形成验证 |
| 测试报告 | 任务选择 + 报告预览 + Excel 导出 |
| API 接口配置 | base_url/auth_config JSON 动态管理 |
| LLM 配置 | 7 场景参数独立配置 |

### 前端通用 `api()` helper

```js
function api(path, method, body) {
  return fetch(path, {method, headers, body}).then(r => {
    if (r.status === 401) { window.location.href = '/api/auth/login'; throw... }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  });
}
```

全局 `window.fetch` 拦截器：401 自动跳 SSO 登录（排除 `/api/auth/*` 自身）。

## 6. 数据库

MySQL `pipi_test`，用户 `pipi`。代码兼容 SQLite（通过 `USE_MYSQL` 环境变量切换）。

### SQL 占位符

- MySQL: `%s` / SQLite: `?`
- `execute_query()` 内部自动 `?` → `%s`，写 SQL 统一用 `?`

### 关键表

| 表 | 用途 |
|----|------|
| `personas` | 用户画像（id 字符串主键、device_id、target_api、61 字段画像） |
| `test_cases` | 测试用例（dimension_code、input_text、expected_output、failure_flags、is_redteam、redteam_trap_type） |
| `test_tasks` | 测试任务（status、progress、case_ids） |
| `test_results` | 执行结果（actual_output、score、deduction_reason、human_score、human_note、dialog_ids、ttfb_ms、total_ms、target_api） |
| `test_dimensions` | 测试维度定义 |
| `scheduled_tasks` | 预约任务（cron 表达式 + full_flow flag） |
| `growth_tasks` | 成长模拟任务 |
| `jira_config` | Jira 集成配置 |
| `api_endpoints` | 玩偶 API 配置（base_url、auth_config JSON、protocol） |
| `auto_evaluation` | 自动评测记录（含 human_score、human_note） |
| `eval_corrections` | 人工纠正案例（eval_type、ref_id、dimension_code），用于 few-shot prompt 注入 |
| `user_facts` | 用户事实记忆（persona_id、category、fact_key、entity_name、fact_value、is_active、occurred_at、emotion_tag） |
| `chat_messages` | 聊天历史（persona_id、role、user_name、text、ttfb_ms、total_ms） |
| `async_tasks` | 异步任务（type、status、progress_json、result_json） |
| `concurrency_slots` | 并发槽位（aivs_global、api_global、llm_global，current_count/max_count） |
| `eval_config` | 评测开关（auto_eval_enabled、inject_corrections） |
| `llm_config` | LLM 7 场景参数（model、temperature、max_tokens、timeout） |
| `toy_persona` | 玩偶人设配置（当前仅「小米绒绒」） |

### 并发槽位

`concurrency_slots` 表用于限制 AIVS/API/LLM 的并发数：

- `aivs_global` — AIVS Java SDK 子进程并发上限
- `api_global` — 玩偶 API 并发上限
- `llm_global` — LLM 代理并发上限

Worker 调用前先 `SELECT ... FOR UPDATE` 占槽，调用结束 `UPDATE current_count - 1`。gunicorn worker 被 SIGKILL 时可能产生孤立 Java 子进程占槽 → `_ensure_tables()` 启动时自动重置 `aivs_global.current_count=0`。

## 7. 外部集成

| 集成 | 用途 | 地址 |
|------|------|------|
| 玩偶 API | 玩偶对话（SSE 流式） | `https://<DOLL_API_DOMAIN>/toy/v1/chat/completions` |
| AIVS Java SDK | AIVS 文本模式（通过 ask.sh 子进程调用） | 本地 JVM 子进程 |
| LLM 代理 | 用例生成 / 评测 / 复核 / 事实提取 | `https://<LLM_PROXY_DOMAIN>/v1/chat/completions` |
| Jira Server | 评测失败 → Bug 单 | `https://<JIRA_DOMAIN>/`（Bearer Token） |
| SSO OIDC | 单点登录 | 自建 OIDC Provider |

## 8. 部署架构

```
开发机 (macOS)                      服务器 (<SERVER_IP>, Alibaba Cloud Linux 3)
├── web_admin.py                    /opt/pipi-test/web/
├── pipi_api.py                     ├── web_admin.py
├── index.html                      ├── pipi_api.py
├── start_web.sh                    ├── index.html
├── interface_profiles/*.json       ├── start_web.sh
└── ...                             ├── .env (DB密码、API Key、OIDC配置)
                                    ├── cert.pem / key.pem (自签 HTTPS)
                                    ├── web_admin.log
                                    └── web_admin_https.log
```

### 部署流程

```bash
# 本地改代码后提交
git add -A && git commit -m "..."

# 通过 git bundle 部署
git bundle create pipi.bundle --all
scp pipi.bundle root@<SERVER_IP>:/opt/pipi-test/web/pipi.bundle
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && \
  git fetch origin && git reset --hard origin/main && rm pipi.bundle"

# 重启服务（必须用 start_web.sh，包含 LLM 代理环境变量和 API Key）
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && ./start_web.sh restart"
```

### 服务进程

gunicorn 监听两个端口（HTTP 8080 + HTTPS 443），各 8 worker，timeout 300s。nginx 在前面做反代和证书卸载（如有）。

### start_web.sh 关键内容

```bash
cd /opt/pipi-test/web
pkill -f 'gunicorn.*web_admin' 2>/dev/null
[ -f .env ] && set -a && source .env && set +a
nohup gunicorn -w 8 -b 0.0.0.0:8080 --timeout 300 web_admin:app >> web_admin.log 2>&1 &
nohup gunicorn -w 8 -b 0.0.0.0:443 --certfile=cert.pem --keyfile=key.pem --timeout 300 web_admin:app >> web_admin_https.log 2>&1 &
```

## 9. 关键流程

### 9.1 实时对话流程

```
浏览器 sendMsg()
  → POST /api/chat {persona_id, message, target_api}
  → call_api(persona_id, message)
    → save_chat_msg(persona_id, "user", ...)
    → SELECT persona + build_system_prompt()
    → pipi_api.call_pipi_stream(messages, device_id, ...)
      → SSE 流式接收
      → parse_sse_buffer() 解析 delta.content
      → data:[DONE] 触发 break
    → save_chat_msg(persona_id, "pipi", reply, ttfb_ms, total_ms)
    → 启动 _extract_and_save 线程（事实抽取）
    → 启动 _evaluate_and_save 线程（实时评测，如开关开启）
  → 返回 {full_text, message_id, dialog_id, response_time_ms, ttfb_ms}
  → 前端 addMsg + loadEvalForMessage 轮询评测分数
```

### 9.2 测试用例生成流程

```
worker: _generate_cases_worker(task_id)
  → 遍历 test_dimensions
  → 对每个维度调用 generate_test_cases(dim, ...)
    → 内部最多 3 次 LLM 重试 + JSON 解析
  → 写入 test_cases (quality_status='pending')
  → 触发 async_review_cases()
    → LLM 复核 quality_status → 'passed' / 'failed'
    → failed 的用例按维度分组调用 _regenerate_dimension_with_feedback()
    → 最多重试 2 次
  → 完成后更新 async_tasks.status='completed'
```

### 9.3 评测流程

```
worker: _evaluate_task_worker(task_id)
  → 遍历 test_results WHERE task_id=?
  → 对每条调用 evaluate_test_case(case, actual_output, ...)
    → 加载 eval_corrections[eval_type='test_case', dimension_code]
    → _format_corrections_for_prompt() 拼 few-shot
    → 3 个 LLM 裁判独立打分
    → 取均值、计算标准差
  → 写回 test_results.score / deduction_reason / eval_detail
  → 失败用例（score < 阈值）触发 Jira Bug 创建
```

### 9.4 多轮对话格式

测试用例 `input_text` 使用 `【Rn】` 标记轮次：

```
【R1】你今天开心吗？
【R2】为什么？
```

执行时按 `【R(\d+)】` 正则切分，每轮依次发送，messages 累积。

## 10. 关键模式与陷阱

### Python scoping trap

`import re` 若写在函数内的条件分支（`if`/`for`/`try`）中，Python 把 `re` 标记为局部变量。当条件不满足时 `re` 未赋值，后续 `re.match()` 直接 `UnboundLocalError`。**方案：`import re` 放函数顶部或模块顶部。**

### AIVS Java SDK 孤儿进程

`ask.sh` 用 `disown` 把 Java 子进程脱离 job control。gunicorn worker 被 SIGKILL 时，Java 进程变 PPID=1 孤儿，仍持有 `aivs_global` 槽。表现：后续 AIVS 调用全部卡在等槽，触发 gunicorn 300s timeout → 504。

**已修复**：`call_aivs_stream` 在 `subprocess.TimeoutExpired` 时 `pkill -9 -f com.example.AivsDemo`；`_ensure_tables()` 启动时重置 `aivs_global.current_count=0`。

### Decimal 序列化

MySQL `SUM(...)` 返回 DECIMAL，pymysql 转 Python `decimal.Decimal`，Flask 默认 JSONEncoder 不识别 → 接口 500。表现：前端 `loadFactsCount` 抛错无 catch，事实数 badge 永远停留 `…` 占位符。

**已修复**：自定义 `_LocalJSONEncoder` 加 Decimal 分支。

### datetime 时区错位

Flask 默认 JSONEncoder 把 datetime 转 RFC 822 + GMT 后缀，浏览器 `new Date()` 误判为 UTC 再 +8h 转本地，时间显示偏移 8 小时。

**已修复**：`_LocalJSONEncoder` datetime → `YYYY-MM-DD HH:MM:SS` 本地字符串，无时区后缀。

## 11. 本地开发

本地可运行 Flask 开发服务器，但 LLM 相关功能（用例生成、评测）因无 `start_web.sh` 中的 API Key 环境变量无法工作，只能测试 CRUD 类接口：

```bash
# 本地 SQLite
export USE_MYSQL=0
python3 web_admin.py

# 或远程 MySQL
export USE_MYSQL=1
export MYSQL_HOST=<SERVER_IP>
python3 web_admin.py
```

## 12. 命名约定

AI 陪伴角色统一称"玩偶"，不用具体产品名。
