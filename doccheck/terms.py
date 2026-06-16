"""术语管理模块 - 术语清单维护和术语一致性检查"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CheckConfig, TermDefinition, TermList, save_terms
from .document import Document, find_documents, load_document
from .scanner import BaseScanner
from .utils import (
    CheckResult,
    FirstOccurrence,
    Issue,
    IssueType,
    Severity,
    in_chapter_range,
    is_recently_modified,
)


class TermManager:
    """术语管理器"""

    def __init__(self, terms: Optional[TermList] = None, config: Optional[CheckConfig] = None):
        self.terms = terms or TermList()
        self.config = config or CheckConfig()

    def add_term(
        self,
        canonical: str,
        category: str = "general",
        aliases: Optional[List[str]] = None,
        description: str = "",
        severity: str = "warning",
        allowed_variants: Optional[List[str]] = None,
    ) -> TermDefinition:
        """添加术语"""
        term_def = TermDefinition(
            canonical=canonical,
            category=category,
            aliases=aliases or [],
            description=description,
            severity=severity,
            allowed_variants=allowed_variants or [],
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
        """检查术语使用一致性"""
        scanner = BaseScanner(config=self.config, terms=self.terms)
        result = scanner.scan(root)

        file_paths = find_documents(
            root,
            extensions=self.config.extensions,
            exclude_patterns=self.config.exclude_patterns,
        )
        documents: List[Document] = []
        for fp in file_paths:
            if self.config.modified_within_days is not None:
                if not is_recently_modified(fp, self.config.modified_within_days):
                    continue
            try:
                doc = load_document(fp, encoding=self.config.encoding)
            except Exception:
                continue
            if not in_chapter_range(doc.chapter_index, self.config.chapter_range):
                continue
            documents.append(doc)

        variant_map = self.terms.all_variants()
        term_issues: Dict[str, Dict[Path, List[Tuple[int, str]]]] = defaultdict(
            lambda: defaultdict(list)
        )

        import re
        for doc in documents:
            for variant, term_def in variant_map.items():
                if variant == term_def.canonical or variant in term_def.allowed_variants:
                    continue
                pattern = re.compile(re.escape(variant))
                for line_no, line in enumerate(doc.lines, 1):
                    for match in pattern.finditer(line):
                        start = max(0, match.start() - 15)
                        end = min(len(line), match.end() + 15)
                        context = line[start:end].strip()
                        term_issues[variant][doc.file_path].append((line_no, context))

        for variant, file_occurrences in term_issues.items():
            term_def = variant_map[variant]
            total = sum(len(occs) for occs in file_occurrences.values())
            if total == 0:
                continue
            severity_map = {
                "error": Severity.ERROR,
                "warning": Severity.WARNING,
                "info": Severity.INFO,
            }
            issue_severity = severity_map.get(term_def.severity, Severity.WARNING)
            for file_path, occurrences in file_occurrences.items():
                for line_no, ctx in occurrences[:10]:
                    result.add_issue(Issue(
                        type=IssueType.TERM_INCONSISTENT,
                        severity=issue_severity,
                        message=f"术语不一致: \"{variant}\" → 应使用标准写法 \"{term_def.canonical}\" [{term_def.category}]",
                        file_path=file_path,
                        line_number=line_no,
                        context=ctx,
                        suggestion=f"统一替换为 \"{term_def.canonical}\"" + (
                            f"（{term_def.description}）" if term_def.description else ""
                        ),
                        metadata={
                            "variant": variant,
                            "canonical": term_def.canonical,
                            "category": term_def.category,
                            "total_occurrences_in_file": len(occurrences),
                            "total_occurrences": total,
                        },
                    ))

        result.sort_issues()
        return result

    def get_term_stats(self, root: Path) -> Dict[str, Dict]:
        """获取术语统计信息"""
        scanner = BaseScanner(config=self.config, terms=self.terms)
        scanner.load_documents(root)
        result = scanner.scan(root)

        stats: Dict[str, Dict] = {
            "summary": {
                "total_terms": len(self.terms.terms),
                "by_category": {},
                "total_issues": len(result.issues),
            },
            "terms": {},
            "first_occurrences": {},
        }

        for cat in self.terms.categories:
            stats["summary"]["by_category"][cat] = len(self.terms.get_by_category(cat))

        variant_map = self.terms.all_variants()
        canonical_counts: Dict[str, int] = defaultdict(int)
        alias_counts: Dict[str, Dict[str, int]] = defaultdict(dict)

        for term, occurrences in scanner._term_occurrences.items():
            term_def = variant_map.get(term)
            if term_def:
                if term == term_def.canonical:
                    canonical_counts[term_def.canonical] += len(occurrences)
                else:
                    alias_counts[term_def.canonical][term] = len(occurrences)
            else:
                if term not in stats["terms"]:
                    stats["terms"][term] = {
                        "status": "untracked",
                        "occurrences": len(occurrences),
                    }

        for term_def in self.terms.terms.values():
            stats["terms"][term_def.canonical] = {
                "status": "tracked",
                "category": term_def.category,
                "canonical_occurrences": canonical_counts.get(term_def.canonical, 0),
                "aliases": term_def.aliases,
                "alias_occurrences": dict(alias_counts.get(term_def.canonical, {})),
                "total_variant_occurrences": (
                    canonical_counts.get(term_def.canonical, 0)
                    + sum(alias_counts.get(term_def.canonical, {}).values())
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

        for term, count in occurrence_counts.items():
            if count < min_occurrences:
                continue
            existing = self.terms.get(term)
            if existing:
                continue
            is_alias = False
            for t in self.terms.terms.values():
                if term in t.aliases:
                    is_alias = True
                    break
            if is_alias:
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
