"""CLI入口 - 四大命令: scan、terms、refs、report"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Optional

import click
import yaml

from . import __version__
from .config import (
    DEFAULT_IGNORE_FILENAME,
    DEFAULT_SNAPSHOT_FILENAME,
    DEFAULT_TASKSTATES_FILENAME,
    CheckConfig,
    IgnoreRules,
    TermDefinition,
    TermList,
    add_ignore_rule,
    generate_sample_ignore_rules,
    load_config,
    load_ignore_rules,
    load_snapshot,
    load_task_states,
    load_terms,
    save_config,
    save_ignore_rules,
    save_snapshot as _save_snapshot_func,
    save_task_states as _save_task_states_func,
    save_terms,
)
from .refs import ReferenceChecker
from .report import ReportGenerator
from .scanner import BaseScanner
from .terms import TermManager
from .utils import ChapterFilter, ReportSnapshot, TaskStatus, TASK_STATUS_LABELS


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
    ignore_file: Optional[str] = None,
) -> CheckConfig:
    """根据命令行参数构建配置（统一使用chapter_filter_str）"""
    cfg = load_config(Path(config_path) if config_path else None)

    if chapter_range:
        cfg.chapter_filter_str = chapter_range
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
    if ignore_file:
        cfg.ignore_file = Path(ignore_file)

    return cfg


def _resolve_ignore_file(explicit: Optional[str], cfg: CheckConfig) -> Optional[Path]:
    """决定实际使用的忽略规则文件路径"""
    if explicit:
        return Path(explicit)
    if cfg.ignore_file:
        return Path(cfg.ignore_file) if isinstance(cfg.ignore_file, str) else cfg.ignore_file
    default = Path.cwd() / DEFAULT_IGNORE_FILENAME
    if default.exists():
        return default
    return None


def _load_ignore_safely(path: Optional[Path]) -> Optional[IgnoreRules]:
    """安全加载忽略规则，文件不存在返回None"""
    if path is None:
        return None
    if not path.exists():
        return None
    try:
        return load_ignore_rules(path)
    except Exception as e:
        click.echo(f"⚠️  加载忽略规则文件失败: {e}", err=True)
        return None


def _get_folder(folder: Optional[str]) -> Path:
    """获取扫描的根目录"""
    return Path(folder).resolve() if folder else Path.cwd()


@click.group(help="📋 文档一致性检查工具 - 批量检查长篇文档的一致性问题")
@click.version_option(__version__, "-V", "--version", prog_name="doccheck")
@click.option("-c", "--config", "config_path", type=click.Path(), help="配置文件路径")
@click.option("-i", "--ignore-file", type=click.Path(),
              help=f"忽略规则文件路径 (默认: 查找当前目录下的 {DEFAULT_IGNORE_FILENAME})")
@click.pass_context
def main(ctx: click.Context, config_path: Optional[str], ignore_file: Optional[str]):
    """文档一致性检查工具主入口"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["ignore_file"] = ignore_file


# ==================== scan 命令 ====================

@main.command("scan", help="🔍 扫描文档，检查章节编号、重复标题、专有名词一致性")
@click.argument("folder", required=False, type=click.Path(exists=True, file_okay=False))
@click.option("-r", "--chapter-range", "chapter_range",
              help="章节范围：'5'=仅第5章，'5-10'=第5到10章")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml", "markdown", "md", "html", "csv"]),
              default=None, help="输出格式 (默认: console)")
@click.option("-o", "--output", type=click.Path(), help="输出文件路径")
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="术语清单文件")
@click.option("-m", "--modified", type=int, help="只检查最近N天修改过的文件")
@click.option("--extensions", help="文件扩展名，逗号分隔 (默认: .md,.markdown,.txt)")
@click.option("--exclude", help="排除目录模式，逗号分隔")
@click.option("--no-dedup", is_flag=True, help="禁用问题去重")
@click.option("--no-ignore", is_flag=True, help="禁用忽略规则")
@click.option("--diff/--no-diff", default=False, help="对比上次报告，显示新增/已解决/仍未处理")
@click.option("--save-snapshot/--no-save-snapshot", default=True,
              help="运行后是否保存快照（用于下次 diff 对比）")
