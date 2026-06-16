"""CLI入口 - 四大命令: scan、terms、refs、report"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

import click
import yaml

from . import __version__
from .config import CheckConfig, TermList, load_config, load_terms, save_config, save_terms
from .refs import ReferenceChecker
from .report import ReportGenerator
from .scanner import BaseScanner
from .terms import TermManager
from .utils import parse_chapter_range


def _build_config(
    folder: Optional[str],
    chapter_range: Optional[str],
    output_format: Optional[str],
    output: Optional[str],
    terms_file: Optional[str],
    modified: Optional[int],
    extensions: Optional[str],
    exclude: Optional[str],
    config_path: Optional[str],
) -> CheckConfig:
    """根据命令行参数构建配置"""
    cfg = load_config(Path(config_path) if config_path else None)

    if chapter_range:
        cfg.chapter_range = parse_chapter_range(chapter_range)
    if output_format:
        cfg.output_format = output_format
    if output:
        cfg.output_path = Path(output)
    if terms_file:
        cfg.terms_file = Path(terms_file)
    if modified is not None:
        cfg.modified_within_days = modified
    if extensions:
        cfg.extensions = [e if e.startswith(".") else f".{e}" for e in extensions.split(",")]
    if exclude:
        cfg.exclude_patterns = exclude.split(",")

    return cfg


def _get_folder(folder: Optional[str]) -> Path:
    """获取扫描的根目录"""
    return Path(folder).resolve() if folder else Path.cwd()


@click.group(help="📋 文档一致性检查工具 - 批量检查长篇文档的一致性问题")
@click.version_option(__version__, "-V", "--version", prog_name="doccheck")
@click.option("-c", "--config", "config_path", type=click.Path(), help="配置文件路径")
@click.pass_context
def main(ctx: click.Context, config_path: Optional[str]):
    """文档一致性检查工具主入口"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


# ==================== scan 命令 ====================

@main.command("scan", help="🔍 扫描文档，检查章节编号、重复标题、专有名词一致性")
@click.argument("folder", required=False, type=click.Path(exists=True, file_okay=False))
@click.option("-r", "--chapter-range", "chapter_range", help="章节范围，如 '1-10' 或 '5'")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml", "markdown", "md", "html", "csv"]),
              default=None, help="输出格式 (默认: console)")
@click.option("-o", "--output", type=click.Path(), help="输出文件路径")
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="术语清单文件")
@click.option("-m", "--modified", type=int, help="只检查最近N天修改过的文件")
@click.option("--extensions", help="文件扩展名，逗号分隔 (默认: .md,.markdown,.txt)")
@click.option("--exclude", help="排除目录模式，逗号分隔")
@click.option("-q", "--quiet", is_flag=True, help="静默模式，只输出结果摘要")
@click.pass_context
def scan_cmd(
    ctx: click.Context,
    folder: Optional[str],
    chapter_range: Optional[str],
    output_format: Optional[str],
    output: Optional[str],
    terms_file: Optional[str],
    modified: Optional[int],
    extensions: Optional[str],
    exclude: Optional[str],
    quiet: bool,
):
    """扫描文档并执行基础检查"""
    root = _get_folder(folder)
    cfg = _build_config(folder, chapter_range, output_format, output, terms_file,
                        modified, extensions, exclude, ctx.obj.get("config_path"))

    if not quiet:
        click.echo(f"📁 扫描目录: {root}")
        if cfg.chapter_range:
            click.echo(f"📖 章节范围: {cfg.chapter_range}")
        if cfg.modified_within_days:
            click.echo(f"🕐 仅最近 {cfg.modified_within_days} 天修改的文件")

    terms = load_terms(cfg.terms_file)
    scanner = BaseScanner(config=cfg, terms=terms)
    result = scanner.scan(root)

    fmt = cfg.output_format or "console"
    out_path = cfg.output_path

    reporter = ReportGenerator(cfg)
    content = reporter.generate(result, fmt=fmt, output=out_path)

    if out_path:
        click.echo(f"✅ 报告已写入: {out_path}")
        if fmt == "console" and not quiet:
            click.echo()
            click.echo(content)
    else:
        click.echo(content)

    counts = result.issue_count
    if counts["error"] > 0:
        sys.exit(1)


