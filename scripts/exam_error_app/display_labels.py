"""Canonical teacher-facing Chinese labels for internal protocol codes."""

from __future__ import annotations


ERROR_LABELS = {
    "concept-missing": "概念缺失",
    "concept-confusion": "概念混淆",
    "theorem-misuse": "定理误用",
    "formula-misuse": "公式误用",
    "invalid-inference": "推理无效",
    "condition-omitted": "条件遗漏",
    "case-incomplete": "分类不完整",
    "strategy-mismatch": "策略不匹配",
    "calculation-error": "计算错误",
    "sign-error": "符号错误",
    "transformation-error": "变形错误",
    "step-omitted": "步骤遗漏",
    "requirement-misread": "题意误读",
    "condition-missed": "条件漏读",
    "diagram-misread": "图形误读",
    "unit-missing": "单位缺失",
    "notation-invalid": "书写符号不规范",
    "conclusion-incomplete": "结论不完整",
    "explanation-insufficient": "说明不充分",
    "unanswered": "未作答",
    "illegible": "字迹无法识别",
    "answer-misaligned": "题答错位",
    "ocr-symbol-error": "OCR 符号识别错误",
    "extraction-error": "内容抽取错误",
    "rubric-gap": "评分标准缺口",
    "unclassified": "暂未分类",
}

KNOWLEDGE_LABELS = {
    "math/number-and-operation": "数与运算",
    "math/algebra/expression": "代数式",
    "math/algebra/expression/identity": "乘法公式与恒等变形",
    "math/algebra/equation/linear": "一元一次方程",
    "math/algebra/equation/quadratic": "一元二次方程",
    "math/function": "函数",
    "math/geometry": "几何",
    "math/geometry/triangle/area": "三角形面积",
    "math/statistics-and-probability": "统计与概率",
    "math/statistics/mean": "平均数",
}

REVIEW_REASON_LABELS = {
    "ocr_math_symbol_changed": "数学符号 OCR 识别可能变化",
    "multiple_valid_answers": "可能存在多个有效答案",
    "rubric_method_uncovered": "评分标准未覆盖该解法",
    "rubric_gap": "评分标准存在缺口",
    "answer_misaligned": "题目与答案可能错位",
    "critical_field_conflict": "关键字段存在冲突",
    "reference_answer_missing": "缺少参考答案",
    "numeric_parse_failed": "数值答案无法解析",
    "symbolic_equivalence_unavailable": "符号等价校验能力不可用",
    "subjective_requires_rubric": "主观题缺少评分点",
    "subjective_teacher_confirmation_required": "主观题需要教师确认",
    "ocr_confidence_missing": "缺少 OCR 置信度",
    "tagging_confidence_missing": "缺少错因标签置信度",
    "low_ocr_confidence": "OCR 置信度偏低",
    "low_grading_confidence": "评分置信度偏低",
    "low_tagging_confidence": "错因标签置信度偏低",
    "unit_mismatch": "答案单位不匹配",
    "unanswered": "学生未作答",
    "migrated_from_v1": "由旧版数据迁移，建议确认",
    "teacher_confirmation_required": "需要教师确认",
}

REVIEW_STATUS_LABELS = {
    "unreviewed": "未复核",
    "provisional": "暂定结果",
    "needs_review": "需要复核",
    "auto_confirmed": "系统已确认",
    "teacher_confirmed": "教师已确认",
    "rejected": "已驳回",
}

QUESTION_TYPE_LABELS = {
    "single_choice": "单项选择题",
    "multiple_choice": "多项选择题",
    "true_false": "判断题",
    "fill_blank": "填空题",
    "numeric": "数值题",
    "formula": "公式题",
    "ordered_steps": "步骤题",
    "subjective": "主观题",
}

SUBJECT_LABELS = {
    "math": "数学",
    "chinese": "语文",
    "english": "英语",
    "physics": "物理",
    "chemistry": "化学",
    "biology": "生物",
    "history": "历史",
    "geography": "地理",
    "politics": "道德与法治",
}

GRADE_LABELS = {
    "grade-1": "一年级",
    "grade-2": "二年级",
    "grade-3": "三年级",
    "grade-4": "四年级",
    "grade-5": "五年级",
    "grade-6": "六年级",
    "grade-7": "七年级",
    "grade-8": "八年级",
    "grade-9": "九年级",
    "grade-10": "高一",
    "grade-11": "高二",
    "grade-12": "高三",
}

GRADE_ALIASES = {
    "小学一年级": "一年级",
    "小学二年级": "二年级",
    "小学三年级": "三年级",
    "小学四年级": "四年级",
    "小学五年级": "五年级",
    "小学六年级": "六年级",
    "初一": "七年级",
    "初二": "八年级",
    "初三": "九年级",
    **{label: label for label in GRADE_LABELS.values()},
}


def error_label(code: str) -> str:
    return ERROR_LABELS.get(code, "其他错因")


def knowledge_label(code: str) -> str:
    return KNOWLEDGE_LABELS.get(code, "自定义知识点")


def review_reason_label(code: str) -> str:
    return REVIEW_REASON_LABELS.get(code, "其他需要人工确认的情况")


def review_status_label(code: str | None) -> str:
    return REVIEW_STATUS_LABELS.get(code or "", "状态待确认")


def question_type_label(code: str | None) -> str:
    return QUESTION_TYPE_LABELS.get(code or "", "其他题型")


def question_label(number: int | None) -> str:
    return f"第 {number} 题" if number is not None else "题号待确认"


def subject_label(code: str | None) -> str:
    value = (code or "").strip()
    if value in SUBJECT_LABELS:
        return SUBJECT_LABELS[value]
    for label in SUBJECT_LABELS.values():
        if value == label or value.endswith(label):
            return label
    return "其他学科"


def grade_label(code: str | None) -> str:
    value = (code or "").strip()
    return GRADE_LABELS.get(value, GRADE_ALIASES.get(value, "年级待确认"))