@click.option("--snapshot-file", type=click.Path(),
              help=f"快照文件路径 (默认: {DEFAULT_SNAPSHOT_FILENAME})")
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
    no_dedup: bool,
    no_ignore: bool,
    diff: bool,
    save_snapshot: bool,
    snapshot_file: Optional[str],
    quiet: bool,
):
    """扫描文档并执行基础检查"""
    root = _get_folder(folder)
    cfg = _build_config(folder, chapter_range, output_format, output, terms_file,
                        modified, extensions, exclude,
                        ctx.obj.get("config_path"), ctx.obj.get("ignore_file"))
    cfg.enable_dedup = not no_dedup
    cfg.enable_ignore = not no_ignore

    ignore_path = _resolve_ignore_file(ctx.obj.get("ignore_file"), cfg)
    ignore_rules = _load_ignore_safely(ignore_path) if cfg.enable_ignore else None

    # 加载上次快照（用于diff）
    previous_snap = None
    if diff:
        snap_path = Path(snapshot_file) if snapshot_file else None
        previous_snap = load_snapshot(snap_path)
        if previous_snap is None and not quiet:
            click.echo("ℹ️  未找到上次快照，本次将生成基线快照")

    if not quiet:
        click.echo(f"📁 扫描目录: {root}")
        if cfg.chapter_filter_str:
            cf = ChapterFilter.parse(cfg.chapter_filter_str)
            click.echo(f"📖 章节筛选: {cf.describe()}")
        if cfg.modified_within_days:
            click.echo(f"🕐 仅最近 {cfg.modified_within_days} 天修改的文件")
        if ignore_rules and ignore_path:
            click.echo(f"🔕 加载忽略规则: {ignore_path} ({len(ignore_rules.rules)} 条)")
        if diff and previous_snap:
            click.echo(f"📸 对比快照: {previous_snap.created_at or '未知时间'}")

    terms = load_terms(cfg.terms_file)
    scanner = BaseScanner(config=cfg, terms=terms, ignore_rules=ignore_rules)
    result = scanner.scan(root)

    fmt = cfg.output_format or "console"
    out_path = cfg.output_path

    reporter = ReportGenerator(cfg, previous_snapshot=previous_snap)
    content = reporter.generate(result, fmt=fmt, output=out_path)

    # 保存快照
    if save_snapshot:
        current_snap = ReportSnapshot.from_result(result)
        snap_path = Path(snapshot_file) if snapshot_file else None
        saved_path = _save_snapshot_func(current_snap, snap_path)
        if not quiet:
            click.echo(f"📸 已保存快照: {saved_path}")

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
            parts = [f"  [{t.category}] {t.canonical}"]
            extra = []
            if t.aliases:
                extra.append(f"别名:{', '.join(t.aliases)}")
            if t.allowed_variants:
                extra.append(f"允许:{', '.join(t.allowed_variants)}")
            if t.forbidden_writings:
                extra.append(f"禁用:{', '.join(t.forbidden_writings)}")
            if extra:
                parts.append(f"（{' | '.join(extra)}）")
            if t.description:
                parts.append(f" - {t.description}")
            if t.report_aliases:
                parts.append(" [报告别名]")
            lines.append("".join(parts))
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
@click.option("-a", "--alias", multiple=True, help="别名 (对话用，默认不提示)")
@click.option("--allow-variant", multiple=True, help="允许的变体写法 (可用，低优先级提示)")
@click.option("--forbid", "--forbidden-writing", "forbidden", multiple=True,
              help="禁用写法 (必须修改，ERROR级别)")
@click.option("--report-aliases", is_flag=True, help="报告别名出现的位置（默认不报告别名）")
@click.option("-d", "--description", default="", help="描述说明")
@click.option("-s", "--severity", default="warning",
              type=click.Choice(["error", "warning", "info"]), help="违规严重程度")
