# 运行时能力与一键安装

在对外接入、部署排查或解释降级行为前，先运行：

```text
python scripts/exam_error_cli.py capabilities
```

该命令返回当前实例的能力 JSON，包括：

- `mode`：`core`、`math`、`semantic`、`full` 或 `blocked`
- `capabilities`：核心命令、SymPy、HNSW、句向量运行时是否可用
- `degraded_components`：当前降级的组件，例如 `sympy_equivalence`、`semantic_retrieval`
- `commands`：推荐的安装与复核命令

## 一键安装

推荐安装命令：

```text
python -m scripts.install_runtime
```

这会安装推荐的本地增强运行时：

- `sympy`
- `numpy`
- `usearch`
- `sentence-transformers`

如果希望使用 ONNX 推理而不是 sentence-transformers：

```text
python -m scripts.install_runtime --profile full-onnx
```

如果只想补数学符号等价：

```text
python -m scripts.install_runtime --profile math
```

只查看计划而不真正安装：

```text
python -m scripts.install_runtime --dry-run --json
```

## 仍需手工准备的内容

一键安装只补 Python 运行时，不会自动下载模型文件。启用语义检索时，仍需由部署方准备本地模型目录，并在索引或搜索时显式提供：

- `--embedding-provider sentence-transformers` 或 `--embedding-provider onnx`
- `--model-path <本地模型目录>`
- `--model-license <许可证标识>`
- `--model-sha256 <批准的完整模型目录 SHA-256>`

安装完成后，再次运行：

```text
python scripts/exam_error_cli.py capabilities
```

确认 `degraded_components` 中不再包含目标组件。

## 内存阈值与固态落盘

CLI 在解析前检查输入 JSON 大小，默认上限为 256 MB；通过 `EXAM_ERROR_MAX_INPUT_MB` 设置部署上限。分析契约同时限制题目、作答、证据、文本和向量维度，超限必须在进入评分、报告或索引前失败。

索引投影记录和 JSON 输出使用可滚动缓冲区，默认在内存累计到 256 MB 后转存到本地固态目录。通过 `index ... --memory-threshold-mb <MB> --spill-dir <目录>` 按任务配置，或使用环境变量 `EXAM_ERROR_MEMORY_THRESHOLD_MB`、`EXAM_ERROR_SPILL_DIR` 设置部署默认值。阈值最低为 1 MB。

落盘是中间数据保护机制，不代表对整个 Python 进程实施硬内存配额；解析后的分析文档、SQLite/HNSW 运行时和第三方模型仍会占用内存。宿主仍应设置进程级内存限制、磁盘配额、目录访问控制和磁盘加密。
