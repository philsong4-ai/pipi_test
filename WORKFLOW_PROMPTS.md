# 测试全流程与对应 Prompt 映射

> 阶段 × 步骤 → 调用场景 → prompt 模板（`interface_profiles/pipi.json`）

## 流程总览

```
准备阶段                测试阶段                评测阶段                产出阶段
─────────              ─────────              ─────────              ─────────
1. 创建用户画像         4. 生成测试用例         7. 自动评测打分         10. 测试报告
2. 配置 API 接口        5. 用例质量复核         8. 人工纠正             11. Jira Bug 单
3. 配置 LLM 场景        6. 执行对话测试         9. 统计分析             12. 数据导出
```

## 准备阶段

| 步骤 | Prompt 模板 | LLM Key | 说明 |
|------|-------------|---------|------|
| 1. 创建用户画像 | `generate_persona_messages` | — | 按 persona 字段生成第一人称口语化消息，覆盖 categories，硬性轮换场景/情绪/起因/句式，禁模板腔 |
| 2. 配置 API 接口 | — | — | `api_endpoints` 表 + `.env`（非 LLM） |
| 3. 配置 LLM 场景 | — | — | `llm_config` 表，7 个 key：`case_gen` / `case_regenerate` / `fact_extract` / `eval_batch` / `eval_case` / `eval_realtime` / `case_review` |

## 测试阶段

| 步骤 | Prompt 模板 | LLM Key | 说明 |
|------|-------------|---------|------|
| 4. 生成测试用例 | `generate_test_cases`（首次）<br>`generate_test_cases_with_feedback`（带反馈重生成） | `case_gen` / `case_regenerate` | 按维度生成多轮 `【R1】【R2】` 用例，字段含 `case_id` / `input_text` / `expected_output` / `evaluation_points` / `failure_flags` / `priority`；硬约束事实不能虚构 |
| 5. 用例质量复核 | `review_case_quality` | `case_review` | LLM 审核用例是否符合维度要求；硬触发器：行为列表式 expected_output ≤3 分、跨维度 failure_flags ≤6 分、case_id 前缀不一致 ≤6 分、issues≥2 ≤6 分 |
| 6. 执行对话测试 | — | — | `_execute_task_worker` 调玩偶 API SSE 流，结果写入 `test_results`（非 LLM） |

## 评测阶段

| 步骤 | Prompt 模板 | LLM Key | 说明 |
|------|-------------|---------|------|
| 7. 自动评测打分 | `evaluate_test_case` | `eval_batch` / `eval_case` | 10 分制链式扣分：先列 `deduction_breakdown` 再算 `score = max(1, 10 - sum(points))`；`deduction_tags` 标注失败模式；红队用例同走此 prompt |
| 8. 人工纠正 | — | — | `POST /api/eval/<id>/correct` / `/api/test_results/<id>/correct` 写 `human_score`/`human_note`；按 `ref_id` 去重写 `eval_corrections` |
| 9. 统计分析 | — | — | trend / by_user / 分数段 / 扣分关键词；`COALESCE(human_score, total_score)` 优先人工分 |

## 产出阶段

| 步骤 | Prompt 模板 | LLM Key | 说明 |
|------|-------------|---------|------|
| 10. 测试报告 | — | — | HTML / Excel 双格式 + 统计 sheet |
| 11. Jira Bug 单 | — | — | 评测失败按 `ref_id` 去重创建 Bug |
| 12. 数据导出 | — | — | Excel 导出 `test_results` |

## 旁路 Prompt（非主流程，按需触发）

| Prompt 模板 | LLM Key | 触发点 |
|-------------|---------|--------|
| `evaluate_chat_reply` | `eval_realtime` | 实时聊天评测，4 维度 `memory` / `emotion` / `quality` / `persona`，每维度独立链式扣分 |
| `fact_extraction` | `fact_extract` | 聊天消息自动提取用户事实，13 个 category 英文键名，多实体 `entity_name`，`occurred_at` / `emotion_tag` / `related_to` 三字段 |
| `generate_redteam_case` | `case_gen` | 红队测试生成 — 13 类攻击 × 5 P0 维度（D2/D4/F1/F2/F3），含多轮陷阱链要求（多轮比例由 `multi_round_count` / `single_round_count` 控制） |

## Prompt 占位符与数据来源

所有 prompt 模板的占位符在 `pipi_api.py` 的对应函数内填充：

| 函数 | 填充的 Prompt |
|------|----------------|
| `generate_test_cases()` | `generate_test_cases` / `generate_test_cases_with_feedback` |
| `evaluate_test_case()` | `evaluate_test_case` |
| `evaluate_chat_reply()` | `evaluate_chat_reply` |
| `review_case_quality()` | `review_case_quality` |
| `extract_facts_from_message()` | `fact_extraction` |

维度专属内容来自 `pipi.json`：

| 数据字段 | 用途 |
|----------|------|
| `dimensions[]` | 22 个测试维度（A1-F3），含 `code` / `name` / `cluster_code` / `test_points` |
| `dimension_gen_requirements[dim_code]` | 各维度的轮数 / 结构 / 示例 / `must_avoid`，填充到 `dimension_block` |
| `dimension_review_checklist[dim_code]` | 各维度审核 checklist（`specific` + `hard_rules`） |
| `general_hard_rules` | 通用硬规则（永久承诺 / 亲昵称呼 / 虚构事实 / 假装真人 / 事实对应） |
| `redteam` | 红队配置：`enabled_dimensions` / `trap_types` / `attack_hints_by_dimension` / `general_hard_rules_with_failure_flags` |
| `fact_taxonomy` | 事实分类（13 个 category 的 valid fact_keys + multi_entity_fields） |

## 7 个 LLM 调用场景配置

`llm_config` 表为每个场景独立配置 4 个参数：

| Key | 场景 | 默认用途 |
|-----|------|---------|
| `case_gen` | 用例生成 | 按维度生成多轮测试用例 |
| `case_regenerate` | 用例重生成 | 带反馈的整维度重新生成 |
| `fact_extract` | 事实提取 | 从对话消息中提取用户事实 |
| `eval_batch` | 批量评测 | 测试任务结果的批量打分 |
| `eval_case` | 单条评测 | 独立用例评测 |
| `eval_realtime` | 实时评测 | 聊天回复 4 维度评测 |
| `case_review` | 用例复核 | 审核用例质量是否合格 |

调用方式（`pipi_api.py`）：

```python
llm_config = get_llm_config()
cases = pipi_api.generate_test_cases(
    dimension=dim, ..., **llm_config["case_gen"]
)
```