@click.option("-t", "--terms", "terms_file", type=click.Path(), help="术语清单文件路径")
@click.pass_context
def terms_add(
    ctx: click.Context,
    canonical: str,
    category: str,
    alias: tuple,
    allow_variant: tuple,
    forbidden: tuple,
    report_aliases: bool,
    description: str,
    severity: str,
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
        forbidden_writings=list(forbidden),
        report_aliases=report_aliases,
    )

    save_path = manager.save(cfg.terms_file)
    click.echo(f"✅ 已添加术语: [{term_def.category}] {term_def.canonical}")
    if term_def.aliases:
        click.echo(f"   别名: {', '.join(term_def.aliases)}")
    if term_def.allowed_variants:
        click.echo(f"   允许变体: {', '.join(term_def.allowed_variants)}")
    if term_def.forbidden_writings:
        click.echo(f"   禁用写法: {', '.join(term_def.forbidden_writings)}")
    if term_def.report_aliases:
        click.echo("   ⚠️  别名出现位置将在报告中提示")
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
@click.option("-r", "--chapter-range", "chapter_range",
              help="章节范围：'5'=仅第5章，'5-10'=第5到10章")
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="术语清单文件")
@click.option("-m", "--modified", type=int, help="只检查最近N天修改的文件")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml", "markdown", "md", "html", "csv"]),
              default=None, help="输出格式")
