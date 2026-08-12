# 解耦架构与扩展边界

## 依赖方向

只允许以下单向依赖：

```text
CLI（组合根）
  -> application pipeline / contracts
  -> domain adapters
  -> reporting adapter
  -> retrieval adapter

reporting adapter -> ReportViewModel
retrieval adapter -> IndexRecord
domain adapters -> contracts
contracts -> 无项目内依赖
```

教师单文件报告使用更严格的单向链路：

```text
Agent / 输入文件
  -> AnalysisPipeline
  -> teacher_report_projection（唯一可读取分析文档的报告适配器）
  -> Teacher Report View v1（稳定契约）
  -> teacher_report_renderer（只读取 View）
  -> 锁定的 teacher-report.html
  -> CLI 文件输出
```

`TeacherReportApplication` 只编排上述端口，不读取模板、不写文件、不计算报告数据。CLI 是唯一组合根和文件输出层。

禁止 application、评分、审计、报告投影或检索投影反向导入 CLI。报告渲染器不得计算分数或统计；数据库模块不得遍历完整分析文档。

## 稳定契约

- `AnalysisPipeline`：只协调迁移、校验、分析、审计、统计和图谱端口。
- `ObjectiveGradingResult`：评分器返回值；评分器不写文件、不生成报告、不访问数据库。
- `ReportViewModel`：报告渲染器的唯一领域输入。
- `Teacher Report View`：教师单文件 HTML 的唯一渲染输入；包含显式版本并拒绝未知顶层字段。
- `teacher-report.html`：通过版本和 SHA-256 锁定；渲染器拒绝未经版本升级的模板变更。
- `IndexRecord`：检索与数据库层的唯一分析输入。
- `AuditChainService`：通过注入哈希、时钟和事件 ID 生成器保持可测试性。

## 扩展规则

- 新题型：只修改评分服务、规则验证和对应测试。
- 新报告格式：只消费 `ReportViewModel`，不读取原始 attempts。
- 教师报告界面：只修改锁定模板并升级渲染器与模板版本，不修改分析流水线；只有教师视图字段变化时才升级契约版本。
- 新检索后端：只消费 `IndexRecord`，不导入评分或报告模块。
- 新审计存储：实现审计端口，不修改评分器。
- 新数据版本：在迁移适配器处理；保持 application pipeline 不变。

## 兼容层

`scripts/exam_error_core.py` 保留旧公共函数，供既有调用方使用。新增模块应优先依赖 `scripts/exam_error_app/` 中的契约，不得继续扩大兼容层职责。
