# 受控错因分类法

正式错因标签只使用下列名称；未覆盖的新错因或知识点先放入 `suggested_tags`，不得自动进入正式词表。

扩展流程使用 `taxonomy/extensions.json`：Agent 只可新增 `pending` 候选项；教师在 HTML 报告中导出审核决定后，使用 `scripts/exam_error_cli.py taxonomy apply` 写入 `approved` 或 `rejected` 状态。只有 `approved` 项可以在后续分析中复用，Skill 的基础词表不会被运行过程改写。

| 分组 | 标签 |
|---|---|
| 知识 | `concept-missing`, `concept-confusion`, `theorem-misuse`, `formula-misuse` |
| 推理 | `invalid-inference`, `condition-omitted`, `case-incomplete`, `strategy-mismatch` |
| 过程 | `calculation-error`, `sign-error`, `transformation-error`, `step-omitted` |
| 阅读 | `requirement-misread`, `condition-missed`, `diagram-misread` |
| 表达 | `unit-missing`, `notation-invalid`, `conclusion-incomplete`, `explanation-insufficient` |
| 回答 | `unanswered`, `illegible`, `answer-misaligned` |
| 系统 | `ocr-symbol-error`, `extraction-error`, `rubric-gap`, `unclassified` |

每个标签必须带置信度并引用至少一个 evidence ID。证据不足时使用 `unclassified`。不要把知识点名称放入 error 维度。

禁止从单个回答推断 `careless`、动机不足、时间管理差、能力低或类似行为解释；这些词不能作为正式标签。
