"""报告生成模块 - 多种输出格式、任务清单导出"""
from __future__ import annotations

import csv
import io
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .config import CheckConfig
from .utils import CheckResult, Issue, IssueType, Severity, FirstOccurrence, SuspectedSynonym, format_time, relative_path


SEVERITY_ICONS = {
    Severity.ERROR: "❌",
    Severity.WARNING: "⚠️",
    Severity.INFO: "ℹ️",
    Severity.SUGGESTION: "💡",
}

SEVERITY_LABELS = {
    Severity.ERROR: "错误",
    Severity.WARNING: "警告",
    Severity.INFO: "信息",
    Severity.SUGGESTION: "建议",
}

ISSUE_TYPE_LABELS = {
    IssueType.CHAPTER_NUMBER_GAP: "章节编号缺失",
    IssueType.CHAPTER_NUMBER_DUPLICATE: "章节编号重复",
    IssueType.DUPLICATE_HEADING: "重复标题",
    IssueType.MISSING_IMAGE_ALT: "图片缺少说明",
    IssueType.BROKEN_LINK: "引用失效",
    IssueType.TERM_INCONSISTENT: "术语不一致",
    IssueType.TERM_ALIAS_FOUND: "术语别名",
    IssueType.SUSPECTED_SYNONYM: "疑似同义词",
    IssueType.UNKNOWN_TERM: "未知术语",
}

TYPE_EMOJI_MAP = {
    "error": "🔴",
    "warning": "🟡",
    "info": "🔵",
    "suggestion": "🟢",
}


@dataclass
class TaskItem:
    """可分配给编辑的任务条目"""
    id: str
    severity: str
    type: str
    title: str
    description: str
    file_path: str
    line_number: Optional[int]
    assignee: str = ""
    status: str = "open"
    priority: str = "medium"
    suggestion: str = ""
    context: str = ""
    tags: List[str] = None
    created_at: str = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.created_at is None:
            self.created_at = datetime.now().isoformat(timespec="seconds")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "type": self.type,
            "title": self.title,
            "description": self.description,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "assignee": self.assignee,
            "status": self.status,
            "priority": self.priority,
            "suggestion": self.suggestion,
            "context": self.context,
            "tags": self.tags,
            "created_at": self.created_at,
        }


