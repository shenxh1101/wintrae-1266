"""配置管理模块"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .utils import (
    IgnoreRule,
    IgnoreRules,
    is_substring_of_canonical,
    TermVariantInfo,
    TaskStateStore,
    ReportSnapshot,
)


DEFAULT_TERMS_FILENAME = "terms.yaml"
DEFAULT_CONFIG_FILENAME = ".doccheck.yaml"
DEFAULT_IGNORE_FILENAME = ".doccheck-ignore.yaml"


@dataclass
class TermDefinition:
    """
    术语定义 - 明确区分各类写法：
    - canonical: 标准（规范）写法，推荐使用
    - aliases: 别名，允许在对话等非正式场景使用（不强制修改，仅提示）
    - allowed_variants: 允许变体，偶尔可用但推荐统一（低级提醒）
    - forbidden_writings: 禁用写法，出现必须修改（高级别错误）
    """
    canonical: str
    category: str = "general"
    aliases: List[str] = field(default_factory=list)
    allowed_variants: List[str] = field(default_factory=list)
    forbidden_writings: List[str] = field(default_factory=list)
    description: str = ""
    severity: str = "warning"
    report_aliases: bool = False

    def classify_variant(self, variant: str, all_canonicals: Optional[List[str]] = None) -> Optional[TermVariantInfo]:
        """
        判断一个写法属于哪一类，返回分类信息。
        返回 None 表示此变体与该术语无关。
        判断优先级：标准写法 > 禁用写法 > 允许变体 > 别名 > 子串（含于标准写法但未显式配置）
        """
        v = variant.strip()
        if not v:
            return None

        if v.lower() == self.canonical.lower():
            return TermVariantInfo(
                canonical=self.canonical,
                variant=v,
                variant_type="canonical",
                category=self.category,
                description=self.description,
                severity=self.severity,
                needs_edit=False,
            )

        for fw in self.forbidden_writings:
            if v.lower() == fw.lower():
                return TermVariantInfo(
                    canonical=self.canonical,
                    variant=v,
                    variant_type="forbidden",
                    category=self.category,
                    description=self.description,
                    severity="error",
                    needs_edit=True,
                )

        for av in self.allowed_variants:
            if v.lower() == av.lower():
                return TermVariantInfo(
                    canonical=self.canonical,
                    variant=v,
                    variant_type="allowed",
                    category=self.category,
                    description=self.description,
                    severity="info",
                    needs_edit=False,
                )

        for alias in self.aliases:
            if v.lower() == alias.lower():
                if self.report_aliases:
                    return TermVariantInfo(
                        canonical=self.canonical,
                        variant=v,
                        variant_type="alias",
                        category=self.category,
                        description=self.description,
                        severity="suggestion",
                        needs_edit=False,
                    )
                return None

        if is_substring_of_canonical(v, self.canonical, all_canonicals):
            return TermVariantInfo(
                canonical=self.canonical,
                variant=v,
                variant_type="substring_canonical",
                category=self.category,
                description=self.description,
                severity=self.severity,
                needs_edit=False,
            )

        return None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"canonical": self.canonical}
        if self.category != "general":
            d["category"] = self.category
        if self.aliases:
            d["aliases"] = self.aliases
        if self.allowed_variants:
            d["allowed_variants"] = self.allowed_variants
        if self.forbidden_writings:
            d["forbidden_writings"] = self.forbidden_writings
        if self.description:
            d["description"] = self.description
        if self.severity != "warning":
            d["severity"] = self.severity
        if self.report_aliases:
            d["report_aliases"] = True
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TermDefinition":
        return cls(
            canonical=str(data["canonical"]),
            category=str(data.get("category", "general")),
            aliases=list(data.get("aliases", [])),
            allowed_variants=list(data.get("allowed_variants", [])),
            forbidden_writings=list(data.get("forbidden_writings", [])),
            description=str(data.get("description", "")),
            severity=str(data.get("severity", "warning")),
            report_aliases=bool(data.get("report_aliases", False)),
        )


@dataclass
class TermList:
    """术语清单"""
    terms: Dict[str, TermDefinition] = field(default_factory=dict)
    categories: List[str] = field(default_factory=lambda: ["character", "location", "proper_noun", "general", "organization"])

    def add(self, term: TermDefinition) -> None:
        key = term.canonical.lower()
        self.terms[key] = term
        if term.category not in self.categories:
            self.categories.append(term.category)

    def get(self, name: str) -> Optional[TermDefinition]:
        return self.terms.get(name.lower())

    @property
    def all_canonical_strings(self) -> List[str]:
        """获取所有标准写法字符串列表"""
        return [t.canonical for t in self.terms.values()]

    def classify_any_variant(self, variant: str) -> Optional[TermVariantInfo]:
        """在全部术语中查找匹配的变体分类"""
        canonicals = self.all_canonical_strings
        for term_def in self.terms.values():
            info = term_def.classify_variant(variant, canonicals)
            if info is not None:
                return info
        return None

    def all_checkable_variants(self) -> Dict[str, TermVariantInfo]:
        """
        获取所有需要检查的变体 -> 分类信息映射。
        不包含：标准写法、标准写法子串、未开启report_aliases的别名
        """
        result: Dict[str, TermVariantInfo] = {}
        canonicals = self.all_canonical_strings
        for term_def in self.terms.values():
            for fw in term_def.forbidden_writings:
                info = term_def.classify_variant(fw, canonicals)
                if info and info.variant_type not in ("canonical", "substring_canonical"):
                    result[fw] = info
            for av in term_def.allowed_variants:
                info = term_def.classify_variant(av, canonicals)
                if info and info.variant_type not in ("canonical", "substring_canonical"):
                    result[av] = info
            if term_def.report_aliases:
                for alias in term_def.aliases:
                    info = term_def.classify_variant(alias, canonicals)
                    if info and info.variant_type not in ("canonical", "substring_canonical"):
                        result[alias] = info
        return result

    def all_known_forms(self) -> Dict[str, TermDefinition]:
        """获取所有已知形式（含别名等）到 TermDefinition 的映射"""
        result: Dict[str, TermDefinition] = {}
        for term in self.terms.values():
            result[term.canonical] = term
            for alias in term.aliases:
                result[alias] = term
            for av in term.allowed_variants:
                result[av] = term
            for fw in term.forbidden_writings:
                result[fw] = term
        return result

    def get_by_category(self, category: str) -> List[TermDefinition]:
        return [t for t in self.terms.values() if t.category == category]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "categories": self.categories,
            "terms": [t.to_dict() for t in self.terms.values()],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TermList":
        tl = cls()
        tl.categories = list(data.get("categories", tl.categories))
        for td in data.get("terms", []):
            tl.add(TermDefinition.from_dict(td))
        return tl


@dataclass
class CheckConfig:
    """检查配置"""
    extensions: List[str] = field(default_factory=lambda: [".md", ".markdown", ".txt"])
    exclude_patterns: List[str] = field(default_factory=lambda: [
        ".git", "node_modules", "__pycache__", ".venv", "venv",
    ])
    encoding: str = "utf-8"
    chapter_filter_str: Optional[str] = None
    modified_within_days: Optional[int] = None
    output_format: str = "console"
    output_path: Optional[Path] = None
    terms_file: Optional[Path] = None
    ignore_file: Optional[Path] = None
    enable_dedup: bool = True
    enable_ignore: bool = True
    report_alias_terms: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "extensions": self.extensions,
            "exclude_patterns": self.exclude_patterns,
            "encoding": self.encoding,
            "output_format": self.output_format,
            "enable_dedup": self.enable_dedup,
            "enable_ignore": self.enable_ignore,
        }
        if self.chapter_filter_str:
            d["chapter_range"] = self.chapter_filter_str
        if self.modified_within_days:
            d["modified_within_days"] = self.modified_within_days
        if self.output_path:
            d["output_path"] = str(self.output_path)
        if self.terms_file:
            d["terms_file"] = str(self.terms_file)
        if self.ignore_file:
            d["ignore_file"] = str(self.ignore_file)
        if self.report_alias_terms:
            d["report_alias_terms"] = True
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckConfig":
        cfg = cls()
        cfg.extensions = list(data.get("extensions", cfg.extensions))
        cfg.exclude_patterns = list(data.get("exclude_patterns", cfg.exclude_patterns))
        cfg.encoding = str(data.get("encoding", cfg.encoding))
        cfg.output_format = str(data.get("output_format", cfg.output_format))
        cfg.enable_dedup = bool(data.get("enable_dedup", True))
        cfg.enable_ignore = bool(data.get("enable_ignore", True))
        cfg.report_alias_terms = bool(data.get("report_alias_terms", False))
        if "chapter_range" in data and data["chapter_range"]:
            cfg.chapter_filter_str = str(data["chapter_range"])
        if "modified_within_days" in data:
            cfg.modified_within_days = int(data["modified_within_days"])
        if "output_path" in data:
            cfg.output_path = Path(data["output_path"])
        if "terms_file" in data:
            cfg.terms_file = Path(data["terms_file"])
        if "ignore_file" in data:
            cfg.ignore_file = Path(data["ignore_file"])
        return cfg


def load_config(path: Optional[Path] = None) -> CheckConfig:
    """从配置文件加载配置"""
    if path is None:
        for candidate in [Path.cwd() / DEFAULT_CONFIG_FILENAME, Path.home() / DEFAULT_CONFIG_FILENAME]:
            if candidate.exists():
                path = candidate
                break
    if path and path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return CheckConfig.from_dict(data.get("check", {}))
    return CheckConfig()


def save_config(config: CheckConfig, path: Path) -> None:
    """保存配置到文件"""
    data = {"check": config.to_dict()}
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def load_terms(path: Optional[Path] = None) -> TermList:
    """加载术语清单"""
    if path is None:
        for candidate in [Path.cwd() / DEFAULT_TERMS_FILENAME, Path.cwd() / "terms.yml"]:
            if candidate.exists():
                path = candidate
                break
    if path and path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return TermList.from_dict(data)
    return TermList()


def save_terms(terms: TermList, path: Path) -> None:
    """保存术语清单"""
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(terms.to_dict(), f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def load_ignore_rules(path: Optional[Path] = None) -> IgnoreRules:
    """加载忽略规则"""
    if path is None:
        for candidate in [
            Path.cwd() / DEFAULT_IGNORE_FILENAME,
            Path.cwd() / ".doccheck-ignore.yml",
            Path.cwd() / "ignore.yaml",
        ]:
            if candidate.exists():
                path = candidate
                break
    if path and path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return IgnoreRules.from_dict(data)
    return IgnoreRules()


def save_ignore_rules(rules: IgnoreRules, path: Path) -> None:
    """保存忽略规则"""
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(rules.to_dict(), f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def add_ignore_rule(
    rule_type: str,
    pattern: str,
    reason: str = "",
    is_regex: bool = False,
    path: Optional[Path] = None,
) -> IgnoreRule:
    """添加一条忽略规则并保存"""
    rules = load_ignore_rules(path)
    rule = IgnoreRule(
        rule_type=rule_type,
        pattern=pattern,
        reason=reason,
        created_at=datetime.now().isoformat(timespec="seconds"),
        is_regex=is_regex,
    )
    rules.add(rule)
    save_path = path or Path.cwd() / DEFAULT_IGNORE_FILENAME
    save_ignore_rules(rules, save_path)
    return rule


# ==================== 任务状态持久化 ====================

DEFAULT_TASKSTATES_FILENAME = ".doccheck-taskstates.yaml"


def load_task_states(path: Optional[Path] = None) -> TaskStateStore:
    """加载任务状态"""
    if path is None:
        candidate = Path.cwd() / DEFAULT_TASKSTATES_FILENAME
        if candidate.exists():
            path = candidate
    if path and path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return TaskStateStore.from_dict(data)
    return TaskStateStore()


def save_task_states(store: TaskStateStore, path: Path) -> None:
    """保存任务状态"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(store.to_dict(), f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# ==================== 报告快照持久化 ====================