@click.option("-o", "--output", type=click.Path(), help="输出文件路径")
@click.option("--no-dedup", is_flag=True, help="禁用问题去重")
@click.option("--no-ignore", is_flag=True, help="禁用忽略规则")
@click.pass_context
def terms_check(
    ctx: click.Context,
    folder: Optional[str],
    chapter_range: Optional[str],
    terms_file: Optional[str],
    modified: Optional[int],
    output_format: Optional[str],
    output: Optional[str],
    no_dedup: bool,
    no_ignore: bool,
):
    """检查术语使用一致性"""
    root = _get_folder(folder)
    cfg = _build_config(folder, chapter_range, output_format, output, terms_file,
                        modified, None, None,
                        ctx.obj.get("config_path"), ctx.obj.get("ignore_file"))
    cfg.enable_dedup = not no_dedup
    cfg.enable_ignore = not no_ignore

    ignore_path = _resolve_ignore_file(ctx.obj.get("ignore_file"), cfg)
    ignore_rules = _load_ignore_safely(ignore_path) if cfg.enable_ignore else None

    click.echo(f"📚 术语一致性检查 - 目录: {root}")
    if cfg.chapter_filter_str:
        from .utils import ChapterFilter
        cf = ChapterFilter.parse(cfg.chapter_filter_str)
        click.echo(f"📖 章节筛选: {cf.describe()}")
    if ignore_rules and ignore_path:
        click.echo(f"🔕 加载忽略规则: {ignore_path} ({len(ignore_rules.rules)} 条)")

    terms = load_terms(cfg.terms_file)
    if not terms.terms:
        click.echo("⚠️  术语清单为空，请先使用 'doccheck terms add' 添加术语")
        click.echo("   或使用 'doccheck terms suggest' 从文档中自动发现")
        sys.exit(1)

    manager = TermManager(terms=terms, config=cfg, ignore_rules=ignore_rules)
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
@click.option("-r", "--chapter-range", "chapter_range", help="章节范围")
@click.option("-n", "--min-occurrences", type=int, default=3, help="最小出现次数 (默认: 3)")
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="现有术语清单")
@click.option("--add-all", is_flag=True, help="自动添加所有建议术语")
@click.pass_context
def terms_suggest(
    ctx: click.Context,
    folder: Optional[str],
    chapter_range: Optional[str],
    min_occurrences: int,
    terms_file: Optional[str],
    add_all: bool,
):
    """从文档中发现新术语建议"""
    root = _get_folder(folder)
    cfg = _build_config(folder, chapter_range, None, None, terms_file,
                        None, None, None,
                        ctx.obj.get("config_path"), ctx.obj.get("ignore_file"))

    ignore_path = _resolve_ignore_file(ctx.obj.get("ignore_file"), cfg)
    ignore_rules = _load_ignore_safely(ignore_path) if cfg.enable_ignore else None

    click.echo(f"🔍 扫描文档发现术语建议 - 目录: {root}")

    terms = load_terms(cfg.terms_file)
    manager = TermManager(terms=terms, config=cfg, ignore_rules=ignore_rules)
    scanner = BaseScanner(config=cfg, terms=terms, ignore_rules=ignore_rules)
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
@click.option("-r", "--chapter-range", "chapter_range", help="章节范围")
@click.option("-t", "--terms", "terms_file", type=click.Path(exists=True), help="术语清单文件")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml"]), default="console", help="输出格式")
@click.option("-o", "--output", type=click.Path(), help="输出文件路径")
@click.pass_context
def terms_stats(
    ctx: click.Context,
    folder: Optional[str],
    chapter_range: Optional[str],
    terms_file: Optional[str],
    output_format: str,
    output: Optional[str],
):
    """显示术语使用统计"""
    root = _get_folder(folder)
    cfg = _build_config(folder, chapter_range, output_format, output, terms_file,
                        None, None, None,
                        ctx.obj.get("config_path"), ctx.obj.get("ignore_file"))

    ignore_path = _resolve_ignore_file(ctx.obj.get("ignore_file"), cfg)
    ignore_rules = _load_ignore_safely(ignore_path) if cfg.enable_ignore else None

    terms = load_terms(cfg.terms_file)
    manager = TermManager(terms=terms, config=cfg, ignore_rules=ignore_rules)
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
        vtype = summary.get("by_variant_type", {})
        if vtype:
            lines.append("按写法类型:")
            vt_labels = {
                "canonical": "标准写法",
                "alias": "别名",
                "allowed": "允许变体",
                "forbidden": "禁用写法",
                "untracked": "未收录",
            }
            for k, v in vtype.items():
                if v > 0:
                    lines.append(f"  - {vt_labels.get(k, k)}: {v} 次")
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
                lines.append(f"     📌 标准写法: {info['canonical_occurrences']} 次")
            if info.get("alias_occurrences"):
                for alias, cnt in info["alias_occurrences"].items():
                    lines.append(f"     💬 别名 '{alias}': {cnt} 次")
            if info.get("allowed_occurrences"):
                for v, cnt in info["allowed_occurrences"].items():
                    lines.append(f"     ✔️  允许 '{v}': {cnt} 次")
            if info.get("forbidden_occurrences"):
                for v, cnt in info["forbidden_occurrences"].items():
                    lines.append(f"     ❌ 禁用 '{v}': {cnt} 次")
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
@click.option("-r", "--chapter-range", "chapter_range",
              help="章节范围：'5'=仅第5章，'5-10'=第5到10章")
@click.option("-m", "--modified", type=int, help="只检查最近N天修改的文件")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml", "markdown", "md", "html", "csv"]),
              default=None, help="输出格式")