class ReportGenerator:
    """报告生成器"""

    def __init__(self, config: Optional[CheckConfig] = None):
        self.config = config or CheckConfig()

    def generate(self, result: CheckResult, fmt: str = "console", output: Optional[Path] = None) -> str:
        """生成报告"""
        fmt = fmt.lower()
        generators = {
            "console": self._to_console,
            "text": self._to_console,
            "json": self._to_json,
            "yaml": self._to_yaml,
            "yml": self._to_yaml,
            "markdown": self._to_markdown,
            "md": self._to_markdown,
            "html": self._to_html,
            "csv": self._to_csv,
            "tasks": self._to_tasks_markdown,
            "tasklist": self._to_tasks_markdown,
        }
        generator = generators.get(fmt, self._to_console)
        content = generator(result)

        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            with open(output, "w", encoding="utf-8") as f:
                f.write(content)
        return content

    def _to_console(self, result: CheckResult) -> str:
        """控制台格式输出"""
        lines = []
        lines.append("=" * 70)
        lines.append("📋 文档一致性检查报告")
        lines.append("=" * 70)
        lines.append("")
        lines.append(f"📁 扫描文件数: {len(result.files_scanned)}")
        lines.append(f"⏱️  耗时: {format_time(result.duration)}")
        lines.append("")
        counts = result.issue_count
        total = sum(counts.values())
        lines.append(f"📊 问题统计: 总计 {total} 个")
        for sev, icon in SEVERITY_ICONS.items():
            cnt = counts[sev.value]
            if cnt > 0:
                lines.append(f"   {icon} {SEVERITY_LABELS[sev]:>4s}: {cnt}")
        lines.append("")

        if result.issues:
            lines.append("-" * 70)
            lines.append("🔍 问题详情")
            lines.append("-" * 70)
            lines.append("")

            by_severity: Dict[Severity, List[Issue]] = defaultdict(list)
            for issue in result.issues:
                by_severity[issue.severity].append(issue)

            for sev in [Severity.ERROR, Severity.WARNING, Severity.INFO, Severity.SUGGESTION]:
                issues = by_severity.get(sev, [])
                if not issues:
                    continue
                lines.append(f"{SEVERITY_ICONS[sev]} {SEVERITY_LABELS[sev]} ({len(issues)})")
                lines.append("")
                for i, issue in enumerate(issues, 1):
                    loc = issue.location or "N/A"
                    type_label = ISSUE_TYPE_LABELS.get(issue.type, issue.type.value)
                    lines.append(f"  {i}. [{type_label}] {issue.message}")
                    lines.append(f"     📍 {loc}")
                    if issue.suggestion:
                        lines.append(f"     💡 建议: {issue.suggestion}")
                    lines.append("")
        else:
            lines.append("✅ 未发现任何问题！")
            lines.append("")

        if result.suspected_synonyms:
            lines.append("-" * 70)
            lines.append("🤔 疑似同义词组")
            lines.append("-" * 70)
            lines.append("")
            for i, syn in enumerate(result.suspected_synonyms, 1):
                terms_display = " / ".join(
                    f"\"{t}\"({syn.occurrences_count.get(t, 0)}次)" for t in syn.terms
                )
                lines.append(f"  {i}. {terms_display}")
                lines.append(f"     相似度: {syn.similarity:.0%}")
                lines.append("")

        if result.first_occurrences:
            lines.append("-" * 70)
            lines.append("📍 术语首次出现位置")
            lines.append("-" * 70)
            lines.append("")
            sorted_terms = sorted(result.first_occurrences.items())
            display_count = min(30, len(sorted_terms))
            for i, (term, occ) in enumerate(sorted_terms[:display_count], 1):
                rel_path = relative_path(occ.file_path)
                lines.append(f"  {i:3d}. \"{term}\"")
                lines.append(f"       → {rel_path}:{occ.line_number}")
                if occ.context:
                    ctx = occ.context.replace("\n", " ")[:60]
                    lines.append(f"       {ctx}")
                lines.append("")
            if len(sorted_terms) > display_count:
                lines.append(f"  ... 还有 {len(sorted_terms) - display_count} 个术语，使用 --format json/yaml 查看完整列表")
                lines.append("")

        return "\n".join(lines)

    def _to_json(self, result: CheckResult) -> str:
        """JSON格式输出"""
        return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)

    def _to_yaml(self, result: CheckResult) -> str:
        """YAML格式输出"""
        return yaml.dump(result.to_dict(), allow_unicode=True, default_flow_style=False, sort_keys=False)

    def _to_markdown(self, result: CheckResult) -> str:
        """Markdown格式报告"""
        lines = []
        lines.append("# 📋 文档一致性检查报告")
        lines.append("")
        lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**扫描文件数**: {len(result.files_scanned)}")
        lines.append(f"**耗时**: {format_time(result.duration)}")
        lines.append("")

        counts = result.issue_count
        total = sum(counts.values())
        lines.append("## 📊 问题统计")
        lines.append("")
        lines.append("| 级别 | 数量 |")
        lines.append("|------|------|")
        for sev in [Severity.ERROR, Severity.WARNING, Severity.INFO, Severity.SUGGESTION]:
            lines.append(f"| {TYPE_EMOJI_MAP[sev.value]} {SEVERITY_LABELS[sev]} | {counts[sev.value]} |")
        lines.append(f"| **总计** | **{total}** |")
        lines.append("")

        if result.issues:
            lines.append("## 🔍 问题详情")
            lines.append("")
            lines.append("| # | 级别 | 类型 | 描述 | 文件:行 | 建议 |")
            lines.append("|---|------|------|------|---------|------|")
            for i, issue in enumerate(result.issues, 1):
                sev_icon = TYPE_EMOJI_MAP[issue.severity.value]
                type_label = ISSUE_TYPE_LABELS.get(issue.type, issue.type.value)
                loc = issue.location or "-"
                msg = issue.message.replace("|", "\\|")
                sug = issue.suggestion.replace("|", "\\|") if issue.suggestion else "-"
                lines.append(f"| {i} | {sev_icon} | {type_label} | {msg} | `{loc}` | {sug} |")
            lines.append("")

        if result.suspected_synonyms:
            lines.append("## 🤔 疑似同义词组")
            lines.append("")
            lines.append("| # | 术语组 (出现次数) | 相似度 |")
            lines.append("|---|-------------------|--------|")
            for i, syn in enumerate(result.suspected_synonyms, 1):
                terms_str = "<br>".join(
                    f"`{t}` ({syn.occurrences_count.get(t, 0)})" for t in syn.terms
                )
                lines.append(f"| {i} | {terms_str} | {syn.similarity:.0%} |")
            lines.append("")

        if result.first_occurrences:
            lines.append("## 📍 术语首次出现位置")
            lines.append("")
            lines.append("| 术语 | 文件 | 行号 | 上下文 |")
            lines.append("|------|------|------|--------|")
            for term, occ in sorted(result.first_occurrences.items()):
                rel_path = relative_path(occ.file_path)
                ctx = occ.context.replace("\n", " ").replace("|", "\\|")[:80]
                lines.append(f"| `{term}` | `{rel_path}` | {occ.line_number} | {ctx} |")
            lines.append("")

        if result.files_scanned:
            lines.append("## 📁 扫描的文件")
            lines.append("")
            for fp in result.files_scanned:
                lines.append(f"- `{relative_path(fp)}`")
            lines.append("")

        return "\n".join(lines)

    def _to_html(self, result: CheckResult) -> str:
        """HTML格式报告"""
        counts = result.issue_count
        total = sum(counts.values())

        issue_rows = ""
        for i, issue in enumerate(result.issues, 1):
            sev_label = SEVERITY_LABELS.get(issue.severity, issue.severity.value)
            sev_class = f"sev-{issue.severity.value}"
            type_label = ISSUE_TYPE_LABELS.get(issue.type, issue.type.value)
            loc = issue.location or "-"
            issue_rows += f"""
            <tr class="{sev_class}">
                <td>{i}</td>
                <td><span class="badge badge-{issue.severity.value}">{sev_label}</span></td>
                <td>{type_label}</td>
                <td>{_escape_html(issue.message)}</td>
                <td><code>{_escape_html(loc)}</code></td>
                <td>{_escape_html(issue.suggestion) if issue.suggestion else '-'}</td>
            </tr>"""

        synonym_rows = ""
        for i, syn in enumerate(result.suspected_synonyms, 1):
            terms_html = "<br>".join(
                f"<code>{_escape_html(t)}</code> ({syn.occurrences_count.get(t, 0)}次)"
                for t in syn.terms
            )
            synonym_rows += f"""
            <tr>
                <td>{i}</td>
                <td>{terms_html}</td>
                <td>{int(syn.similarity * 100)}%</td>
            </tr>"""

        first_occ_rows = ""
        for term, occ in sorted(result.first_occurrences.items()):
            rel_path = relative_path(occ.file_path)
            ctx = _escape_html(occ.context.replace("\n", " ")[:80])
            first_occ_rows += f"""
            <tr>
                <td><code>{_escape_html(term)}</code></td>
                <td><code>{_escape_html(str(rel_path))}</code></td>
                <td>{occ.line_number}</td>
                <td>{ctx}</td>
            </tr>"""

        files_html = "".join(f"<li><code>{_escape_html(str(relative_path(fp)))}</code></li>" for fp in result.files_scanned)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>文档一致性检查报告</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 2rem; background: #f5f5f5; color: #333; }}
