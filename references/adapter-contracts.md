# 外部能力适配器

## OCR/公式识别

OCR 适配器返回文档、页面和块；每个块包含文本、类型、bbox、坐标单位、置信度和来源哈希。公式识别额外返回规范化 LaTeX。适配器不得直接给出错因或最终分数。

## 符号等价

接口接收学生表达式、参考表达式和假设条件，返回 `equivalent: true|false|null`、置信度、规范化表达式、适配器 ID 和错误信息。解析失败必须返回 null，不得猜测。

## 嵌入

接口提供 `encode(texts)`、`dimension`、`model_id`、`model_fingerprint` 和 `license_id`。支持：

- sentence-transformers 本地目录
- ONNX 模型目录和本地 tokenizer
- 输入中已有的预计算向量

必须验证模型路径存在、禁止自动下载，并在加载前将完整目录 SHA-256 与部署允许列表中的 `--model-sha256` 做常量时间比较；缺失或不匹配时失败关闭。对输出做 L2 归一化并拒绝 NaN/维度变化，不得用兼容回退绕过 `local_files_only` 或 `trust_remote_code=false`。

索引只使用 SQLite FTS5 和结构化标签。运行时不会加载模型、建立向量索引或下载第三方模型。

## 外部服务门禁

默认 `allow_external=false`。启用外部服务时同时要求明确配置、提供器允许列表和审计记录；记录提供器、目的、字段范围和输入输出哈希，不记录密钥。