@click.option("-o", "--output", type=click.Path(), help="输出文件路径")
@click.option("--extensions", help="文件扩展名，逗号分隔")
@click.option("--exclude", help="排除目录模式，逗号分隔")
@click.option("--stats-only", is_flag=True, help="只显示统计摘要")
@click.option("--no-dedup", is_flag=True, help="禁用问题去重")
@click.option("--no-ignore", is_flag=True, help="禁用忽略规则")
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
    no_dedup: bool,
    no_ignore: bool,
):
    """检查引用和链接"""
    root = _get_folder(folder)
    cfg = _build_config(folder, chapter_range, output_format, output, None,
                        modified, extensions, exclude,
                        ctx.obj.get("config_path"), ctx.obj.get("ignore_file"))
    cfg.enable_dedup = not no_dedup
    cfg.enable_ignore = not no_ignore

    ignore_path = _resolve_ignore_file(ctx.obj.get("ignore_file"), cfg)
    ignore_rules = _load_ignore_safely(ignore_path) if cfg.enable_ignore else None

    click.echo(f"🔗 引用检查 - 目录: {root}")
    if cfg.chapter_filter_str:
        from .utils import ChapterFilter
        cf = ChapterFilter.parse(cfg.chapter_filter_str)
        click.echo(f"📖 章节筛选: {cf.describe()}")
    if ignore_rules and ignore_path:
        click.echo(f"🔕 加载忽略规则: {ignore_path} ({len(ignore_rules.rules)} 条)")

    checker = ReferenceChecker(config=cfg, ignore_rules=ignore_rules)

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
@click.option("-r", "--chapter-range", "chapter_range",
              help="章节范围：'5'=仅第5章，'5-10'=第5到10章")
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
@click.option("--no-dedup", is_flag=True, help="禁用问题去重")
@click.option("--no-ignore", is_flag=True, help="禁用忽略规则")
@click.option("--diff/--no-diff", default=False, help="对比上次报告，显示新增/已解决/仍未处理")
@click.option("--save-snapshot/--no-save-snapshot", default=True,
              help="运行后是否保存快照（用于下次 diff 对比）")
@click.option("--snapshot-file", type=click.Path(),
              help=f"快照文件路径 (默认: {DEFAULT_SNAPSHOT_FILENAME})")
@click.option("--task-states/--no-task-states", default=True,
              help="是否加载并使用任务状态存储（沿用编辑状态）")
@click.option("--task-states-file", type=click.Path(),
              help=f"任务状态文件路径 (默认: {DEFAULT_TASKSTATES_FILENAME})")