.container {{ max-width: 1200px; margin: 0 auto; background: #fff; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
h1 {{ color: #1a1a1a; margin-bottom: 1rem; border-bottom: 3px solid #3b82f6; padding-bottom: 0.5rem; }}
h2 {{ color: #1a1a1a; margin: 1.5rem 0 1rem; padding-left: 0.75rem; border-left: 4px solid #3b82f6; }}
.meta {{ background: #f0f9ff; padding: 1rem; border-radius: 6px; margin-bottom: 1.5rem; }}
.meta p {{ margin: 0.25rem 0; }}
.stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1rem; margin: 1rem 0; }}
.stat-card {{ padding: 1rem; border-radius: 6px; text-align: center; color: #fff; }}
.stat-card.error {{ background: #ef4444; }}
.stat-card.warning {{ background: #f59e0b; }}
.stat-card.info {{ background: #3b82f6; }}
.stat-card.suggestion {{ background: #10b981; }}
.stat-card .num {{ font-size: 2rem; font-weight: bold; }}
.stat-card .label {{ font-size: 0.9rem; opacity: 0.9; }}
table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; }}
th, td {{ padding: 0.75rem; text-align: left; border-bottom: 1px solid #e5e7eb; }}
th {{ background: #f9fafb; font-weight: 600; color: #374151; }}
tr:hover {{ background: #f9fafb; }}
tr.sev-error {{ background: #fef2f2; }}
tr.sev-warning {{ background: #fffbeb; }}
.badge {{ padding: 0.25rem 0.5rem; border-radius: 4px; font-size: 0.8rem; font-weight: 600; color: #fff; }}
.badge-error {{ background: #ef4444; }}
.badge-warning {{ background: #f59e0b; }}
.badge-info {{ background: #3b82f6; }}
.badge-suggestion {{ background: #10b981; }}
code {{ background: #f3f4f6; padding: 0.125rem 0.375rem; border-radius: 4px; font-size: 0.85rem; }}
ul.files-list {{ list-style: none; padding-left: 0; }}
ul.files-list li {{ padding: 0.5rem; border-bottom: 1px solid #f3f4f6; }}
.empty {{ text-align: center; padding: 2rem; color: #9ca3af; }}
</style>
</head>
<body>
<div class="container">
    <h1>📋 文档一致性检查报告</h1>
    <div class="meta">
        <p><strong>生成时间:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <p><strong>扫描文件数:</strong> {len(result.files_scanned)}</p>
        <p><strong>耗时:</strong> {format_time(result.duration)}</p>
    </div>

    <h2>📊 问题统计</h2>
    <div class="stats-grid">
        <div class="stat-card error"><div class="num">{counts['error']}</div><div class="label">错误</div></div>
        <div class="stat-card warning"><div class="num">{counts['warning']}</div><div class="label">警告</div></div>
        <div class="stat-card info"><div class="num">{counts['info']}</div><div class="label">信息</div></div>
        <div class="stat-card suggestion"><div class="num">{counts['suggestion']}</div><div class="label">建议</div></div>
    </div>
    <p style="text-align: right; margin-top: 0.5rem;"><strong>总计: {total} 个问题</strong></p>

    <h2>🔍 问题详情</h2>
    {"<table><thead><tr><th>#</th><th>级别</th><th>类型</th><th>描述</th><th>位置</th><th>建议</th></tr></thead><tbody>" + issue_rows + "</tbody></table>" if result.issues else '<div class="empty">✅ 未发现任何问题</div>'}

    {f'<h2>🤔 疑似同义词组</h2><table><thead><tr><th>#</th><th>术语组</th><th>相似度</th></tr></thead><tbody>{synonym_rows}</tbody></table>' if result.suspected_synonyms else ''}

    {f'<h2>📍 术语首次出现位置</h2><table><thead><tr><th>术语</th><th>文件</th><th>行号</th><th>上下文</th></tr></thead><tbody>{first_occ_rows}</tbody></table>' if result.first_occurrences else ''}

    <h2>📁 扫描的文件</h2>
    <ul class="files-list">{files_html}</ul>
</div>
</body>
</html>"""

    def _to_csv(self, result: CheckResult) -> str:
        """CSV格式输出"""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "序号", "严重级别", "类型", "消息", "文件路径", "行号",
            "建议", "上下文",
        ])
        for i, issue in enumerate(result.issues, 1):
            writer.writerow([
                i,
                SEVERITY_LABELS.get(issue.severity, issue.severity.value),
                ISSUE_TYPE_LABELS.get(issue.type, issue.type.value),
                issue.message,
                str(issue.file_path) if issue.file_path else "",
                issue.line_number if issue.line_number else "",
                issue.suggestion,
                issue.context,
            ])
        return output.getvalue()

    def generate_task_list(
        self,
        result: CheckResult,
        output: Optional[Path] = None,
        assignees: Optional[Dict[str, str]] = None,
    ) -> List[TaskItem]:
        """生成可分配给编辑的任务清单"""
        assignees = assignees or {}
        tasks: List[TaskItem] = []

        priority_map = {
            Severity.ERROR: "high",
            Severity.WARNING: "medium",
            Severity.INFO: "low",
            Severity.SUGGESTION: "low",
        }

        for i, issue in enumerate(result.issues, 1):
            task_id = f"TASK-{i:04d}"
            type_label = ISSUE_TYPE_LABELS.get(issue.type, issue.type.value)
            file_name = issue.file_path.name if issue.file_path else "unknown"
            title = f"[{type_label}] {file_name}: {issue.message[:50]}"
            if len(issue.message) > 50:
                title += "..."

            tags = [issue.type.value, issue.severity.value]
            if issue.file_path:
                for k, v in assignees.items():
                    if k in str(issue.file_path).lower() or k in file_name.lower():
                        tags.append(f"area:{v}")

            assignee = ""
            for pattern, person in assignees.items():
                if issue.file_path and pattern in str(issue.file_path).lower():
                    assignee = person
                    break

            tasks.append(TaskItem(
                id=task_id,
                severity=issue.severity.value,
                type=issue.type.value,
                title=title,
                description=issue.message,
                file_path=str(issue.file_path) if issue.file_path else "",
                line_number=issue.line_number,
                assignee=assignee,
                status="open",
                priority=priority_map.get(issue.severity, "medium"),
                suggestion=issue.suggestion,
                context=issue.context,
                tags=tags,
            ))

        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            fmt = output.suffix.lower().lstrip(".")
            if fmt in ("yaml", "yml"):
                data = [t.to_dict() for t in tasks]
                with open(output, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            elif fmt == "json":
                data = [t.to_dict() for t in tasks]
                with open(output, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            elif fmt == "csv":
                with open(output, "w", encoding="utf-8", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "ID", "优先级", "严重程度", "类型", "标题", "描述",
                        "文件", "行号", "负责人", "状态", "建议", "标签",
                    ])
                    for t in tasks:
                        writer.writerow([
                            t.id, t.priority, t.severity, t.type, t.title,
                            t.description, t.file_path, t.line_number or "",
                            t.assignee, t.status, t.suggestion, ",".join(t.tags),
                        ])
            else:
                md_content = self._to_tasks_markdown(result, tasks)
                with open(output, "w", encoding="utf-8") as f:
                    f.write(md_content)

        return tasks

    def _to_tasks_markdown(self, result: CheckResult, tasks: Optional[List[TaskItem]] = None) -> str:
        """任务清单的Markdown格式"""
        if tasks is None:
            tasks = self.generate_task_list(result)

        lines = []
        lines.append("# 🎯 编辑任务清单")
        lines.append("")
        lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**任务总数**: {len(tasks)}")
        lines.append("")

        by_priority: Dict[str, List[TaskItem]] = defaultdict(list)
        for t in tasks:
            by_priority[t.priority].append(t)

        priority_labels = {"high": "🔴 高优先级", "medium": "🟡 中优先级", "low": "🟢 低优先级"}
        priority_order = ["high", "medium", "low"]

        for prio in priority_order:
            if prio not in by_priority:
                continue
            prio_tasks = by_priority[prio]
            lines.append(f"## {priority_labels[prio]} ({len(prio_tasks)})")
            lines.append("")

            by_assignee: Dict[str, List[TaskItem]] = defaultdict(list)
            unassigned = []
            for t in prio_tasks:
                if t.assignee:
                    by_assignee[t.assignee].append(t)
                else:
                    unassigned.append(t)

            for person, person_tasks in sorted(by_assignee.items()):
                lines.append(f"### 👤 {person}")
                lines.append("")
                self._render_task_items(lines, person_tasks)

            if unassigned:
                lines.append("### 📋 待分配")
                lines.append("")
                self._render_task_items(lines, unassigned)

        return "\n".join(lines)

    @staticmethod
    def _render_task_items(lines: List[str], tasks: List[TaskItem]) -> None:
        """渲染任务列表项"""
        for t in tasks:
            sev_icon = SEVERITY_ICONS.get(Severity(t.severity), "")
            loc = f"`{t.file_path}:{t.line_number}`" if t.line_number else f"`{t.file_path}`"
            lines.append(f"- [ ] **{t.id}** {sev_icon} {t.title}")
            lines.append(f"  - 📍 位置: {loc}")
            lines.append(f"  - 📝 描述: {t.description}")
            if t.suggestion:
                lines.append(f"  - 💡 建议: {t.suggestion}")
            lines.append("")


def _escape_html(text: str) -> str:
    """转义HTML特殊字符"""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
