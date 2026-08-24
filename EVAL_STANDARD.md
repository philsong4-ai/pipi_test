# 皮皮 AI 陪伴玩偶评测标准

> 本文档定义皮皮测试系统中三类评测（测试用例评测 / 聊天实时评测 / 用例质量复核）的统一积分规则。所有规则在 `pipi_api.py` 与 `interface_profiles/<target_api>.json` 中实现。

---

## 一、总体原则

### 1.1 链式 CoT 强制

所有评测场景都强制链式扣分：

```
score = max(1, round(10 - sum(deduction_breakdown[*].points)))
```

- LLM 必须先列 `deduction_breakdown`（每项 `{item, points}`），再用 `10 - sum(points)` 算分
- 解析端 `_parse_test_case_eval`（`pipi_api.py:1807`）强制重算，**忽略 LLM 自报的 score**
- 无 breakdown 时回退直接打分，并打印 `[COT] missing breakdown` 警告

### 1.2 通过线

| 阈值 | 状态 |
|---|---|
| `score >= 6` | `passed` |
| `score < 6` | `failed` |

贯穿测试结果、聊天评测、统计接口、前端配色（绿 `#34c759` / 红 `#ff3b30`）。

### 1.3 硬规则优先级

1. **维度硬规则禁区** → 直接判 `failed`（绕过链式扣分）
2. **failure_flags 触发** → 分数 ≤ 5
3. **链式扣分** → 主路径

### 1.4 幻觉判定

- AI 引用【已知用户信息】列表中的事实 → 不算幻觉
- 引用列表外的具体信息 → 视为虚构事实，扣分并打 `deduction_tags`

---

## 二、测试用例评测 `evaluate_test_case`

### 2.1 评测输入

| 字段 | 来源 | 作用 |
|---|---|---|
| `input_text` | 用例 | 用户输入（多轮用 `【Rn】` 标记） |
| `actual_output` | 执行结果 | AI 实际回复 |
| `expected_output` | 用例 | 10 分标杆回复 |
| `evaluation_points` | 用例 | 必查评估点清单（闭合集合） |
| `failure_flags` | 用例 | 扣分触发器清单（闭合集合） |
| `user_facts` | DB | 已知用户事实列表 |

### 2.2 10 分制档位标准

| 分数 | 触发条件 |
|---|---|
| **10** | 所有评估点高质量满足 + 无扣分点触发 + 自然流畅 |
| **8-9** | 评估点基本满足 + 无扣分触发 + 整体到位质量略逊 |
| **6-7** | 评估点大部分满足 + 无扣分触发 + 有明显不足（生硬/遗漏） |
| **4-5** | 评估点半数未满足 **或** 触发 1 个轻度扣分点 |
| **1-3** | 评估点过半数未满足 **或** 触发严重扣分点（说教/冷漠/编造事实） |

### 2.3 输出 JSON 结构

```json
{
  "deduction_breakdown": [
    {"item": "扣分项描述", "points": 1.5}
  ],
  "score": 7,
  "deduction_reason": "扣分原因或评价",
  "eval_points_check": {
    "评估点1": true,
    "评估点2": false
  },
  "failure_flags_triggered": ["触发的扣分点"],
  "deduction_tags": ["短标签1", "短标签2"]
}
```

### 2.4 字段语义

| 字段 | 集合类型 | 来源 | 用途 |
|---|---|---|---|
| `eval_points_check` | 闭合 | 用例预设 | 逐项 ✅/❌ 核对题目考点 |
| `failure_flags_triggered` | 闭合 | 用例预设 | 判定踩雷了哪几条 |
| `deduction_tags` | 开放 | LLM 自由产 | 跨用例扣分原因聚合统计 |

### 2.5 `deduction_tags` 要求

- 2-5 个短标签，每个 ≤ 6 字
- 描述本次回复**实际暴露的失败模式**
- 常见示例：`忘记事实 / 说教语气 / 越界承诺 / 套话模板 / 情绪冷漠 / 编造事实 / 回复过短 / 人设不符 / 拒绝生硬 / 幽默不当`
- 回复优秀无问题时返回空数组 `[]`

### 2.6 同义词归一化

LLM 自由产出的标签在统计接口 `/api/eval/stats/tags` 读取时按硬编码映射表 `_DEDUCTION_TAG_ALIASES`（`web_admin.py:4918`）归一化：

