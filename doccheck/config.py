"""配置管理模块"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


DEFAULT_TERMS_FILENAME = "terms.yaml"
DEFAULT_CONFIG_FILENAME = ".doccheck.yaml"


@dataclass
class TermDefinition:
    """术语定义"""
    canonical: str
    category: str = "general"
    aliases: List[str] = field(default_factory=list)
    description: str = ""
    severity: str = "warning"
    allowed_variants: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = {"canonical": self.canonical}
        if self.category != "general":
            d["category"] = self.category
        if self.aliases:
            d["aliases"] = self.aliases
        if self.description:
            d["description"] = self.description
        if self.severity != "warning":
            d["severity"] = self.severity
        if self.allowed_variants:
            d["allowed_variants"] = self.allowed_variants
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TermDefinition":
        return cls(
            canonical=str(data["canonical"]),
            category=str(data.get("category", "general")),
            aliases=list(data.get("aliases", [])),
            description=str(data.get("description", "")),
            severity=str(data.get("severity", "warning")),
            allowed_variants=list(data.get("allowed_variants", [])),
        )


@dataclass
class TermList:
    """术语清单"""
    terms: Dict[str, TermDefinition] = field(default_factory=dict)
    categories: List[str] = field(default_factory=lambda: ["character", "location", "proper_noun", "general"])

    def add(self, term: TermDefinition) -> None:
        key = term.canonical.lower()
        self.terms[key] = term
        if term.category not in self.categories:
            self.categories.append(term.category)

    def get(self, name: str) -> Optional[TermDefinition]:
        return self.terms.get(name.lower())

    def all_variants(self) -> Dict[str, TermDefinition]:
        """获取所有变体（含别名）到标准形式的映射"""
        result: Dict[str, TermDefinition] = {}
        for term in self.terms.values():
            result[term.canonical] = term
            for alias in term.aliases:
                result[alias] = term
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
    chapter_range: Optional[List[int]] = None
    modified_within_days: Optional[int] = None
    output_format: str = "console"
    output_path: Optional[Path] = None
    terms_file: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "extensions": self.extensions,
            "exclude_patterns": self.exclude_patterns,
            "encoding": self.encoding,
            "output_format": self.output_format,
        }
        if self.chapter_range:
            d["chapter_range"] = self.chapter_range
        if self.modified_within_days:
            d["modified_within_days"] = self.modified_within_days
        if self.output_path:
            d["output_path"] = str(self.output_path)
        if self.terms_file:
            d["terms_file"] = str(self.terms_file)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckConfig":
        cfg = cls()
        cfg.extensions = list(data.get("extensions", cfg.extensions))
        cfg.exclude_patterns = list(data.get("exclude_patterns", cfg.exclude_patterns))
        cfg.encoding = str(data.get("encoding", cfg.encoding))
        cfg.output_format = str(data.get("output_format", cfg.output_format))
        if "chapter_range" in data and data["chapter_range"]:
            cfg.chapter_range = list(data["chapter_range"])
        if "modified_within_days" in data:
            cfg.modified_within_days = int(data["modified_within_days"])
        if "output_path" in data:
            cfg.output_path = Path(data["output_path"])
        if "terms_file" in data:
            cfg.terms_file = Path(data["terms_file"])
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
