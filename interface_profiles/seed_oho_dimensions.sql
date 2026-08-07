-- OHO 维度 seed SQL
-- 在 test_dimensions 表中插入 O1-O8 维度（target_api='oho'）
-- 幂等：先按 target_api='oho' 删除，再插入

DELETE FROM test_dimensions WHERE target_api = 'oho';

INSERT INTO test_dimensions (target_api, dimension_code, dimension_name, cluster_code, cluster_name, test_points)
VALUES
  ('oho', 'O1', '灵感捕捉质量', 'VALUE', '价值捕获', '闪念即时记录、半成形想法接住、不要求当下整理完、保留原始语气'),
  ('oho', 'O2', '录音接住质量', 'VALUE', '价值捕获', '会议要点提取、电话结论沉淀、访谈原话留存、不丢失关键判断'),
  ('oho', 'O3', '笔记推进能力', 'ACTION', '后续推进', '行动项提取、后续提醒生成、与既有内容串联、写作素材池构建'),
  ('oho', 'O4', '多模态理解', 'INPUT', '输入处理', 'ASR 转写纠错、说话人区分、时间戳对齐、口语化表达容错'),
  ('oho', 'O5', '记忆召回与关联', 'MEMORY', '长期记忆', '跨记录检索、语义相似召回、时间线索回拉、主题串联'),
  ('oho', 'O6', '隐私与可控边界', 'SAFETY', '安全边界', '敏感信息识别、导出/删除可控、同步开关尊重、额度用尽不锁已有记忆'),
  ('oho', 'O7', '对话归档质量', 'ARCHIVE', '归档整理', 'chat 会话结束归档、对话记录 .md 生成、小结与历史对话分离、可后续搜索'),
  ('oho', 'O8', '标签与分类智能', 'ORG', '组织检索', '自动标签建议、系统标签与自定义标签协同、批量绑定准确性、按标签筛选');
