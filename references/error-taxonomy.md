# 受控错因分类法

正式错因标签只使用下列名称；未覆盖的新标签先放入 `suggested_tags`，不得自动进入正式词表。

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