# ==================== terms 命令组 ====================

@main.group("terms", help="📚 管理术语清单 (角色名、地名、专有名词)")
def terms_group():
    """术语管理命令组"""
    pass


@terms_group.command("list", help="列出所有术语")
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="术语清单文件路径")
@click.option("-c", "--category", help="按类别筛选 (character/location/proper_noun/general)")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml"]), default="console", help="输出格式")
@click.option("-o", "--output", type=click.Path(), help="输出文件路径")
@click.pass_context
def terms_list(
    ctx: click.Context,
    terms_file: Optional[str],
    category: Optional[str],
    output_format: str,
    output: Optional[str],
):
    """列出术语清单"""
    cfg = _build_config(None, None, None, None, terms_file, None, None, None, ctx.obj.get("config_path"))
    terms = load_terms(cfg.terms_file)
    manager = TermManager(terms=terms, config=cfg)
    term_list = manager.list_terms(category)

    if output_format == "json":
        data = [t.to_dict() for t in term_list]
        content = json.dumps(data, ensure_ascii=False, indent=2)
    elif output_format == "yaml":
        data = [t.to_dict() for t in term_list]
        content = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    else:
        lines = []
        if category:
            lines.append(f"📚 术语清单 (类别: {category}) - 共 {len(term_list)} 个")
        else:
            lines.append(f"📚 术语清单 - 共 {len(term_list)} 个")
        lines.append("")
        for t in term_list:
            alias_str = f" (别名: {', '.join(t.aliases)})" if t.aliases else ""
            desc_str = f" - {t.description}" if t.description else ""
            lines.append(f"  [{t.category}] {t.canonical}{alias_str}{desc_str}")
        content = "\n".join(lines)

    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", encoding="utf-8") as f:
            f.write(content)
        click.echo(f"✅ 已写入: {output}")
    else:
        click.echo(content)


@terms_group.command("add", help="添加新术语")
@click.option("--canonical", required=True, help="标准写法")
@click.option("-c", "--category", default="general",
              type=click.Choice(["character", "location", "proper_noun", "general"]),
              help="术语类别")
@click.option("-a", "--alias", multiple=True, help="别名 (可多次指定)")
@click.option("-d", "--description", default="", help="描述说明")
@click.option("-s", "--severity", default="warning",
              type=click.Choice(["error", "warning", "info"]), help="违规严重程度")
@click.option("--allow-variant", multiple=True, help="允许的变体写法 (可多次指定)")
@click.option("-t", "--terms", "terms_file", type=click.Path(), help="术语清单文件路径")
@click.pass_context
def terms_add(
    ctx: click.Context,
    canonical: str,
    category: str,
    alias: tuple,
    description: str,
    severity: str,
    allow_variant: tuple,
    terms_file: Optional[str],
):
    """添加新术语"""
    cfg = _build_config(None, None, None, None, terms_file, None, None, None, ctx.obj.get("config_path"))
    terms = load_terms(cfg.terms_file)
    manager = TermManager(terms=terms, config=cfg)

    existing = manager.terms.get(canonical)
    if existing:
        if not click.confirm(f"术语 \"{canonical}\" 已存在，是否覆盖？"):
            click.echo("已取消")
            return

    term_def = manager.add_term(
        canonical=canonical,
        category=category,
        aliases=list(alias),
        description=description,
        severity=severity,
        allowed_variants=list(allow_variant),
    )

    save_path = manager.save(cfg.terms_file)
    click.echo(f"✅ 已添加术语: [{term_def.category}] {term_def.canonical}")
    click.echo(f"📄 术语清单: {save_path}")


