# pipi-test

皮皮 AI 陪伴玩偶测试评测系统。用于生成测试用例、执行多轮对话测试、评测 AI 回复质量。

## 架构

```
本地开发 (当前目录)                    服务器部署 (<SERVER_IP>)
├── web_admin.py  ─── git bundle ────► /opt/pipi-test/web/
├── pipi_api.py
├── index.html
└── start_web.sh
```

| 文件 | 说明 |
|------|------|
| `web_admin.py` | Flask 后端（~7700行），API 路由、数据库操作、异步任务调度 |
| `pipi_api.py` | API 调用层（~1400行），SSE 流解析、LLM 调用、评测/生成/审核 |
| `index.html` | 单文件前端（~5000行），UI 渲染、图表、交互逻辑 |
| `start_web.sh` | 服务启动/重启/状态脚本（含 API Key 环境变量） |

## 核心功能

- **用户画像管理** — personas 表 CRUD + 批量导入
- **聊天测试** — 模拟用户与玩偶实时对话，自动提取用户事实
- **测试用例生成** — LLM 按 10 大测试维度生成多轮对话用例
- **用例质量审核** — LLM 复核用例是否符合维度要求，不合格自动重生成
- **异步任务执行** — 后台 worker 模式，通过数据库 status 字段协调
- **评测评分** — 实时评测 + 用例结果评测，多维度打分
- **测试报告** — HTML/Excel 报告生成
- **预约任务** — cron 表达式定时执行 full_flow（生成→审核→执行→评测）
- **Jira 集成** — 评测失败自动创建 Bug
- **LLM 配置** — 7 个调用场景独立配置 model/temperature/max_tokens/timeout

## 快速开始

```bash
# 本地开发（SQLite）
export USE_MYSQL=0
python3 web_admin.py

# 连接服务器 MySQL
export USE_MYSQL=1
export MYSQL_HOST=<SERVER_IP>
python3 web_admin.py
```

## 部署

```bash
git bundle create pipi.bundle --all
scp pipi.bundle root@<SERVER_IP>:/opt/pipi-test/web/
ssh root@<SERVER_IP> "cd /opt/pipi-test/web && git fetch origin && git reset --hard origin/main && rm pipi.bundle && ./start_web.sh restart"
```

## 数据库

MySQL `pipi_test`，代码同时兼容 SQLite（通过 `USE_MYSQL` 环境变量切换）。

## 命名约定

AI 陪伴角色统一称「玩偶」，不用具体产品名。
