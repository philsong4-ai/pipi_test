# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

皮皮AI陪伴角色（玩偶）测试评测系统。用于生成测试用例、执行对话测试、评测AI回复质量。

## Architecture

```
本地开发 (当前目录)                 服务器部署 (<SERVER_IP>)
├── web_admin.py  ─── git bundle ──► /opt/pipi-test/web/
├── pipi_api.py
├── index.html
├── start_web.sh
└── ...
```

- **web_admin.py**: Flask 后端，所有 API 路由、数据库操作、任务调度
- **pipi_api.py**: 玩偶 API 调用层，SSE 解析，LLM 调用（用例生成/评测）
- **index.html**: 单文件前端，包含所有 JS 逻辑

## Server Access

```bash
# SSH 必须用 root 用户
ssh root@<SERVER_IP>

# 服务目录
cd /opt/pipi-test/web/
```

## Common Commands

```bash
# 部署代码（通过 git bundle）
git bundle create pipi.bundle --all
scp pipi.bundle root@<SERVER_IP>:/opt/pipi-test/web/pipi.bundle
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && git fetch origin && git reset --hard origin/main && rm pipi.bundle"

# 重启服务（必须用 start_web.sh，包含 LLM 代理环境变量）
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && ./start_web.sh restart"

# 查看服务状态
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && ./start_web.sh status"

# 查看日志
ssh root@<SERVER_IP> "tail -100 /opt/pipi-test/web/web_admin.log"

# 查看服务器当前版本
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && git log --oneline -5"
```

## Database

MySQL 数据库 `pipi_test`，用户 `pipi`，密码 `<DB_PASSWORD>`

关键表：
- `personas`: 用户画像（含 device_id）
- `test_cases`: 测试用例
- `test_tasks`: 测试任务
- `test_results`: 执行结果和评测分数
- `test_dimensions`: 测试维度定义
- `scheduled_tasks`: 预约任务
- `jira_config`: Jira 集成配置

```bash
# 查询数据库
ssh root@<SERVER_IP> "mysql -u pipi -p<DB_PASSWORD> pipi_test -e 'SELECT ...'"
```

## Key Patterns

### SQL 兼容性
代码同时支持 MySQL 和 SQLite，占位符处理：
```python
sql = "SELECT * FROM table WHERE id = %s"  # MySQL
# execute_query() 内部会自动转换 ? 为 %s
```

### SSE 流解析
玩偶 API 返回 SSE 流，结束标志是 `data:[DONE]`：
```python
if json_str == "[DONE]":
    break  # 主动退出循环
```

### 任务执行
测试任务通过 `_execute_task_worker()` 执行，device_id 从 test_tasks 表获取传给玩偶接口。

## External Integrations

- **玩偶 API**: `https://<DOLL_API_DOMAIN>/toy/v1/chat/completions`（SSE 流式）
- **LLM 代理**: `https://<LLM_PROXY_DOMAIN>/v1/chat/completions`（用例生成/评测）
- **Jira Server**: `https://<JIRA_DOMAIN>/`（Bearer Token 认证）

## Naming Convention

AI 陪伴角色统一称"玩偶"，不用具体产品名。
