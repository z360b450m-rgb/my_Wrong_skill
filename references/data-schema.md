# 数据协议 v2

正式机器协议见 `references/exam-analysis-v2.schema.json`。所有 JSON 使用 UTF-8，时间使用带时区的 ISO 8601，ID 只允许字母、数字、点、下划线和连字符。

## 根对象

`schema_version` 固定为 `2.0`。根对象包含：

- `organization_id`：机构标识；每个机构使用独立索引数据库。
- `analysis_id`：一次分析任务的稳定标识。
- `created_at`：任务创建时间。
- `paper`：不随学生变化的试卷定义。
- `attempts`：一个或多个学生作答；使用稳定 `student_ref` 关联，可携带本地展示字段 `student_name`。
- `provenance`：输入文件、OCR/模型/适配器来源。
- `review_queue`：需要教师处理的项目。
- `audit_log`：按顺序连接的哈希审计事件；末事件使用 `state_hash` 绑定除审计日志外的完整当前文档状态。

## 试卷与题目

`paper` 包含 `paper_id`、`subject`、`grade`、`curriculum_version`、`max_score` 和 `questions`。每个题目包含：

- `question_id`、`question_type`、`question_text`、`max_score`
- `reference_answer`：未知时为 `null`
- `rubric_points`：主观题评分点；每项包含稳定 ID、说明和分值
- `difficulty`：`easy|medium|hard|unknown`
- `tags`：只放题目固有的 `knowledge` 和 `cognitive` 标签
- `source`：可选的统一来源对象
- `semantic_embedding`：可选的预计算向量，不得混用不同模型

题型支持 `single_choice`、`multiple_choice`、`true_false`、`fill_blank`、`numeric`、`formula`、`ordered_steps` 和 `subjective`。

## 作答

每个 attempt 包含 `attempt_id`、可空 `class_id`、稳定 `student_ref`、可选 `student_name`、`submitted_at` 和 `responses`。姓名不能代替 `student_ref` 作为唯一键。response 包含：

- `question_id`
- `raw_ocr_text` 与 `normalized_answer`
- `score` 与派生的 `is_correct`
- `confidence.ocr|grading|tagging`
- `review_status`
- `rubric_results`
- `first_error_step`
- `evidence`
- `error_tags`
- `source`
- 可选 `teacher_review`

`review_status` 只能为 `unreviewed`、`provisional`、`needs_review`、`auto_confirmed`、`teacher_confirmed` 或 `rejected`。

运行时契约与 JSON Schema 使用相同资源边界：单文档最多 10,000 道题、100,000 次作答，每次作答最多 10,000 个响应，每个响应最多 1,000 条证据，语义向量最多 8,192 维，普通文本最多 100,000 字符。答案只允许标量、标量数组或 `null`，不得使用对象或嵌套数组。

## 证据和来源

证据包含 `evidence_id`、`observed`、`explanation`、`causal_role`、可空 `step`、`confidence` 和 `source`。`causal_role` 为 `causal|consequential|context`。

来源统一包含：

```json
{
  "document_id": "doc-001",
  "page": 1,
  "bbox": [10, 20, 300, 180],
  "bbox_unit": "pixel",
  "raw_text": "原始片段",
  "file_hash": "sha256:..."
}
```

字段无法获得时保留对象并使用 `null`，不要伪造坐标或哈希。

## 不变量

- `paper.max_score` 等于题目满分之和。
- `0 <= score <= question.max_score`；使用十进制运算。
- 当 score 非空时，`is_correct` 必须等于 `score == max_score`。
- response 必须且只能引用本试卷中的一个 question。
- 同一题目/作答/证据 ID 不得重复。
- 标签维度、名称和置信度必须有效；置信度为 `null` 或 0 到 1。
- 任何低置信度、主观题或结构冲突必须进入复核队列。

## v1 迁移

v1 根对象映射为一个 paper 和一个 attempt。原 `questions[]` 中的题目定义与学生作答拆开；缺失字段写 `null`；原对象 SHA-256、迁移警告和迁移事件写入 provenance/audit。对 v2 再次迁移必须保持内容不变。
