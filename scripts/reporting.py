#!/usr/bin/env python3
"""Safe Markdown, HTML and offline star-map rendering."""

from __future__ import annotations

import html
import hashlib
import json
from pathlib import Path
from typing import Any

from exam_error_core import build_graph, compute_statistics, export_review_queue
from exam_error_app.contracts import ReportViewModel
from exam_error_app.report_projection import build_report_view


def _safe(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _md(value: Any) -> str:
    return _safe(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _replace_template(template: str, values: dict[str, str]) -> str:
    output = template
    for key, value in values.items():
        output = output.replace("{{" + key + "}}", value)
    return output


def _distribution_markdown(stats: dict[str, Any]) -> str:
    labels = {"knowledge": "知识", "cognitive": "认知", "error": "错因"}
    sections = []
    for dimension in ("knowledge", "cognitive", "error"):
        rows = stats["tag_distribution"][dimension]
        lines = [f"### {labels[dimension]}", "", "| 标签 | 次数 | 分母 | 比例 |", "|---|---:|---:|---:|"]
        if rows:
            for name, item in rows.items():
                lines.append(
                    f"| {_md(name)} | {item['count']} | {item['denominator']} | {item['percentage']:.2f}% |"
                )
        else:
            lines.append("| 无 | 0 | 0 | 0.00% |")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _overall_markdown(stats: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"- 作答数：{stats['total_responses']}",
            f"- 已评分：{stats['scored_responses']}；未评分：{stats['unscored_responses']}",
            (
                f"- 错误作答：{stats['incorrect_responses']}；错误率 "
                f"{stats['incorrect_rate']['value']:.2f}%"
                f"（{stats['incorrect_rate']['numerator']} / {stats['incorrect_rate']['denominator']}）"
            ),
            (
                f"- 得分：{stats['earned_score']} / {stats['possible_score']}；得分率 "
                f"{stats['score_rate']['value']:.2f}%"
            ),
            f"- 待复核：{stats['review_queue_count']}",
        ]
    )


def _combinations_markdown(stats: dict[str, Any]) -> str:
    combinations = stats["error_combinations"][:10]
    if not combinations:
        return "- 暂无足够的错因组合数据。"
    return "\n".join(
        f"- {_md(' + '.join(item['tags']))}：{item['count']} 次（分母 {item['denominator']}）"
        for item in combinations
    )


def _representative_markdown(view: ReportViewModel, limit: int = 8) -> str:
    lines = []
    for item in view.representative_errors[:limit]:
        lines.append(
            f"- `{_md(item.student_ref)}` / `{_md(item.question_id)}`："
            f"{_md(item.observed)}；{_md(item.explanation)}"
        )
    return "\n".join(lines) if lines else "- 暂无已确认错误证据。"


def _recommendations_markdown(stats: dict[str, Any]) -> str:
    recommendations = []
    knowledge = stats["tag_distribution"]["knowledge"]
    errors = stats["tag_distribution"]["error"]
    for name, item in list(knowledge.items())[:3]:
        recommendations.append(
            f"- 优先复习 `{_md(name)}`：覆盖 {item['count']} 个错误作答。"
        )
    for name, item in list(errors.items())[:3]:
        recommendations.append(
            f"- 针对 `{_md(name)}` 设计一次“识别错误—修正步骤—变式验证”活动（{item['count']} 次）。"
        )
    if stats["review_queue_count"]:
        recommendations.append(
            f"- 教学结论发布前先处理 {stats['review_queue_count']} 个待复核项目。"
        )
    return "\n".join(recommendations) if recommendations else "- 当前数据不足，先完成评分和错因标注。"


def _review_markdown(view: ReportViewModel) -> str:
    if not view.review_items:
        return "- 无待复核项目。"
    return "\n".join(
        f"- `{_md(item.get('attempt_id'))}` / `{_md(item.get('question_id'))}`："
        f"{_md(', '.join(item.get('reasons', [])))}"
        for item in view.review_items
    )


def _project_report(data: dict[str, Any], index_version: str = "unindexed") -> ReportViewModel:
    return build_report_view(
        data,
        statistics_builder=compute_statistics,
        graph_builder=build_graph,
        review_exporter=export_review_queue,
        index_version=index_version,
    )


def build_error_report_markdown(
    data: dict[str, Any] | ReportViewModel,
    template_path: str | Path,
) -> str:
    view = data if isinstance(data, ReportViewModel) else _project_report(data)
    stats = view.statistics
    template = Path(template_path).read_text(encoding="utf-8")
    return _replace_template(
        template,
        {
            "overall_metrics": _overall_markdown(stats),
            "tag_distributions": _distribution_markdown(stats),
            "error_combinations": _combinations_markdown(stats),
            "representative_errors": _representative_markdown(view),
            "recommendations": _recommendations_markdown(stats),
            "review_queue": _review_markdown(view),
        },
    )


def _top_items(distribution: dict[str, Any], limit: int = 5) -> list[tuple[str, dict[str, Any]]]:
    return list(distribution.items())[:limit]


def build_lesson_summary_markdown(
    data: dict[str, Any] | ReportViewModel,
    template_path: str | Path,
) -> str:
    view = data if isinstance(data, ReportViewModel) else _project_report(data)
    stats = view.statistics
    graph = view.graph
    priority = sorted(
        graph["nodes"],
        key=lambda item: (-float(item["lost_score"]), -item["review_count"], item["id"]),
    )[:8]
    knowledge = _top_items(stats["tag_distribution"]["knowledge"])
    errors = _top_items(stats["tag_distribution"]["error"])
    values = {
        "priority_questions": "\n".join(
            f"- `{_md(item['id'])}`：累计失分 {item['lost_score']}，待复核 {item['review_count']}"
            for item in priority
        )
        or "- 暂无。",
        "common_problems": "\n".join(
            f"- `{_md(name)}`：{item['count']} 个错误作答（分母 {item['denominator']}）"
            for name, item in errors
        )
        or "- 暂无稳定共性。",
        "teaching_actions": "\n".join(
            f"- 对 `{_md(name)}` 采用例题拆解、错误对照和当堂检验。"
            for name, _ in knowledge
        )
        or "- 先补齐评分与标签证据。",
        "classroom_activities": "\n".join(
            f"- 围绕 `{_md(name)}` 组织同伴诊断：指出第一处因果错误并说明修正依据。"
            for name, _ in errors[:3]
        )
        or "- 使用一题多解和步骤排序活动收集证据。",
        "differentiated_guidance": "\n".join(
            [
                "- 基础组：使用知识点最小练习集和逐步提示。",
                "- 巩固组：完成同构变式并解释第一处错误。",
                "- 提升组：比较多种策略并验证适用条件。",
            ]
        ),
        "homework_recommendations": "\n".join(
            f"- 为 `{_md(name)}` 安排 2 道基础题、2 道变式题和 1 道解释题。"
            for name, _ in knowledge[:3]
        )
        or "- 待知识标签补齐后生成针对性作业。",
    }
    template = Path(template_path).read_text(encoding="utf-8")
    return _replace_template(template, values)


def _html_table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{_safe(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_safe(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _distribution_html(stats: dict[str, Any]) -> str:
    labels = {"knowledge": "知识", "cognitive": "认知", "error": "错因"}
    output = []
    for dimension in ("knowledge", "cognitive", "error"):
        rows = [
            [name, item["count"], item["denominator"], f"{item['percentage']:.2f}%"]
            for name, item in stats["tag_distribution"][dimension].items()
        ]
        output.append(f"<h3>{labels[dimension]}</h3>")
        output.append(_html_table(["标签", "次数", "分母", "比例"], rows or [["无", 0, 0, "0.00%"]]))
    return "".join(output)


def _report_shell(title: str, body: str, shell_path: str | Path) -> str:
    shell = Path(shell_path).read_text(encoding="utf-8")
    nonce = hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
    return (
        shell.replace("{{TITLE}}", _safe(title))
        .replace("{{BODY}}", body)
        .replace("{{CSP_NONCE}}", nonce)
    )


def build_error_report_html(
    data: dict[str, Any] | ReportViewModel,
    shell_path: str | Path,
) -> str:
    view = data if isinstance(data, ReportViewModel) else _project_report(data)
    stats = view.statistics
    metrics = [
        ("作答数", stats["total_responses"]),
        (
            "已评分 / 未评分",
            f"{stats['scored_responses']} / {stats['unscored_responses']}",
        ),
        ("错误作答", stats["incorrect_responses"]),
        (
            "错误率",
            f"{stats['incorrect_rate']['value']:.2f}% "
            f"({stats['incorrect_rate']['numerator']} / "
            f"{stats['incorrect_rate']['denominator']})",
        ),
        (
            "得分 / 得分率",
            f"{stats['earned_score']} / {stats['possible_score']}；"
            f"{stats['score_rate']['value']:.2f}%",
        ),
        ("待复核", stats["review_queue_count"]),
    ]
    metric_html = "".join(
        f'<div class="metric"><span>{_safe(label)}</span><strong>{_safe(value)}</strong></div>'
        for label, value in metrics
    )
    combinations = _html_table(
        ["错因组合", "次数", "分母"],
        [
            [" + ".join(item["tags"]), item["count"], item["denominator"]]
            for item in stats["error_combinations"][:10]
        ]
        or [["暂无", 0, 0]],
    )
    review_rows = [
        [item["attempt_id"], item["question_id"], ", ".join(item["reasons"])]
        for item in view.review_items
    ]
    body = (
        "<h1>错因分析报告</h1>"
        f'<p class="muted">分析任务：{_safe(view.analysis_id)}</p>'
        f'<div class="metric-grid">{metric_html}</div>'
        "<h2>知识、认知与错因分布</h2>"
        + _distribution_html(stats)
        + '<p class="warning">多标签统计以错误作答数为分母，各标签比例之和可能超过 100%。</p>'
        + "<h2>常见错误组合</h2>"
        + combinations
        + "<h2>代表性证据</h2><ul>"
        + "".join(
            f"<li>{_safe(line[2:].replace('`', ''))}</li>"
            for line in _representative_markdown(view).splitlines()
        )
        + "</ul><h2>优先建议</h2><ul>"
        + "".join(
            f"<li>{_safe(line[2:].replace('`', ''))}</li>"
            for line in _recommendations_markdown(stats).splitlines()
        )
        + "</ul><h2>待复核项目</h2>"
        + _html_table(["作答", "题目", "原因"], review_rows or [["无", "", ""]])
    )
    return _report_shell("错因分析报告", body, shell_path)


def build_lesson_summary_html(
    data: dict[str, Any] | ReportViewModel,
    shell_path: str | Path,
) -> str:
    view = data if isinstance(data, ReportViewModel) else _project_report(data)
    stats = view.statistics
    graph = view.graph
    priority = sorted(
        graph["nodes"],
        key=lambda item: (-float(item["lost_score"]), -item["review_count"], item["id"]),
    )[:8]
    error_rows = [
        [name, item["count"], item["denominator"]]
        for name, item in _top_items(stats["tag_distribution"]["error"])
    ]
    knowledge = _top_items(stats["tag_distribution"]["knowledge"])
    body = (
        "<h1>课堂讲解内容归纳</h1>"
        f'<p class="muted">分析任务：{_safe(view.analysis_id)}</p>'
        "<h2>重点讲解题目</h2>"
        + _html_table(
            ["题目", "累计失分", "待复核"],
            [[item["id"], item["lost_score"], item["review_count"]] for item in priority]
            or [["暂无", 0, 0]],
        )
        + "<h2>共性问题</h2>"
        + _html_table(["错因", "次数", "分母"], error_rows or [["暂无", 0, 0]])
        + "<h2>教学行动</h2><ul>"
        + "".join(
            f"<li>对 {_safe(name)} 采用例题拆解、错误对照和当堂检验。</li>"
            for name, _ in knowledge
        )
        + "</ul><h2>课堂活动</h2><ul>"
        + "".join(
            f"<li>围绕 {_safe(name)} 组织同伴诊断并定位第一处因果错误。</li>"
            for name, _ in _top_items(stats["tag_distribution"]["error"], 3)
        )
        + "</ul><h2>学生分层指导</h2><ul>"
        "<li>基础组：知识点最小练习集和逐步提示。</li>"
        "<li>巩固组：完成同构变式并解释第一处错误。</li>"
        "<li>提升组：比较多种策略并验证适用条件。</li>"
        "</ul><h2>课后作业建议</h2><ul>"
        + "".join(
            f"<li>为 {_safe(name)} 安排 2 道基础题、2 道变式题和 1 道解释题。</li>"
            for name, _ in knowledge[:3]
        )
        + "</ul>"
    )
    return _report_shell("课堂讲解内容归纳", body, shell_path)


def render_star_map(graph: dict[str, Any], template_path: str | Path) -> str:
    template = Path(template_path).read_text(encoding="utf-8")
    graph_json = json.dumps(graph, ensure_ascii=False, separators=(",", ":"))
    graph_json = (
        graph_json.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    nonce = hashlib.sha256(graph_json.encode("utf-8")).hexdigest()[:32]
    return (
        template.replace("{{GRAPH_JSON}}", graph_json)
        .replace("{{CSP_NONCE}}", nonce)
    )


def write_report_bundle(
    data: dict[str, Any],
    output_dir: str | Path,
    assets_dir: str | Path,
    report_kind: str = "all",
    output_format: str = "both",
    index_version: str = "unindexed",
) -> dict[str, str]:
    if report_kind not in {"all", "error", "lesson", "graph"}:
        raise ValueError("report_kind must be all, error, lesson or graph")
    if output_format not in {"markdown", "html", "both"}:
        raise ValueError("output_format must be markdown, html or both")
    output_dir = Path(output_dir)
    assets_dir = Path(assets_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    view = _project_report(data, index_version=index_version)
    written: dict[str, str] = {}
    if report_kind in {"all", "error"}:
        if output_format in {"markdown", "both"}:
            path = output_dir / "error-report.md"
            path.write_text(
                build_error_report_markdown(view, assets_dir / "error-report-template.md"),
                encoding="utf-8",
            )
            written["error_report_markdown"] = str(path)
        if output_format in {"html", "both"}:
            path = output_dir / "error-report.html"
            path.write_text(
                build_error_report_html(view, assets_dir / "report-shell.html"),
                encoding="utf-8",
            )
            written["error_report_html"] = str(path)
    if report_kind in {"all", "lesson"}:
        if output_format in {"markdown", "both"}:
            path = output_dir / "lesson-summary.md"
            path.write_text(
                build_lesson_summary_markdown(view, assets_dir / "lesson-summary-template.md"),
                encoding="utf-8",
            )
            written["lesson_summary_markdown"] = str(path)
        if output_format in {"html", "both"}:
            path = output_dir / "lesson-summary.html"
            path.write_text(
                build_lesson_summary_html(view, assets_dir / "report-shell.html"),
                encoding="utf-8",
            )
            written["lesson_summary_html"] = str(path)
    if report_kind in {"all", "graph"}:
        graph = view.graph
        graph_path = output_dir / "star-map.json"
        graph_path.write_text(
            json.dumps(graph, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        html_path = output_dir / "star-map.html"
        html_path.write_text(
            render_star_map(graph, assets_dir / "star-map.html"),
            encoding="utf-8",
        )
        written["star_map_json"] = str(graph_path)
        written["star_map_html"] = str(html_path)
    review_path = output_dir / "review-queue.json"
    review_path.write_text(
        json.dumps(
            {
                "organization_id": view.organization_id,
                "analysis_id": view.analysis_id,
                "document_state_hash": view.document_state_hash,
                "items": list(view.review_items),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    written["review_queue"] = str(review_path)
    return written