@click.option("--sync-task-states/--no-sync-task-states", default=False,
              help="生成报告时是否同步新问题到任务状态文件（新增为待处理）")
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
    no_dedup: bool,
    no_ignore: bool,
    diff: bool,
    save_snapshot: bool,
    snapshot_file: Optional[str],
    task_states: bool,
    task_states_file: Optional[str],
    sync_task_states: bool,
):
    """生成综合报告"""
    root = _get_folder(folder)
    cfg = _build_config(folder, chapter_range, output_format, output, terms_file,
                        modified, extensions, exclude,
                        ctx.obj.get("config_path"), ctx.obj.get("ignore_file"))
    cfg.enable_dedup = not no_dedup
    cfg.enable_ignore = not no_ignore

    ignore_path = _resolve_ignore_file(ctx.obj.get("ignore_file"), cfg)
    ignore_rules = _load_ignore_safely(ignore_path) if cfg.enable_ignore else None

    # 加载上次快照（用于diff）
    previous_snap = None
    if diff:
        snap_path = Path(snapshot_file) if snapshot_file else None
        previous_snap = load_snapshot(snap_path)
        if previous_snap is None:
            click.echo("ℹ️  未找到上次快照，本次将生成基线快照")

    # 加载任务状态存储
    task_store = None
    task_states_path = None
    if task_states:
        task_states_path = Path(task_states_file) if task_states_file else None
        task_store = load_task_states(task_states_path)
        if task_store.states:
            click.echo(f"📝 加载任务状态: {len(task_store.states)} 条记录")

    click.echo(f"📋 生成综合报告 - 目录: {root}")
    if cfg.chapter_filter_str:
        cf = ChapterFilter.parse(cfg.chapter_filter_str)
        click.echo(f"📖 章节筛选: {cf.describe()}")
    if ignore_rules and ignore_path:
        click.echo(f"🔕 加载忽略规则: {ignore_path} ({len(ignore_rules.rules)} 条)")
    if diff and previous_snap:
        click.echo(f"📸 对比快照: {previous_snap.created_at or '未知时间'}")

    terms = load_terms(cfg.terms_file)
    from .utils import CheckResult, deduplicate_issues
    combined = CheckResult()
    import time as _time
    combined.start_time = _time.time()

    if include_scan:
        click.echo("  🔍 执行基础扫描...")
        scanner = BaseScanner(config=cfg, terms=terms, ignore_rules=ignore_rules)
        result = scanner.scan(root)
        combined.issues.extend(result.issues)
        combined.first_occurrences.update(result.first_occurrences)
        combined.suspected_synonyms.extend(result.suspected_synonyms)
        combined.files_scanned = result.files_scanned
        combined.term_stats = result.term_stats
        if result.ignore_stats:
            combined.ignore_stats = _merge_stats(combined.ignore_stats, result.ignore_stats)
        if result.dedup_stats:
            combined.dedup_stats = _merge_stats(combined.dedup_stats, result.dedup_stats)

    if include_terms and terms.terms:
        click.echo("  📚 执行术语检查...")
        manager = TermManager(terms=terms, config=cfg, ignore_rules=ignore_rules)
        result = manager.check_consistency(root)
        combined.issues.extend(result.issues)
        if not combined.files_scanned:
            combined.files_scanned = result.files_scanned
        if result.ignore_stats:
            combined.ignore_stats = _merge_stats(combined.ignore_stats, result.ignore_stats)
        if result.dedup_stats:
            combined.dedup_stats = _merge_stats(combined.dedup_stats, result.dedup_stats)

    if include_refs:
        click.echo("  🔗 执行引用检查...")
        checker = ReferenceChecker(config=cfg, ignore_rules=ignore_rules)
        result = checker.check(root)
        combined.issues.extend(result.issues)
        if not combined.files_scanned:
            combined.files_scanned = result.files_scanned
        if result.ignore_stats:
            combined.ignore_stats = _merge_stats(combined.ignore_stats, result.ignore_stats)
        if result.dedup_stats:
            combined.dedup_stats = _merge_stats(combined.dedup_stats, result.dedup_stats)

    if cfg.enable_dedup and (include_scan or include_terms or include_refs):
        click.echo("  🧩 跨模块去重...")
        deduped, dedup_stats = deduplicate_issues(combined.issues)
        combined.issues = deduped
        combined.dedup_stats = _merge_stats(combined.dedup_stats, dedup_stats)

    combined.end_time = _time.time()
    combined.sort_issues()

    fmt = cfg.output_format
    reporter = ReportGenerator(cfg, task_store=task_store, previous_snapshot=previous_snap)
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
            sync_to_store=False,
        )
        click.echo(f"✅ 任务清单已导出: {task_list} ({len(tasks)} 个任务)")

    # 同步任务状态
    if task_store and sync_task_states:
        new_count = reporter.sync_issues_to_store(combined)
        ts_path = Path(task_states_file) if task_states_file else None
        if ts_path is None:
            ts_path = Path.cwd() / DEFAULT_TASKSTATES_FILENAME
        _save_task_states_func(task_store, ts_path)
        click.echo(f"📝 任务状态已同步: {ts_path} ({len(task_store.states)} 条, 新增 {new_count} 条)")

    # 保存快照
    if save_snapshot:
        current_snap = ReportSnapshot.from_result(combined)
        snap_path = Path(snapshot_file) if snapshot_file else None
        saved_path = _save_snapshot_func(current_snap, snap_path)
        click.echo(f"📸 已保存快照: {saved_path}")

    counts = combined.issue_count
    if counts["error"] > 0:
        sys.exit(1)


def _merge_stats(base: Optional[dict], extra: dict) -> dict:
    """合并两个统计字典（累加数值）"""
    if base is None:
        base = {}
    for k, v in extra.items():
        if isinstance(v, dict):
            base[k] = _merge_stats(base.get(k, {}), v)
        elif isinstance(v, int):
            base[k] = base.get(k, 0) + v
        else:
            base[k] = v
    return base


# ==================== tasks 命令组 ====================

@main.group("tasks", help="📝 管理编辑任务状态（待处理/处理中/已修复/已忽略）")
@click.option("--task-states-file", type=click.Path(),
              help=f"任务状态文件路径 (默认: {DEFAULT_TASKSTATES_FILENAME})")