| 原始 | 标准标签 |
|---|---|
| 缺少追问 / 引导不足 / 互动不足 / 追问泛化 / 追问生硬 | 缺乏追问 |
| 未结合事实 / 信息遗漏 / 关联不足 / 未关联记忆 / 脱离记忆 | 忘记事实 |
| 记错事实 | 记错事实 |
| 未结合偏好 / 未用偏好 / 缺乏个性化 / 未关联喜好 | 忽略偏好 |
| 语气生硬 / 表达重复 | 表达生硬 |
| 共情不足 / 情绪平淡 / 缺乏共情 / 共情缺失 | 情绪冷漠 |
| 回复平淡 / 内容单薄 / 内容不全 | 回复过短 |
| 建议笼统 / 建议缺失 / 缺少建议 | 缺乏建议 |
| 格式残留 / 格式异常 | 格式瑕疵 |
| 越权承诺 | 越界承诺 |

发现新同义词需手改代码 + 重启服务。

---

## 三、聊天实时评测 `evaluate_chat_reply`

### 3.1 四维独立打分

每维度独立 10 分制，独立链式扣分。

| 维度 | code | 关注点 | 常见 tags |
|---|---|---|---|
| 记忆运用 | `memory` | 引用已知信息恰当、不堆砌、不捏造 | 忘记事实 / 记错事实 / 上下文断裂 |
| 情感回应 | `emotion` | 识别情绪、共情不说教 | 情绪冷漠 / 虚假共情 / 情绪错位 |
| 回复质量 | `quality` | 自然流畅、长度适中、有内容 | 说教语气 / 套话模板 / 回复过短 / 幽默不当 |
| 人设一致 | `persona` | 语气词自然、不做客服不说教、知分寸 | 人设不符 / 越界承诺 / 安全违规 |

### 3.2 链式扣分（每维度独立）

```
{dim}_score = max(1, round(10 - sum({dim}_deduction_breakdown[*].points)))
```

### 3.3 总分

```python
total_score = round(sum([memory, emotion, quality, persona]) / 4, 1)
```

### 3.4 通过线

`mean >= 6` → `passed`，否则 `failed`（`pipi_api.py:1920`）

### 3.5 输出 JSON 结构

```json
{
  "memory_deduction_breakdown": [{"item": "...", "points": 1.5}],
  "memory_score": 7,
  "memory_reason": "扣分原因或空",
  "memory_tags": ["短标签1"],
  "emotion_deduction_breakdown": [...],
  "emotion_score": 8,
  ...
  "persona_deduction_breakdown": [...],
  "persona_score": 6,
  "persona_tags": [...]
}
```

### 3.6 `*_tags` 要求

- 每维度 0-5 个短标签，每个 ≤ 6 字
- 描述该维度实际暴露的失败模式
- 无问题时返回 `[]`

---

## 四、客观记忆检查（规则化）

`check_memory_objective(actual_output, user_facts, user_message)` — 规则化 ground-truth 检查，作为 LLM 评测的提示。

### 4.1 输出

```json
{
  "total_count": 5,
  "mentioned_count": 3,
  "missed_facts": [
    {"category": "pet", "fact_key": "name", "entity_name": "豆豆", "fact_value": "英短猫"}
  ]
}
```

### 4.2 用途

- 注入 LLM 评测 prompt 的 `memory_check_text` 段
- 评测结果回包时合并到 `eval_detail.memory_objective_check`
- 前端详情弹窗显示 `客观记忆检查: 3/5` + 未提及清单

---

## 五、多评测员集成（judges 模式）

### 5.1 触发

`judges` 参数 ≥ 2 时启用，默认 3 个不同模型/温度顺序调用。

### 5.2 聚合

- **最终 score** = 所有 judge 的算术均值
- **status** = `mean >= 6` ? `passed` : `failed`
- **`deduction_tags`** = 所有 judge 标签的并集

### 5.3 分歧标记

- `judges_std >= 2.0`（`JUDGE_DISAGREEMENT_THRESHOLD`）→ 标记 `needs_review` 人工复核
- 经验值：3 个 judge 整数打分，std > 2 通常意味着分歧明显（如 8/4/8 → std=2.31）

### 5.4 详情存储

`eval_detail.judges_detail` 保存每个 judge 的 `{model, temperature, score, deduction_tags}`，前端详情弹窗以表格展示。

---

## 六、用例质量复核 `review_case_quality`

**审核用例本身写得好不好**，不是评测 AI 回复。用例生成后异步执行，不合格自动带反馈重生成最多 2 次。

### 6.1 硬触发器（命中即封顶）

| 上限 | 触发条件 |
|---|---|
| **≤ 6** (warning) | failure_flags 完全没含本维度专属错误 / evaluation_points 带序号前缀 / failure_flags 含跨维度冗余项 / case_id 前缀错 / issues ≥ 2 |
| **≤ 3** (failed) | 触发本维度硬规则禁区 / expected_output 是行为原则列表（"1. xxx；2. xxx"）/ expected_output 虚构用户事实 |

### 6.2 基准分（未触发硬触发器时参考）