DEFAULT_SNAPSHOT_FILENAME = ".doccheck-last-snapshot.json"


def load_snapshot(path: Optional[Path] = None) -> Optional[ReportSnapshot]:
    """加载上次报告快照"""
    import json as _json
    if path is None:
        candidate = Path.cwd() / DEFAULT_SNAPSHOT_FILENAME
        if candidate.exists():
            path = candidate
    if path and path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return ReportSnapshot.from_dict(data)
        except (OSError, ValueError):
            return None
    return None


def save_snapshot(snapshot: ReportSnapshot, path: Optional[Path] = None) -> Path:
    """保存报告快照，返回保存路径"""
    import json as _json
    if path is None:
        path = Path.cwd() / DEFAULT_SNAPSHOT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(snapshot.to_dict(), f, ensure_ascii=False, indent=2)
    return path


# ==================== 示例配置生成 ====================

def generate_sample_ignore_rules() -> IgnoreRules:
    """
    生成一份可用的示例忽略规则（用于 init-config --with-ignore）。
    包含常见场景：附录文件术语跳过、已知过期链接、草稿章节检查跳过。
    """
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    return IgnoreRules(rules=[
        IgnoreRule(
            rule_type="term",
            pattern="appendix/",
            is_regex=False,
            reason="附录属于参考资料，不检查术语一致性",
            created_at=now,
        ),
        IgnoreRule(
            rule_type="link",
            pattern="external.example.com",
            is_regex=False,
            reason="外部示例域名链接，有效性由外部维护",
            created_at=now,
        ),
        IgnoreRule(
            rule_type="heading",
            pattern="前言|序章|尾声",
            is_regex=True,
            reason="特殊章节标题允许重复",
            created_at=now,
        ),
        IgnoreRule(
            rule_type="chapter",
            pattern="chapter_0[7-9]",
            is_regex=True,
            reason="第7-9章为草稿，跳过章节编号检查",
            created_at=now,
        ),
    ])
