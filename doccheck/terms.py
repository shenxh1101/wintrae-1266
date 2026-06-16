"""术语管理模块 - 术语清单维护和术语一致性检查"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CheckConfig, IgnoreRules, TermDefinition, TermList, save_terms
from .document import Document, find_documents, load_document
from .scanner import BaseScanner
from .utils import (
    ChapterFilter,
    CheckResult,
    FirstOccurrence,
    Issue,
    IssueType,
    Severity,
    deduplicate_issues,
    is_recently_modified,
)


class TermManager:
    """术语管理器"""

    def __init__(
        self,
        terms: Optional[TermList] = None,
        config: Optional[CheckConfig] = None,
        ignore_rules: Optional[IgnoreRules] = None,
    ):
        self.terms = terms or TermList()
        self.config = config or CheckConfig()
        self.ignore_rules = ignore_rules

    def add_term(
        self,
        canonical: str,
        category: str = "general",
        aliases: Optional[List[str]] = None,
        description: str = "",
        severity: str = "warning",
        allowed_variants: Optional[List[str]] = None,
        forbidden_writings: Optional[List[str]] = None,
        report_aliases: bool = False,
    ) -> TermDefinition:
        """添加术语"""
        term_def = TermDefinition(
            canonical=canonical,
            category=category,
            aliases=aliases or [],
            description=description,
            severity=severity,
            allowed_variants=allowed_variants or [],
            forbidden_writings=forbidden_writings or [],
            report_aliases=report_aliases,
        )
        self.terms.add(term_def)
        return term_def

    def remove_term(self, canonical: str) -> bool:
        """删除术语"""
        key = canonical.lower()
        if key in self.terms.terms:
            del self.terms.terms[key]
            return True
        return False

    def update_term(
        self,
        canonical: str,
        category: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        description: Optional[str] = None,
        severity: Optional[str] = None,
        allowed_variants: Optional[List[str]] = None,
        forbidden_writings: Optional[List[str]] = None,
        report_aliases: Optional[bool] = None,
    ) -> Optional[TermDefinition]:
        """更新术语"""
        existing = self.terms.get(canonical)
        if not existing:
            return None
        if category is not None:
            existing.category = category
        if aliases is not None:
            existing.aliases = aliases
        if description is not None:
            existing.description = description
        if severity is not None:
            existing.severity = severity
        if allowed_variants is not None:
            existing.allowed_variants = allowed_variants
        if forbidden_writings is not None:
            existing.forbidden_writings = forbidden_writings
        if report_aliases is not None:
            existing.report_aliases = report_aliases
        return existing

    def list_terms(self, category: Optional[str] = None) -> List[TermDefinition]:
        """列出术语"""
        if category:
            return sorted(
                self.terms.get_by_category(category),
                key=lambda t: t.canonical,
            )
        return sorted(self.terms.terms.values(), key=lambda t: (t.category, t.canonical))

    def save(self, path: Optional[Path] = None) -> Path:
        """保存术语清单"""
        save_path = path or self.config.terms_file or Path.cwd() / "terms.yaml"
        save_terms(self.terms, save_path)
        return save_path

    def check_consistency(self, root: Path) -> CheckResult:
        """检查术语使用一致性（复用BaseScanner的完整检查+术语分析）"""
        scanner = BaseScanner(
            config=self.config,
            terms=self.terms,
            ignore_rules=self.ignore_rules,
        )
        result = scanner.scan(root)

        result.sort_issues()
        return result

    def get_term_stats(self, root: Path) -> Dict[str, Dict]:
        """获取术语统计信息，区分不同变体类型"""
        scanner = BaseScanner(
            config=self.config,
            terms=self.terms,
            ignore_rules=self.ignore_rules,
        )
        scanner.load_documents(root)
        result = scanner.scan(root)

        stats: Dict[str, Dict] = {
            "summary": {
                "total_terms": len(self.terms.terms),
                "by_category": {},
                "total_issues": len(result.issues),
                "by_variant_type": {
                    "canonical": 0,
                    "alias": 0,
                    "allowed": 0,
                    "forbidden": 0,
                    "untracked": 0,
                },
            },
            "terms": {},
            "first_occurrences": {},
        }

        for cat in self.terms.categories:
            stats["summary"]["by_category"][cat] = len(self.terms.get_by_category(cat))

        canonical_counts: Dict[str, int] = defaultdict(int)
        alias_counts: Dict[str, Dict[str, int]] = defaultdict(dict)
        allowed_counts: Dict[str, Dict[str, int]] = defaultdict(dict)
        forbidden_counts: Dict[str, Dict[str, int]] = defaultdict(dict)

        for term, occurrences in scanner._term_occurrences.items():
            info = self.terms.classify_any_variant(term)
            if info is None:
                stats["summary"]["by_variant_type"]["untracked"] += len(occurrences)
                if term not in stats["terms"]:
                    stats["terms"][term] = {
                        "status": "untracked",
                        "occurrences": len(occurrences),
                    }
                continue

            canonical = info.canonical
            vtype = info.variant_type
            count = len(occurrences)

            if vtype == "canonical":
                canonical_counts[canonical] += count
                stats["summary"]["by_variant_type"]["canonical"] += count
            elif vtype == "alias":
                alias_counts[canonical][term] = count
                stats["summary"]["by_variant_type"]["alias"] += count
            elif vtype == "allowed":
                allowed_counts[canonical][term] = count
                stats["summary"]["by_variant_type"]["allowed"] += count
            elif vtype == "forbidden":
                forbidden_counts[canonical][term] = count
                stats["summary"]["by_variant_type"]["forbidden"] += count

        for term_def in self.terms.terms.values():
            c = term_def.canonical
            stats["terms"][c] = {
                "status": "tracked",
                "category": term_def.category,
                "description": term_def.description,
                "canonical_occurrences": canonical_counts.get(c, 0),
                "aliases": term_def.aliases,
                "alias_occurrences": dict(alias_counts.get(c, {})),
                "allowed_variants": term_def.allowed_variants,
                "allowed_occurrences": dict(allowed_counts.get(c, {})),
                "forbidden_writings": term_def.forbidden_writings,
                "forbidden_occurrences": dict(forbidden_counts.get(c, {})),
                "report_aliases": term_def.report_aliases,
                "total_variant_occurrences": (
                    canonical_counts.get(c, 0)
                    + sum(alias_counts.get(c, {}).values())
                    + sum(allowed_counts.get(c, {}).values())
                    + sum(forbidden_counts.get(c, {}).values())
                ),
            }

        for term, occ in result.first_occurrences.items():
            stats["first_occurrences"][term] = {
                "file": str(occ.file_path),
                "line": occ.line_number,
                "context": occ.context,
            }

        return stats

    def suggest_terms_from_scan(self, result: CheckResult, min_occurrences: int = 3) -> List[Dict]:
        """从扫描结果中建议添加新术语"""
        suggestions = []
        occurrence_counts = result.term_stats.get("occurrence_counts", {})

        known_keys = set()
        for tdef in self.terms.terms.values():
            known_keys.add(tdef.canonical.lower())
            for a in tdef.aliases:
                known_keys.add(a.lower())
            for v in tdef.allowed_variants:
                known_keys.add(v.lower())
            for f in tdef.forbidden_writings:
                known_keys.add(f.lower())

        for term, count in occurrence_counts.items():
            if count < min_occurrences:
                continue
            if term.lower() in known_keys:
                continue
            info = self.terms.classify_any_variant(term)
            if info is not None:
                continue

            first_occ = result.first_occurrences.get(term)
            suggestions.append({
                "term": term,
                "occurrences": count,
                "first_occurrence": {
                    "file": str(first_occ.file_path) if first_occ else None,
                    "line": first_occ.line_number if first_occ else None,
                    "context": first_occ.context if first_occ else "",
                },
                "suggested_category": self._suggest_category(term),
            })

        suggestions.sort(key=lambda s: -s["occurrences"])
        return suggestions

    @staticmethod
    def _suggest_category(term: str) -> str:
        """根据术语特征建议分类"""
        import re
        if re.search(r"(先生|女士|小姐|老师|教授|博士|医生|公爵|王子|公主|国王|女王)$", term):
            return "character"
        if re.search(r"(市|省|国|镇|村|岛|山|河|湖|海|路|街|广场|公园)$", term):
            return "location"
        if re.search(r"^[A-Z][a-zA-Z\s]+$", term) and len(term.split()) >= 2:
            return "proper_noun"
        if re.search(r"[\u4e00-\u9fff]{2,4}", term) and "·" not in term:
            import random
            return random.choice(["character", "proper_noun"])
        return "proper_noun"