@terms_group.command("remove", help="删除术语")
@click.argument("canonical")
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="术语清单文件路径")
@click.option("-y", "--yes", is_flag=True, help="跳过确认")
@click.pass_context
def terms_remove(ctx: click.Context, canonical: str, terms_file: Optional[str], yes: bool):
    """删除术语"""
    cfg = _build_config(None, None, None, None, terms_file, None, None, None, ctx.obj.get("config_path"))
    terms = load_terms(cfg.terms_file)
    manager = TermManager(terms=terms, config=cfg)

    existing = manager.terms.get(canonical)
    if not existing:
        click.echo(f"❌ 未找到术语: {canonical}")
        sys.exit(1)

    if not yes and not click.confirm(f"确认删除术语 \"{canonical}\"？"):
        click.echo("已取消")
        return

    if manager.remove_term(canonical):
        save_path = manager.save(cfg.terms_file)
        click.echo(f"✅ 已删除术语: {canonical}")
        click.echo(f"📄 术语清单: {save_path}")


@terms_group.command("check", help="检查术语使用一致性")
@click.argument("folder", required=False, type=click.Path(exists=True, file_okay=False))
@click.option("-r", "--chapter-range", "chapter_range", help="章节范围")
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="术语清单文件")
@click.option("-m", "--modified", type=int, help="只检查最近N天修改的文件")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml", "markdown", "md", "html", "csv"]),
              default=None, help="输出格式")
@click.option("-o", "--output", type=click.Path(), help="输出文件路径")
@click.pass_context
def terms_check(
    ctx: click.Context,
    folder: Optional[str],
    chapter_range: Optional[str],
    terms_file: Optional[str],
    modified: Optional[int],
    output_format: Optional[str],
    output: Optional[str],
):
    """检查术语使用一致性"""
    root = _get_folder(folder)
    cfg = _build_config(folder, chapter_range, output_format, output, terms_file,
                        modified, None, None, ctx.obj.get("config_path"))

    click.echo(f"📚 术语一致性检查 - 目录: {root}")

    terms = load_terms(cfg.terms_file)
    if not terms.terms:
        click.echo("⚠️  术语清单为空，请先使用 'doccheck terms add' 添加术语")
        click.echo("   或使用 'doccheck terms suggest' 从文档中自动发现")
        sys.exit(1)

    manager = TermManager(terms=terms, config=cfg)
    result = manager.check_consistency(root)

    fmt = cfg.output_format or "console"
    reporter = ReportGenerator(cfg)
    content = reporter.generate(result, fmt=fmt, output=cfg.output_path)

    if cfg.output_path:
        click.echo(f"✅ 报告已写入: {cfg.output_path}")
        if fmt == "console":
            click.echo()
            click.echo(content)
    else:
        click.echo(content)


@terms_group.command("suggest", help="从文档中发现可能的新术语建议")
@click.argument("folder", required=False, type=click.Path(exists=True, file_okay=False))
@click.option("-n", "--min-occurrences", type=int, default=3, help="最小出现次数 (默认: 3)")
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="现有术语清单")
@click.option("--add-all", is_flag=True, help="自动添加所有建议术语")
@click.pass_context
def terms_suggest(
    ctx: click.Context,
    folder: Optional[str],
    min_occurrences: int,
    terms_file: Optional[str],
    add_all: bool,
):
    """从文档中发现新术语建议"""
    root = _get_folder(folder)
    cfg = _build_config(folder, None, None, None, terms_file, None, None, None, ctx.obj.get("config_path"))

    click.echo(f"🔍 扫描文档发现术语建议 - 目录: {root}")

    terms = load_terms(cfg.terms_file)
    manager = TermManager(terms=terms, config=cfg)
    scanner = BaseScanner(config=cfg, terms=terms)
    result = scanner.scan(root)

    suggestions = manager.suggest_terms_from_scan(result, min_occurrences)

    if not suggestions:
        click.echo("✅ 未发现需要添加的新术语建议")
        return

    click.echo(f"📊 发现 {len(suggestions)} 个术语建议:")
    click.echo("")

    for i, s in enumerate(suggestions, 1):
        fo = s["first_occurrence"]
        loc = f"{fo['file']}:{fo['line']}" if fo.get("file") else "N/A"
        click.echo(f"  {i:3d}. [{s['suggested_category']}] {s['term']}  ({s['occurrences']} 次)")
        click.echo(f"       首次出现: {loc}")
        if add_all:
            manager.add_term(
                canonical=s["term"],
                category=s["suggested_category"],
                description=f"自动添加，出现{s['occurrences']}次",
            )
            click.echo(f"       → ✅ 已添加")
        click.echo("")

    if add_all:
        save_path = manager.save(cfg.terms_file)
        click.echo(f"📄 已更新术语清单: {save_path}")
    else:
        if click.confirm("是否将建议术语添加到清单？"):
            for s in suggestions:
                if click.confirm(f"  添加 \"{s['term']}\" (类别: {s['suggested_category']})？", default=True):
                    manager.add_term(
                        canonical=s["term"],
                        category=s["suggested_category"],
                    )
            save_path = manager.save(cfg.terms_file)
            click.echo(f"📄 已更新术语清单: {save_path}")