| 分数 | 标准 |
|---|---|
| 10 | 完全符合通用+专属标准，无 issues |
| 7-9 | 基本合格，issues ≤ 1 且非硬触发器 |
| 4-6 | 需修改，有明显问题 |
| 1-3 | 触发硬规则禁区或不合格需重新生成 |

### 6.3 状态映射

| 分数 | 状态 |
|---|---|
| `>= 8` | `passed` |
| `4-7` | `warning` |
| `<= 3` | `failed` |

### 6.4 反误判规则

在判 `expected_output 引用了不存在的事实` 之前，必须逐条对照【用户已知事实】列表。以下不算虚构：

- 表述简化但指向同一事实（"最近追的剧" 简化为 "追剧" → 匹配）
- 已知事实的通勤/地址/年龄等信息的简化表达
- 已知事实中的人际关系描述简化

只有在已知事实列表中**完全找不到任何对应**时，才判为虚构事实。

---

## 七、人工纠正

### 7.1 优先级

- `auto_evaluation.human_score` 和 `test_results.human_score` 优先于自动 score
- 统计接口用 `COALESCE(human_score, total_score)` 取分
- 前端 badge 显示 ✏️ 标记 + 橙色 `#ff9500`

### 7.2 纠正后重算

人工纠正后按 `human_score >= 6` 重算 `status`（`web_admin.py:5493`），与自动评测同阈值。

### 7.3 纠正记录

- `human_note` 非空时写入 `eval_corrections` 表
- 按 `ref_id` 去重（同 item 多次纠正只保留最新一条）
- 字段：`eval_type / ref_id / dimension_code / user_input / ai_reply / auto_score / human_score / correction_reason`

### 7.4 Few-shot 注入

下次评测前 `_load_recent_corrections(eval_type, dimension_code, limit=5)` 加载最近的纠正记录，经 `_format_corrections_for_prompt()` 拼接到 system_prompt 末尾作为 few-shot 案例。

---

## 八、配置入口

### 8.1 LLM 配置（7 个场景独立）

通过 `get_llm_config()` 读取，每个 key 独立配置 `{model, temperature, max_tokens, timeout}`：

| key | 用途 | 默认温度 |
|---|---|---|
| `case_gen` | 用例生成 | 0.3 |
| `case_regenerate` | 用例重生成 | 0.3 |
| `fact_extract` | 事实提取 | 0.3 |
| `eval_batch` | 批量评测 | 0 |
| `eval_case` | 单用例评测 | 0 |
| `eval_realtime` | 聊天实时评测 | 0 |
| `case_review` | 用例复核 | 0.3 |

### 8.2 并发闸

- `api_global` (max=8)：跨 gunicorn worker 共享 MySQL 行锁原子计数，玩偶 API 调用前抢
- `aivs_global` (max=1)：AIVS Java SDK 串行
- `llm_global` (max=16)：LLM 调用并发
- `_EVAL_SEMAPHORE` (max=8)：进程内评测线程信号量

### 8.3 Prompt 模板

按 `target_api` 切换：`interface_profiles/<target_api>.json` 的 `prompts.evaluate_test_case / evaluate_chat_reply / review_case_quality`。缺失时回退到 `pipi.json`。

---

## 九、评分速查表

| 场景 | 维度 | 通过线 | 强制 CoT | 硬规则优先 |
|---|---|---|---|---|
| 测试用例评测 | 单维度 10 分 | ≥6 passed | ✅ | ✅ |
| 聊天实时评测 | 4 维各 10 分取均值 | mean≥6 passed | ✅（每维独立） | ✅ |
| 用例质量复核 | 单维度 10 分 | ≥8 passed / 4-7 warning / ≤3 failed | ❌（先列 issues 再算分） | ✅ |
| 多评测员集成 | N judge 均值 | mean≥6 passed | ✅（每 judge 独立） | ✅ |

---

## 十、参考实现位置

| 规则 | 文件 | 行号 |
|---|---|---|
| 链式扣分重算 | `pipi_api.py` | 1807-1837 |
| 测试用例评测 prompt | `interface_profiles/pipi.json` | 494-496 |
| 聊天评测 prompt | `interface_profiles/pipi.json` | 498-500 |
| 用例复核 prompt | `interface_profiles/pipi.json` | 508-510 |
| 通过线判定 | `pipi_api.py` | 1824, 1920 |
| 标签归一化映射 | `web_admin.py` | 4918-4940 |
| 多评测员聚合 | `pipi_api.py` | 1920-1930 |
| 人工纠正重算 status | `web_admin.py` | 5493 |
| eval_detail 序列化 | `web_admin.py` | 5001-5020 |
| 并发闸实现 | `web_admin.py` | `_acquire_slot / _release_slot` |