@click.pass_context
def tasks_group(ctx: click.Context, task_states_file: Optional[str]):
    """任务状态管理命令组"""
    ctx.ensure_object(dict)
    ctx.obj["task_states_file"] = task_states_file


def _load_task_states_from_ctx(ctx: click.Context):
    """从上下文加载任务状态存储"""
    ts_file = ctx.obj.get("task_states_file")
    path = Path(ts_file) if ts_file else None
    store = load_task_states(path)
    return store, path


def _save_task_states_from_ctx(store, ctx: click.Context):
    """保存任务状态到上下文指定路径"""
    ts_file = ctx.obj.get("task_states_file")
    path = Path(ts_file) if ts_file else Path.cwd() / DEFAULT_TASKSTATES_FILENAME
    _save_task_states_func(store, path)
    return path


@tasks_group.command("list", help="列出所有任务状态")
@click.option("-s", "--status", help="按状态筛选 (pending/in_progress/fixed/ignored)")
@click.option("-f", "--format", "output_format",
              type=click.Choice(["console", "json", "yaml"]),
              default="console", help="输出格式")
@click.pass_context
def tasks_list_cmd(ctx: click.Context, status: Optional[str], output_format: str):
    """列出任务状态"""
    store, _ = _load_task_states_from_ctx(ctx)

    if not store.states:
        click.echo("ℹ️  暂无任务状态记录")
        return

    filtered = list(store.states.items())
    if status:
        filtered = [(sid, rec) for sid, rec in filtered if rec.status == status]

    if output_format == "json":
        click.echo(json.dumps(store.to_dict(), ensure_ascii=False, indent=2))
    elif output_format == "yaml":
        click.echo(yaml.dump(store.to_dict(), allow_unicode=True, default_flow_style=False, sort_keys=False))
    else:
        from collections import Counter
        cnt = Counter(rec.status for rec in store.states.values())
        click.echo(f"📝 共 {len(store.states)} 个任务")
        for st, c in cnt.most_common():
            label = TASK_STATUS_LABELS.get(TaskStatus(st), st)
            click.echo(f"  {label}: {c}")
        click.echo()

        if filtered:
            click.echo(f"--- 显示 {len(filtered)} 条 ---")
            for sid, rec in sorted(filtered, key=lambda x: (x[1].status, x[0])):
                label = TASK_STATUS_LABELS.get(TaskStatus(rec.status), rec.status)
                assignee = f" [{rec.assignee}]" if rec.assignee else ""
                updated = f" 更新: {rec.updated_at}" if rec.updated_at else ""
                click.echo(f"  {sid} | {label}{assignee} |{updated}")


@tasks_group.command("set-status", help="设置任务状态")
@click.argument("stable_id")
@click.argument("status")
@click.option("--assignee", help="指定负责人")
@click.option("--note", help="添加备注")
@click.pass_context
def tasks_set_status(ctx: click.Context, stable_id: str, status: str,
                     assignee: Optional[str], note: Optional[str]):
    """设置单个任务的状态

    \b
    STATUS 可选值:
      pending     待处理
      in_progress 处理中
      fixed       已修复
      ignored     已忽略
    """
    valid_statuses = {s.value for s in TaskStatus}
    if status not in valid_statuses:
        click.echo(f"❌ 无效状态: {status}")
        click.echo(f"   有效值: {', '.join(sorted(valid_statuses))}")
        sys.exit(1)

    store, _ = _load_task_states_from_ctx(ctx)
    store.set_status(stable_id, status, assignee=assignee, note=note)
    path = _save_task_states_from_ctx(store, ctx)

    label = TASK_STATUS_LABELS.get(TaskStatus(status), status)
    click.echo(f"✅ 任务 {stable_id} 状态已更新为: {label}")
    click.echo(f"   保存至: {path}")


