# 标签与关系系统

## 三个维度

- `knowledge`：课程知识点，使用层次路径，例如 `math/algebra/equation/quadratic`。
- `cognitive`：`remember|understand|apply|analyze|evaluate|create`。
- `error`：只使用 `error-taxonomy.md` 中的受控错因。

题目定义保存 knowledge/cognitive；学生 response 保存 error，避免把题目属性与学生错误混在一起。机构自定义知识目录使用 `org/<organization_id>/...` 命名空间，并记录目录版本。

## 通用数学基线

内置路径至少覆盖：

- `math/number-and-operation`
- `math/algebra/expression`
- `math/algebra/equation/linear`
- `math/algebra/equation/quadratic`
- `math/function`
- `math/geometry`
- `math/statistics-and-probability`

## 题目关系

使用加权 Jaccard 计算标签相似度，语义使用余弦相似度：

```text
0.50 * 知识相似度
+ 0.25 * 认知相似度
+ 0.15 * 错因相似度
+ 0.10 * 语义相似度
```

缺失某个维度时只对可用权重重新归一化。标签置信度参与加权 Jaccard，但不得与最终关系置信度混为一个值。