@terms_group.command("stats", help="显示术语使用统计")
@click.argument("folder", required=False, type=click.Path(exists=True, file_okay=False))
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="术语清单文件")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml"]), default="console", help="输出格式")
@click.option("-o", "--output", type=click.Path(), help="输出文件路径")
@click.pass_context
def terms_stats(
    ctx: click.Context,
    folder: Optional[str],
    terms_file: Optional[str],
    output_format: str,
    output: Optional[str],
):
    """显示术语使用统计"""
    root = _get_folder(folder)
    cfg = _build_config(folder, None, output_format, output, terms_file, None, None, None, ctx.obj.get("config_path"))

    terms = load_terms(cfg.terms_file)
    manager = TermManager(terms=terms, config=cfg)
    stats = manager.get_term_stats(root)

    if output_format == "json":
        content = json.dumps(stats, ensure_ascii=False, indent=2)
    elif output_format == "yaml":
        content = yaml.dump(stats, allow_unicode=True, default_flow_style=False, sort_keys=False)
    else:
        lines = []
        summary = stats["summary"]
        lines.append("📊 术语使用统计")
        lines.append("=" * 50)
        lines.append(f"术语总数: {summary['total_terms']}")
        lines.append("按类别:")
        for cat, cnt in summary["by_category"].items():
            lines.append(f"  - {cat}: {cnt}")
        lines.append(f"相关问题数: {summary['total_issues']}")
        lines.append("")
        lines.append("📚 各术语使用情况:")
        lines.append("-" * 50)
        for name, info in stats["terms"].items():
            status_icon = "✅" if info["status"] == "tracked" else "❓"
            cat = info.get("category", "untracked")
            total = info.get("total_variant_occurrences", info.get("occurrences", 0))
            lines.append(f"{status_icon} [{cat}] {name}: {total} 次")
            if "canonical_occurrences" in info:
                lines.append(f"     标准写法: {info['canonical_occurrences']} 次")
            if info.get("alias_occurrences"):
                for alias, cnt in info["alias_occurrences"].items():
                    lines.append(f"     别名 {alias}: {cnt} 次")
        content = "\n".join(lines)

    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", encoding="utf-8") as f:
            f.write(content)
        click.echo(f"✅ 已写入: {output}")
    else:
        click.echo(content)


# ==================== refs 命令 ====================

@main.command("refs", help="🔗 检查文件引用、图片说明、链接有效性")
@click.argument("folder", required=False, type=click.Path(exists=True, file_okay=False))
@click.option("-r", "--chapter-range", "chapter_range", help="章节范围")
@click.option("-m", "--modified", type=int, help="只检查最近N天修改的文件")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml", "markdown", "md", "html", "csv"]),
              default=None, help="输出格式")