@tasks_group.command("ignore", help="将任务标记为已忽略")
@click.argument("stable_id")
@click.option("--reason", help="忽略原因")
@click.pass_context
def tasks_ignore_cmd(ctx: click.Context, stable_id: str, reason: Optional[str]):
    """将指定任务标记为已忽略"""
    store, _ = _load_task_states_from_ctx(ctx)
    store.set_status(stable_id, TaskStatus.IGNORED.value, note=reason)
    path = _save_task_states_from_ctx(store, ctx)
    click.echo(f"✅ 任务 {stable_id} 已标记为「已忽略」")
    if reason:
        click.echo(f"   原因: {reason}")
    click.echo(f"   保存至: {path}")


@tasks_group.command("fix", help="将任务标记为已修复")
@click.argument("stable_id")
@click.option("--note", help="修复说明")
@click.pass_context
def tasks_fix_cmd(ctx: click.Context, stable_id: str, note: Optional[str]):
    """将指定任务标记为已修复"""
    store, _ = _load_task_states_from_ctx(ctx)
    store.set_status(stable_id, TaskStatus.FIXED.value, note=note)
    path = _save_task_states_from_ctx(store, ctx)
    click.echo(f"✅ 任务 {stable_id} 已标记为「已修复」")
    if note:
        click.echo(f"   说明: {note}")
    click.echo(f"   保存至: {path}")


@tasks_group.command("progress", help="将任务标记为处理中")
@click.argument("stable_id")
@click.option("--assignee", help="指定负责人")
@click.pass_context
def tasks_progress_cmd(ctx: click.Context, stable_id: str, assignee: Optional[str]):
    """将指定任务标记为处理中"""
    store, _ = _load_task_states_from_ctx(ctx)
    store.set_status(stable_id, TaskStatus.IN_PROGRESS.value, assignee=assignee)
    path = _save_task_states_from_ctx(store, ctx)
    click.echo(f"✅ 任务 {stable_id} 已标记为「处理中」")
    if assignee:
        click.echo(f"   负责人: {assignee}")
    click.echo(f"   保存至: {path}")


# ==================== 辅助命令 ====================

@main.command("init-config", help="⚙️  在当前目录生成默认配置文件和示例文件")
@click.option("-f", "--force", is_flag=True, help="覆盖已存在的配置文件")
@click.option("-t", "--with-terms", is_flag=True, help="同时创建示例术语清单")
@click.option("-i", "--with-ignore", is_flag=True, help="同时创建示例忽略规则文件")
def init_config(force: bool, with_terms: bool, with_ignore: bool):
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
            sample_terms.add(TermDefinition(
                canonical="李明轩",
                category="character",
                aliases=["明轩", "轩哥", "李教授"],
                allowed_variants=["李明軒"],
                forbidden_writings=["李明暄", "李鸣轩"],
                description="男主角，年轻的考古学教授",
                severity="warning",
                report_aliases=False,
            ))
            sample_terms.add(TermDefinition(
                canonical="云隐城",
                category="location",
                aliases=["云隐", "古城"],
                forbidden_writings=["云影城"],
                description="故事主要发生地，千年古城",
                severity="warning",
            ))
            sample_terms.add(TermDefinition(
                canonical="龙纹玉佩",
                category="proper_noun",
                aliases=["玉佩"],
                forbidden_writings=["龙纹玉配", "龙文玉佩"],
                description="贯穿全文的关键道具",
                severity="error",
                report_aliases=True,
            ))
            save_terms(sample_terms, terms_path)
            click.echo(f"✅ 已创建示例术语清单: {terms_path}")

    if with_ignore:
        ignore_path = Path.cwd() / DEFAULT_IGNORE_FILENAME
        if ignore_path.exists() and not force:
            click.echo(f"⚠️  忽略规则文件已存在: {ignore_path}")
        else:
            sample_ignore = generate_sample_ignore_rules()
            save_ignore_rules(sample_ignore, ignore_path)
            click.echo(f"✅ 已创建示例忽略规则: {ignore_path} ({len(sample_ignore.rules)} 条规则)")


if __name__ == "__main__":
    main()
