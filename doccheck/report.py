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
from .utils import (
    CheckResult,
    Issue,
    IssueType,
    Severity,
    FirstOccurrence,
    SuspectedSynonym,
    format_time,
    relative_path,
    TaskStateStore,
    TaskStatus,
    TASK_STATUS_LABELS,
    ReportSnapshot,
    ReportSnapshotIssue,
    ReportDiffResult,
    diff_report_snapshots,
)


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
    IssueType.TERM_ALIAS_FOUND: "术语别名/变体",
    IssueType.TERM_FORBIDDEN: "术语禁用写法",
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
    stable_id: str = ""
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
        if not self.stable_id and self.id:
            self.stable_id = self.id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "stable_id": self.stable_id,
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

    def __init__(
        self,
        config: Optional[CheckConfig] = None,
        task_store: Optional[TaskStateStore] = None,
        previous_snapshot: Optional[ReportSnapshot] = None,
    ):
        self.config = config or CheckConfig()
        self.task_store = task_store
        self.previous_snapshot = previous_snapshot
        self._diff_result: Optional[ReportDiffResult] = None
        if previous_snapshot is not None:
            self._diff_result = None  # 延迟到有 CheckResult 时计算

    def generate(
        self,
        result: CheckResult,
        fmt: str = "console",
        output: Optional[Path] = None,
    ) -> str:
        """生成报告"""
        fmt = fmt.lower()

        # 计算diff结果（如果有上次快照）
        if self.previous_snapshot is not None and self._diff_result is None:
            current_snap = ReportSnapshot.from_result(result)
            self._diff_result = diff_report_snapshots(self.previous_snapshot, current_snap)

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

    @property
    def diff_result(self) -> Optional[ReportDiffResult]:
        return self._diff_result

    @staticmethod
    def _format_ignore_stats(result: CheckResult) -> Optional[str]:
        """格式化忽略统计信息（用于console/markdown顶部）"""
        stats = result.ignore_stats
        if not stats or stats.get("total", 0) == 0:
            return None

        total = stats["total"]
        by_type = stats.get("by_type", {})
        parts = [f"🔕 已忽略 {total} 条问题"]
        if by_type:
            type_parts = []
            for itype, cnt in sorted(by_type.items(), key=lambda x: -x[1]):
                type_label = ISSUE_TYPE_LABELS.get(IssueType(itype), itype) if itype in [e.value for e in IssueType] else itype
                type_parts.append(f"{type_label} {cnt}个")
            if type_parts:
                parts.append("（按类型：" + "、".join(type_parts) + "）")
        by_rule = stats.get("by_rule", {})
        if by_rule:
            rule_parts = []
            for pat, cnt in sorted(by_rule.items(), key=lambda x: -x[1]):
                rule_parts.append(f"'{pat}'×{cnt}")
            parts.append(" [匹配: " + ", ".join(rule_parts) + "]")
        return "".join(parts)

    @staticmethod
    def _format_dedup_stats(result: CheckResult) -> Optional[str]:
        """格式化去重统计信息"""
        stats = result.dedup_stats
        if not stats or stats.get("removed", 0) == 0:
            return None

        removed = stats["removed"]
        groups = stats.get("merged_groups", 0)
        if groups > 0:
            return f"🗑️  合并去重：减少 {removed} 条（共合并 {groups} 组）"
        return f"🗑️  合并去重：减少 {removed} 条"

    def _format_diff_summary(self) -> Optional[str]:
        """格式化 diff 概要（用于顶部统计行）"""
        if self._diff_result is None:
            return None
        diff = self._diff_result
        parts = [f"📈 对比上次：新增 {diff.new_count}，已解决 {diff.resolved_count}，仍未处理 {diff.unchanged_count}"]
        if diff.previous and diff.previous.created_at:
            parts.append(f"（上次快照: {diff.previous.created_at}）")
        return "".join(parts)

    def _render_diff_console(self) -> str:
        """渲染控制台格式的 diff 视图"""
        if self._diff_result is None:
            return ""
        diff = self._diff_result
        lines = []
        lines.append("-" * 70)
        lines.append("📈 对比上次报告")
        lines.append("-" * 70)
        lines.append("")
        lines.append(f"  新增问题: {diff.new_count} 条")
        lines.append(f"  已解决: {diff.resolved_count} 条")
        lines.append(f"  仍未处理: {diff.unchanged_count} 条")
        lines.append("")

        if diff.new_issues:
            lines.append(f"🆕 新增问题（{len(diff.new_issues)}）：")
            for i, issue in enumerate(diff.new_issues[:15], 1):
                sev_icon = SEVERITY_ICONS.get(Severity(issue.severity), "  ")
                type_label = ISSUE_TYPE_LABELS.get(IssueType(issue.type), issue.type)
                lines.append(f"  {i}. {sev_icon} [{issue.stable_id}] [{type_label}] {issue.message[:60]}")
            if len(diff.new_issues) > 15:
                lines.append(f"  ... 还有 {len(diff.new_issues) - 15} 条")
            lines.append("")

        if diff.resolved_issues:
            lines.append(f"✅ 已解决（{len(diff.resolved_issues)}）：")
            for i, issue in enumerate(diff.resolved_issues[:10], 1):
                type_label = ISSUE_TYPE_LABELS.get(IssueType(issue.type), issue.type)
                lines.append(f"  {i}. [{issue.stable_id}] [{type_label}] {issue.message[:60]}")
            if len(diff.resolved_issues) > 10:
                lines.append(f"  ... 还有 {len(diff.resolved_issues) - 10} 条")
            lines.append("")

        return "\n".join(lines)

    def _render_diff_markdown(self) -> str:
        """渲染 Markdown 格式的 diff 视图"""
        if self._diff_result is None:
            return ""
        diff = self._diff_result
        lines = []

        lines.append("## 📈 对比上次报告")
        lines.append("")
        lines.append("| 类别 | 数量 |")
        lines.append("|------|------|")
        lines.append(f"| 🆕 新增问题 | **{diff.new_count}** |")
        lines.append(f"| ✅ 已解决 | **{diff.resolved_count}** |")
        lines.append(f"| ⏳ 仍未处理 | **{diff.unchanged_count}** |")
        lines.append("")
        if diff.previous and diff.previous.created_at:
            lines.append(f"> 上次快照生成时间: {diff.previous.created_at}")
            lines.append("")

        if diff.new_issues:
            lines.append(f"### 🆕 新增问题（{len(diff.new_issues)}）")
            lines.append("")
            lines.append("| 稳定ID | 级别 | 类型 | 描述 |")
            lines.append("|--------|------|------|------|")
            for issue in diff.new_issues:
                sev_icon = TYPE_EMOJI_MAP.get(issue.severity, "")
                type_label = ISSUE_TYPE_LABELS.get(IssueType(issue.type), issue.type)
                msg = issue.message.replace("|", "\\|")
                lines.append(f"| `{issue.stable_id}` | {sev_icon} | {type_label} | {msg} |")
            lines.append("")

        if diff.resolved_issues:
            lines.append(f"### ✅ 已解决（{len(diff.resolved_issues)}）")
            lines.append("")
            lines.append("| 稳定ID | 类型 | 描述 |")
            lines.append("|--------|------|------|")
            for issue in diff.resolved_issues:
                type_label = ISSUE_TYPE_LABELS.get(IssueType(issue.type), issue.type)
                msg = issue.message.replace("|", "\\|")
                lines.append(f"| `{issue.stable_id}` | {type_label} | {msg} |")
            lines.append("")

        if diff.unchanged_issues:
            lines.append(f"### ⏳ 仍未处理（{len(diff.unchanged_issues)}）")
            lines.append("")
            lines.append("| 稳定ID | 级别 | 类型 | 描述 |")
            lines.append("|--------|------|------|------|")
            for issue in diff.unchanged_issues[:20]:
                sev_icon = TYPE_EMOJI_MAP.get(issue.severity, "")
                type_label = ISSUE_TYPE_LABELS.get(IssueType(issue.type), issue.type)
                msg = issue.message.replace("|", "\\|")
                lines.append(f"| `{issue.stable_id}` | {sev_icon} | {type_label} | {msg} |")
            if len(diff.unchanged_issues) > 20:
                lines.append(f"| ... | | | 还有 {len(diff.unchanged_issues) - 20} 条 |")
            lines.append("")

        return "\n".join(lines)

    def _render_diff_html(self) -> str:
        """渲染 HTML 格式的 diff 视图"""
        if self._diff_result is None:
            return ""
        diff = self._diff_result

        def _issue_rows(issues, max_show=30):
            rows = ""
            for i, issue in enumerate(issues[:max_show]):
                sev_label = SEVERITY_LABELS.get(Severity(issue.severity), issue.severity)
                sev_class = f"sev-{issue.severity}"
                type_label = ISSUE_TYPE_LABELS.get(IssueType(issue.type), issue.type)
                rows += f"""
                <tr class="{sev_class}">
                    <td><code style='background:#eef2ff;color:#3730a3'>{_escape_html(issue.stable_id)}</code></td>
                    <td><span class="badge badge-{issue.severity}">{sev_label}</span></td>
                    <td>{type_label}</td>
                    <td>{_escape_html(issue.message)}</td>
                </tr>"""
            if len(issues) > max_show:
                rows += f"<tr><td colspan='4' style='text-align:center;color:#9ca3af'>... 还有 {len(issues) - max_show} 条</td></tr>"
            return rows

        prev_time = diff.previous.created_at if diff.previous else ""

        new_html = f"""
        <h2>📈 对比上次报告</h2>
        <div class="stats-grid" style="grid-template-columns:repeat(3, 1fr)">
            <div class="stat-card" style="background:#f59e0b"><div class="num">{diff.new_count}</div><div class="label">新增</div></div>
            <div class="stat-card" style="background:#10b981"><div class="num">{diff.resolved_count}</div><div class="label">已解决</div></div>
            <div class="stat-card" style="background:#6b7280"><div class="num">{diff.unchanged_count}</div><div class="label">仍未处理</div></div>
        </div>
        {f"<p style='color:#6b7280;text-align:right'>上次快照: {prev_time}</p>" if prev_time else ""}
        """

        if diff.new_issues:
            new_html += f"""
            <h3>🆕 新增问题（{len(diff.new_issues)}）</h3>
            <table>
                <thead><tr><th>稳定ID</th><th>级别</th><th>类型</th><th>描述</th></tr></thead>
                <tbody>{_issue_rows(diff.new_issues)}</tbody>
            </table>
            """

        if diff.resolved_issues:
            new_html += f"""
            <h3>✅ 已解决（{len(diff.resolved_issues)}）</h3>
            <table>
                <thead><tr><th>稳定ID</th><th>类型</th><th>描述</th></tr></thead>
                <tbody>{_issue_rows(diff.resolved_issues)}</tbody>
            </table>
            """

        if diff.unchanged_issues:
            new_html += f"""
            <h3>⏳ 仍未处理（{len(diff.unchanged_issues)}）</h3>
            <table>
                <thead><tr><th>稳定ID</th><th>级别</th><th>类型</th><th>描述</th></tr></thead>
                <tbody>{_issue_rows(diff.unchanged_issues, max_show=15)}</tbody>
            </table>
            """

        return new_html

    def _to_console(self, result: CheckResult) -> str:
        """控制台格式输出"""
        lines = []
        lines.append("=" * 70)
        lines.append("📋 文档一致性检查报告")
        lines.append("=" * 70)
        lines.append("")
        lines.append(f"📁 扫描文件数: {len(result.files_scanned)}")
        lines.append(f"⏱️  耗时: {format_time(result.duration)}")

        dedup_info = self._format_dedup_stats(result)
        ignore_info = self._format_ignore_stats(result)
        if dedup_info:
            lines.append(dedup_info)
        if ignore_info:
            lines.append(ignore_info)
        lines.append("")

        counts = result.issue_count
        total = sum(counts.values())
        lines.append(f"📊 问题统计: 总计 {total} 个")
        for sev, icon in SEVERITY_ICONS.items():
            cnt = counts[sev.value]
            if cnt > 0:
                lines.append(f"   {icon} {SEVERITY_LABELS[sev]:>4s}: {cnt}")
        lines.append("")

        diff_summary = self._format_diff_summary()
        if diff_summary:
            lines.append(diff_summary)
            lines.append("")

        if self.task_store and self.task_store.states:
            from collections import Counter
            status_counter = Counter(
                rec.status for rec in self.task_store.states.values()
            )
            status_parts = []
            for st in [TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.FIXED, TaskStatus.IGNORED]:
                cnt = status_counter.get(st.value, 0)
                if cnt > 0:
                    status_parts.append(f"{TASK_STATUS_LABELS.get(st, st.value)}: {cnt}")
            if status_parts:
                lines.append("📝 任务状态: " + "  ".join(status_parts))
                lines.append("")

        diff_console = self._render_diff_console()
        if diff_console:
            lines.append(diff_console)
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
                    sid = issue.stable_id or "N/A"
                    needs_edit = ""
                    meta = issue.metadata or {}
                    if meta.get("needs_edit") is True:
                        needs_edit = " [必须修改]"
                    elif meta.get("needs_edit") is False:
                        needs_edit = " [可选]"
                    lines.append(f"  {i}. [{sid}] [{type_label}] {issue.message}{needs_edit}")
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

        dedup_info = self._format_dedup_stats(result)
        ignore_info = self._format_ignore_stats(result)
        if dedup_info:
            lines.append(f"> {dedup_info}")
        if ignore_info:
            lines.append(f"> {ignore_info}")
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

        diff_summary = self._format_diff_summary()
        if diff_summary:
            lines.append(f"> {diff_summary}")
            lines.append("")

        if self.task_store and self.task_store.states:
            from collections import Counter
            status_counter = Counter(rec.status for rec in self.task_store.states.values())
            status_parts = []
            for st in [TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.FIXED, TaskStatus.IGNORED]:
                cnt = status_counter.get(st.value, 0)
                label = TASK_STATUS_LABELS.get(st, st.value)
                status_parts.append(f"**{label}**: {cnt}")
            lines.append("## 📝 任务状态概览")
            lines.append("")
            lines.append(" | ".join(status_parts))
            lines.append("")

        diff_md = self._render_diff_markdown()
        if diff_md:
            lines.append(diff_md)
            lines.append("")

        if result.issues:
            lines.append("## 🔍 问题详情")
            lines.append("")
            lines.append("| # | 稳定ID | 级别 | 类型 | 描述 | 文件:行 | 建议 |")
            lines.append("|---|--------|------|------|------|---------|------|")
            for i, issue in enumerate(result.issues, 1):
                sev_icon = TYPE_EMOJI_MAP[issue.severity.value]
                type_label = ISSUE_TYPE_LABELS.get(issue.type, issue.type.value)
                loc = issue.location or "-"
                msg = issue.message.replace("|", "\\|")
                sug = issue.suggestion.replace("|", "\\|") if issue.suggestion else "-"
                sid = issue.stable_id or "-"
                lines.append(f"| {i} | `{sid}` | {sev_icon} | {type_label} | {msg} | `{loc}` | {sug} |")
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

        extra_info_parts = []
        dedup_info = self._format_dedup_stats(result)
        ignore_info = self._format_ignore_stats(result)
        if dedup_info:
            extra_info_parts.append(f"<p style='color:#6b7280;margin:0.25rem 0;'>{dedup_info}</p>")
        if ignore_info:
            extra_info_parts.append(f"<p style='color:#6b7280;margin:0.25rem 0;'>{ignore_info}</p>")
        extra_info_html = "".join(extra_info_parts)

        issue_rows = ""
        for i, issue in enumerate(result.issues, 1):
            sev_label = SEVERITY_LABELS.get(issue.severity, issue.severity.value)
            sev_class = f"sev-{issue.severity.value}"
            type_label = ISSUE_TYPE_LABELS.get(issue.type, issue.type.value)
            loc = issue.location or "-"
            sid = issue.stable_id or "-"
            issue_rows += f"""
            <tr class="{sev_class}">
                <td>{i}</td>
                <td><code style='background:#eef2ff;color:#3730a3'>{_escape_html(sid)}</code></td>
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

        issue_table_header = """<table><thead><tr><th>#</th><th>稳定ID</th><th>级别</th><th>类型</th><th>描述</th><th>位置</th><th>建议</th></tr></thead><tbody>"""

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>文档一致性检查报告</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 2rem; background: #f5f5f5; color: #333; }}
.container {{ max-width: 1300px; margin: 0 auto; background: #fff; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
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
table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.9rem; }}
th, td {{ padding: 0.6rem 0.75rem; text-align: left; border-bottom: 1px solid #e5e7eb; }}
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
        {extra_info_html}
    </div>

    <h2>📊 问题统计</h2>
    <div class="stats-grid">
        <div class="stat-card error"><div class="num">{counts['error']}</div><div class="label">错误</div></div>
        <div class="stat-card warning"><div class="num">{counts['warning']}</div><div class="label">警告</div></div>
        <div class="stat-card info"><div class="num">{counts['info']}</div><div class="label">信息</div></div>
        <div class="stat-card suggestion"><div class="num">{counts['suggestion']}</div><div class="label">建议</div></div>
    </div>
    <p style="text-align: right; margin-top: 0.5rem;"><strong>总计: {total} 个问题</strong></p>

    {self._render_diff_html() if self._diff_result else ''}

    <h2>🔍 问题详情</h2>
    {"<table><thead><tr><th>#</th><th>稳定ID</th><th>级别</th><th>类型</th><th>描述</th><th>位置</th><th>建议</th></tr></thead><tbody>" + issue_rows + "</tbody></table>" if result.issues else '<div class="empty">✅ 未发现任何问题</div>'}

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
            "序号", "稳定ID", "严重级别", "类型", "消息", "文件路径", "行号",
            "建议", "上下文", "必须修改",
        ])
        for i, issue in enumerate(result.issues, 1):
            meta = issue.metadata or {}
            needs_edit = meta.get("needs_edit", "")
            writer.writerow([
                i,
                issue.stable_id or "",
                SEVERITY_LABELS.get(issue.severity, issue.severity.value),
                ISSUE_TYPE_LABELS.get(issue.type, issue.type.value),
                issue.message,
                str(issue.file_path) if issue.file_path else "",
                issue.line_number if issue.line_number else "",
                issue.suggestion,
                issue.context,
                "是" if needs_edit is True else ("否" if needs_edit is False else ""),
            ])
        return output.getvalue()

    def generate_task_list(
        self,
        result: CheckResult,
        output: Optional[Path] = None,
        assignees: Optional[Dict[str, str]] = None,
        sync_to_store: bool = False,
    ) -> List[TaskItem]:
        """生成可分配给编辑的任务清单（使用稳定ID）。

        Args:
            result: 检查结果
            output: 输出文件路径
            assignees: 负责人模式映射
            sync_to_store: 是否将新问题同步写入 task_store（新增为 pending）
        """
        assignees = assignees or {}
        tasks: List[TaskItem] = []

        priority_map = {
            Severity.ERROR: "high",
            Severity.WARNING: "medium",
            Severity.INFO: "low",
            Severity.SUGGESTION: "low",
        }

        for i, issue in enumerate(result.issues, 1):
            sid = issue.stable_id or f"ISS-{i:06d}"
            type_label = ISSUE_TYPE_LABELS.get(issue.type, issue.type.value)
            file_name = issue.file_path.name if issue.file_path else "unknown"
            title = f"[{type_label}] {file_name}: {issue.message[:50]}"
            if len(issue.message) > 50:
                title += "..."

            tags = [issue.type.value, issue.severity.value]
            meta = issue.metadata or {}
            if meta.get("needs_edit") is True:
                tags.append("needs_edit")
            elif meta.get("needs_edit") is False:
                tags.append("optional")
            if meta.get("variant_type"):
                tags.append(f"variant:{meta['variant_type']}")
            if issue.file_path:
                for k, v in assignees.items():
                    if k in str(issue.file_path).lower() or k in file_name.lower():
                        tags.append(f"area:{v}")

            assignee = ""
            status = "open"
            status_label = ""

            # 从任务状态存储中读取已有状态
            if self.task_store is not None:
                rec = self.task_store.get(sid)
                if rec:
                    status = rec.status
                    assignee = rec.assignee or assignee
                    status_label = TASK_STATUS_LABELS.get(
                        TaskStatus(status), status
                    )

            # 如果没有指定负责人，尝试从 assignees 自动分配
            if not assignee:
                for pattern, person in assignees.items():
                    if issue.file_path and pattern in str(issue.file_path).lower():
                        assignee = person
                        break

            # 同步到存储（新问题标记为 pending）
            if sync_to_store and self.task_store is not None:
                if not self.task_store.get(sid):
                    self.task_store.set_status(
                        sid,
                        TaskStatus.PENDING.value,
                        assignee=assignee or None,
                    )

            task = TaskItem(
                id=sid,
                stable_id=sid,
                severity=issue.severity.value,
                type=issue.type.value,
                title=title,
                description=issue.message,
                file_path=str(issue.file_path) if issue.file_path else "",
                line_number=issue.line_number,
                assignee=assignee,
                status=status,
                priority=priority_map.get(issue.severity, "medium"),
                suggestion=issue.suggestion,
                context=issue.context,
                tags=tags,
            )
            if status_label:
                task.tags = list(task.tags) + [f"status:{status}"]
            tasks.append(task)

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
                        "ID", "稳定ID", "优先级", "严重程度", "类型", "状态", "标题", "描述",
                        "文件", "行号", "负责人", "建议", "标签",
                    ])
                    for t in tasks:
                        writer.writerow([
                            t.id, t.stable_id, t.priority, t.severity, t.type,
                            t.status, t.title, t.description,
                            t.file_path, t.line_number or "",
                            t.assignee, t.suggestion, ",".join(t.tags),
                        ])
            else:
                md_content = self._to_tasks_markdown(result, tasks)
                with open(output, "w", encoding="utf-8") as f:
                    f.write(md_content)

        return tasks

    def sync_issues_to_store(self, result: CheckResult) -> int:
        """将当前结果中的问题同步到任务状态存储。

        新问题标记为 pending，已有问题保留原状态。
        返回新增的任务数量。
        """
        if self.task_store is None:
            return 0

        new_count = 0
        for issue in result.issues:
            sid = issue.stable_id
            if not sid:
                continue
            if not self.task_store.get(sid):
                self.task_store.set_status(sid, TaskStatus.PENDING.value)
                new_count += 1
        return new_count

    def _to_tasks_markdown(self, result: CheckResult, tasks: Optional[List[TaskItem]] = None) -> str:
        """任务清单的Markdown格式（按状态分组，优先级排序）"""
        if tasks is None:
            tasks = self.generate_task_list(result)

        lines = []
        lines.append("# 🎯 编辑任务清单")
        lines.append("")
        lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**任务总数**: {len(tasks)}")

        ignore_info = self._format_ignore_stats(result)
        if ignore_info:
            lines.append(f"> {ignore_info}")
        dedup_info = self._format_dedup_stats(result)
        if dedup_info:
            lines.append(f"> {dedup_info}")
        lines.append("")

        # 按状态分组
        by_status: Dict[str, List[TaskItem]] = defaultdict(list)
        for t in tasks:
            by_status[t.status].append(t)

        # 显示顺序：待处理 → 处理中 → 已修复 → 已忽略 → 其他
        status_order = [
            TaskStatus.PENDING.value,
            TaskStatus.IN_PROGRESS.value,
            TaskStatus.FIXED.value,
            TaskStatus.IGNORED.value,
        ]
        status_labels = {
            TaskStatus.PENDING.value: "⏳ 待处理",
            TaskStatus.IN_PROGRESS.value: "� 处理中",
            TaskStatus.FIXED.value: "✅ 已修复",
            TaskStatus.IGNORED.value: "🚫 已忽略",
        }

        for st in status_order:
            st_tasks = by_status.get(st, [])
            if not st_tasks:
                continue
            label = status_labels.get(st, st)
            lines.append(f"## {label}（{len(st_tasks)}）")
            lines.append("")

            # 组内按优先级排序
            st_tasks_sorted = sorted(
                st_tasks,
                key=lambda t: ({"high": 0, "medium": 1, "low": 2}.get(t.priority, 9), t.id)
            )

            by_priority: Dict[str, List[TaskItem]] = defaultdict(list)
            for t in st_tasks_sorted:
                by_priority[t.priority].append(t)

            priority_labels = {"high": "🔴 高优先级", "medium": "🟡 中优先级", "low": "🟢 低优先级"}
            for prio in ["high", "medium", "low"]:
                prio_tasks = by_priority.get(prio, [])
                if not prio_tasks:
                    continue
                if len(st_tasks_sorted) > 10:
                    lines.append(f"### {priority_labels[prio]}")
                    lines.append("")
                self._render_task_items(lines, prio_tasks, show_status=False)

        # 其他未分类状态
        known = set(status_order)
        other_tasks = [t for t in tasks if t.status not in known]
        if other_tasks:
            lines.append(f"## 📋 其他（{len(other_tasks)}）")
            lines.append("")
            self._render_task_items(lines, other_tasks, show_status=True)

        return "\n".join(lines)

    @staticmethod
    def _render_task_items(lines: List[str], tasks: List[TaskItem], show_status: bool = False) -> None:
        """渲染任务列表项"""
        for t in tasks:
            sev_icon = SEVERITY_ICONS.get(Severity(t.severity), "")
            loc = f"`{t.file_path}:{t.line_number}`" if t.line_number else f"`{t.file_path}`"
            tags_str = ""
            if t.tags:
                visible_tags = []
                for tg in t.tags:
                    if tg == "needs_edit":
                        visible_tags.append("`[必须修改]`")
                    elif tg.startswith("variant:"):
                        vtype = tg.split(":", 1)[1]
                        vmap = {"forbidden": "禁用", "allowed": "允许", "alias": "别名"}
                        visible_tags.append(f"`[{vmap.get(vtype, vtype)}]`")
                    elif tg.startswith("status:") and show_status:
                        st = tg.split(":", 1)[1]
                        smap = {"pending": "待处理", "in_progress": "处理中",
                                "fixed": "已修复", "ignored": "已忽略"}
                        visible_tags.append(f"`[{smap.get(st, st)}]`")
                if visible_tags:
                    tags_str = " " + " ".join(visible_tags)
            checkbox = " "
            status_badge = ""
            if t.status == "fixed":
                checkbox = "x"
            elif t.status == "ignored":
                checkbox = "~"
            elif t.status == "in_progress":
                status_badge = " `[处理中]`"
            elif t.status == "pending":
                status_badge = " `[待处理]`"
            lines.append(f"- [{checkbox}] **{t.id}** {sev_icon} {t.title}{tags_str}{status_badge}")
            lines.append(f"  - 📍 位置: {loc}")
            lines.append(f"  - 📝 描述: {t.description}")
            if t.suggestion:
                lines.append(f"  - 💡 建议: {t.suggestion}")
            if t.assignee:
                lines.append(f"  - 👤 负责人: {t.assignee}")
            if hasattr(t, 'note') and t.note:
                lines.append(f"  - 📌 备注: {t.note}")
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