@click.option("-o", "--output", type=click.Path(), help="输出文件路径")
@click.option("--extensions", help="文件扩展名，逗号分隔")
@click.option("--exclude", help="排除目录模式，逗号分隔")
@click.option("--stats-only", is_flag=True, help="只显示统计摘要")
@click.pass_context
def refs_cmd(
    ctx: click.Context,
    folder: Optional[str],
    chapter_range: Optional[str],
    modified: Optional[int],
    output_format: Optional[str],
    output: Optional[str],
    extensions: Optional[str],
    exclude: Optional[str],
    stats_only: bool,
):
    """检查引用和链接"""
    root = _get_folder(folder)
    cfg = _build_config(folder, chapter_range, output_format, output, None,
                        modified, extensions, exclude, ctx.obj.get("config_path"))

    click.echo(f"🔗 引用检查 - 目录: {root}")

    checker = ReferenceChecker(config=cfg)

    if stats_only:
        stats = checker.get_reference_stats(root)
        lines = []
        lines.append("📊 引用统计摘要")
        lines.append("=" * 40)
        lines.append(f"文档总数: {stats['total_documents']}")
        lines.append(f"图片总数: {stats['total_images']}")
        lines.append(f"  - 缺少说明: {stats['images_without_alt']}")
        lines.append(f"  - 文件缺失: {stats['missing_images']}")
        lines.append(f"链接总数: {stats['total_links']}")
        lines.append(f"  - 内部链接: {stats['internal_links']}")
        lines.append(f"  - 外部链接: {stats['external_links']}")
        lines.append(f"  - 可能失效: {stats['broken_links']}")
        click.echo("\n".join(lines))
        return

    result = checker.check(root)

    fmt = cfg.output_format or "console"
    reporter = ReportGenerator(cfg)
    content = reporter.generate(result, fmt=fmt, output=cfg.output_path)

    if cfg.output_path:
        click.echo(f"✅ 报告已写入: {cfg.output_path}")
        if fmt == "console":
            click.echo()
            click.echo(content)
    else:
        click.echo(content)

    if result.issue_count["error"] > 0:
        sys.exit(1)


# ==================== report 命令 ====================

@main.command("report", help="📋 生成完整检查报告，支持导出任务清单")
@click.argument("folder", required=False, type=click.Path(exists=True, file_okay=False))
@click.option("-r", "--chapter-range", "chapter_range", help="章节范围")
@click.option("-m", "--modified", type=int, help="只检查最近N天修改的文件")
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="术语清单文件")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml", "markdown", "md", "html", "csv", "tasks", "tasklist"]),
              default="markdown", help="输出格式 (默认: markdown)")
@click.option("-o", "--output", type=click.Path(), help="输出文件路径")
@click.option("--task-list", "task_list", type=click.Path(),
              help="额外导出可分配的编辑任务清单 (输出为 .md/.yaml/.json/.csv)")
@click.option("--assignee", multiple=True,
              help="分配规则，格式 '关键词=负责人' (可多次指定)")
@click.option("--extensions", help="文件扩展名，逗号分隔")
@click.option("--exclude", help="排除目录模式，逗号分隔")
@click.option("--include-scan/--no-scan", default=True, help="包含基础扫描")
@click.option("--include-terms/--no-terms", default=True, help="包含术语检查")
@click.option("--include-refs/--no-refs", default=True, help="包含引用检查")
@click.pass_context
def report_cmd(
    ctx: click.Context,
    folder: Optional[str],
    chapter_range: Optional[str],
    modified: Optional[int],
    terms_file: Optional[str],
    output_format: str,
    output: Optional[str],
    task_list: Optional[str],
    assignee: tuple,
    extensions: Optional[str],
    exclude: Optional[str],
    include_scan: bool,
    include_terms: bool,
    include_refs: bool,
):
    """生成综合报告"""
    root = _get_folder(folder)
    cfg = _build_config(folder, chapter_range, output_format, output, terms_file,
                        modified, extensions, exclude, ctx.obj.get("config_path"))

    click.echo(f"📋 生成综合报告 - 目录: {root}")

    terms = load_terms(cfg.terms_file)
    from .utils import CheckResult
    combined = CheckResult()
    import time as _time
    combined.start_time = _time.time()

    if include_scan:
        click.echo("  🔍 执行基础扫描...")
        scanner = BaseScanner(config=cfg, terms=terms)
        result = scanner.scan(root)
        combined.issues.extend(result.issues)
        combined.first_occurrences.update(result.first_occurrences)
        combined.suspected_synonyms.extend(result.suspected_synonyms)
        combined.files_scanned = result.files_scanned
        combined.term_stats = result.term_stats

    if include_terms and terms.terms:
        click.echo("  📚 执行术语检查...")
        manager = TermManager(terms=terms, config=cfg)
        result = manager.check_consistency(root)
        combined.issues.extend(result.issues)
        if not combined.files_scanned:
            combined.files_scanned = result.files_scanned

    if include_refs:
        click.echo("  🔗 执行引用检查...")
        checker = ReferenceChecker(config=cfg)
        result = checker.check(root)
        combined.issues.extend(result.issues)
        if not combined.files_scanned:
            combined.files_scanned = result.files_scanned

    combined.end_time = _time.time()
    combined.sort_issues()

    fmt = cfg.output_format
    reporter = ReportGenerator(cfg)
    content = reporter.generate(combined, fmt=fmt, output=cfg.output_path)

    if cfg.output_path:
        click.echo(f"✅ 报告已写入: {cfg.output_path}")
        if fmt in ("console", "text"):
            click.echo()
            click.echo(content)
    elif fmt not in ("console", "text"):
        default_name = f"doccheck-report.{fmt}"
        cfg.output_path = Path(default_name)
        reporter.generate(combined, fmt=fmt, output=cfg.output_path)
        click.echo(f"✅ 报告已写入: {cfg.output_path}")
    else:
        click.echo(content)

    if task_list:
        click.echo("  🎯 生成任务清单...")
        assignee_map = {}
        for rule in assignee:
            if "=" in rule:
                k, v = rule.split("=", 1)
                assignee_map[k.strip().lower()] = v.strip()
        tasks = reporter.generate_task_list(
            combined,
            output=Path(task_list),
            assignees=assignee_map,
        )
        click.echo(f"✅ 任务清单已导出: {task_list} ({len(tasks)} 个任务)")

    counts = combined.issue_count
    if counts["error"] > 0:
        sys.exit(1)


# ==================== 辅助命令 ====================

@main.command("init-config", help="⚙️  在当前目录生成默认配置文件")
@click.option("-f", "--force", is_flag=True, help="覆盖已存在的配置文件")
@click.option("-t", "--with-terms", is_flag=True, help="同时创建示例术语清单")
def init_config(force: bool, with_terms: bool):
    """生成默认配置文件"""
    cfg_path = Path.cwd() / ".doccheck.yaml"
    if cfg_path.exists() and not force:
        click.echo(f"⚠️  配置文件已存在: {cfg_path}")
        click.echo("   使用 --force 覆盖")
    else:
        default_cfg = CheckConfig()
        save_config(default_cfg, cfg_path)
        click.echo(f"✅ 已创建配置文件: {cfg_path}")

    if with_terms:
        terms_path = Path.cwd() / "terms.yaml"
        if terms_path.exists() and not force:
            click.echo(f"⚠️  术语清单已存在: {terms_path}")
        else:
            sample_terms = TermList()
            from .config import TermDefinition
            sample_terms.add(TermDefinition(
                canonical="张三",
                category="character",
                aliases=["小张", "张先生"],
                description="男主角",
                severity="warning",
            ))
            sample_terms.add(TermDefinition(
                canonical="北京市",
                category="location",
                aliases=["北京", "京城"],
                description="故事背景城市",
                severity="warning",
            ))
            save_terms(sample_terms, terms_path)
            click.echo(f"✅ 已创建示例术语清单: {terms_path}")


if __name__ == "__main__":
    main()
