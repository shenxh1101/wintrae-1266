"""基础扫描器 - 章节编号、重复标题、专有名词收集"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CheckConfig, TermList, IgnoreRules, load_ignore_rules
from .document import Document, Heading, find_documents, load_document
from .utils import (
    ChapterFilter,
    CheckResult,
    FirstOccurrence,
    Issue,
    IssueType,
    Severity,
    SuspectedSynonym,
    deduplicate_issues,
    extract_cjk_names,
    find_similar_strings,
    is_recently_modified,
    string_similarity,
)


class BaseScanner:
    """基础扫描器 - 负责加载文档并执行基础检查"""

    def __init__(
        self,
        config: Optional[CheckConfig] = None,
        terms: Optional[TermList] = None,
        ignore_rules: Optional[IgnoreRules] = None,
    ):
        self.config = config or CheckConfig()
        self.terms = terms or TermList()
        self.ignore_rules = ignore_rules
        self._chapter_filter = ChapterFilter.parse(self.config.chapter_filter_str)
        self._heading_index: Dict[Tuple[str, int], List[Tuple[Path, int]]] = defaultdict(list)
        self._chapter_numbers: Dict[int, List[Path]] = defaultdict(list)
        self._all_documents: List[Document] = []
        self._term_occurrences: Dict[str, List[Tuple[Path, int, str]]] = defaultdict(list)
        self._all_issues_before_filter: List[Issue] = []

    @property
    def chapter_filter(self) -> ChapterFilter:
        return self._chapter_filter

    def load_documents(self, root: Path) -> List[Document]:
        """加载目录下的所有文档"""
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
            except Exception as e:
                print(f"警告: 无法加载文件 {fp}: {e}")
                continue
            if not self._chapter_filter.matches(doc.chapter_index):
                continue
            documents.append(doc)
        self._all_documents = documents
        return documents

    def scan(self, root: Path) -> CheckResult:
        """执行完整扫描"""
        result = CheckResult()
        result.files_scanned = []

        documents = self.load_documents(root)
        if not documents:
            result.end_time = time.time()
            return result

        for doc in documents:
            result.files_scanned.append(doc.file_path)
            self._scan_document(doc, result)

        self._check_chapter_continuity(result)
        self._check_duplicate_headings(result)
        self._analyze_terms(result)

        raw_issues = list(self._all_issues_before_filter) + list(result.issues)

        if self.config.enable_dedup:
            deduped, dedup_stats = deduplicate_issues(raw_issues)
            result.dedup_stats = dedup_stats
            raw_issues = deduped

        if self.config.enable_ignore and self.ignore_rules is not None:
            kept, ignore_stats = self.ignore_rules.filter_issues(raw_issues)
            result.ignore_stats = ignore_stats
            raw_issues = kept

        result.issues = raw_issues
        result.end_time = time.time()
        result.sort_issues()
        return result

    def _scan_document(self, doc: Document, result: CheckResult) -> None:
        """扫描单个文档"""
        doc_chapter_nums = set()

        for heading in doc.headings:
            key = (heading.text.strip().lower(), heading.level)
            self._heading_index[key].append((doc.file_path, heading.line_number))

            if heading.level <= 2 and heading.number_value and len(heading.number_value) == 1:
                chap_num = heading.number_value[0]
                doc_chapter_nums.add(chap_num)

        if doc.chapter_index is not None:
            doc_chapter_nums.add(doc.chapter_index)

        for chap_num in doc_chapter_nums:
            if doc.file_path not in self._chapter_numbers[chap_num]:
                self._chapter_numbers[chap_num].append(doc.file_path)

        self._collect_terms(doc)

    def _check_chapter_continuity(self, result: CheckResult) -> None:
        """检查章节编号连续性"""
        if not self._chapter_numbers:
            return

        all_nums = sorted(self._chapter_numbers.keys())
        if len(all_nums) < 2:
            return

        for num, files in self._chapter_numbers.items():
            if len(files) > 1:
                file_list = ", ".join(str(f) for f in files)
                self._all_issues_before_filter.append(Issue(
                    type=IssueType.CHAPTER_NUMBER_DUPLICATE,
                    severity=Severity.ERROR,
                    message=f"章节编号重复: 第{num}章出现在多个文件中",
                    file_path=files[0],
                    context=f"涉及文件: {file_list}",
                    suggestion=f"请确认第{num}章的唯一归属，删除重复的章节编号",
                    metadata={"chapter": num, "files": [str(f) for f in files]},
                ))

        min_num, max_num = all_nums[0], all_nums[-1]
        expected = set(range(min_num, max_num + 1))
        actual = set(all_nums)
        missing = sorted(expected - actual)

        for miss in missing:
            prev_num = max([n for n in all_nums if n < miss], default=None)
            next_num = min([n for n in all_nums if n > miss], default=None)
            ctx_parts = []
            if prev_num:
                prev_files = ", ".join(str(f) for f in self._chapter_numbers[prev_num])
                ctx_parts.append(f"前一章({prev_num}): {prev_files}")
            if next_num:
                next_files = ", ".join(str(f) for f in self._chapter_numbers[next_num])
                ctx_parts.append(f"后一章({next_num}): {next_files}")
            self._all_issues_before_filter.append(Issue(
                type=IssueType.CHAPTER_NUMBER_GAP,
                severity=Severity.WARNING,
                message=f"章节编号缺失: 缺少第{miss}章",
                file_path=self._chapter_numbers.get(prev_num, [None])[0] if prev_num else None,
                context=" | ".join(ctx_parts),
                suggestion=f"检查是否遗漏了第{miss}章，或者调整前后章节编号",
                metadata={"missing_chapter": miss, "prev": prev_num, "next": next_num},
            ))

    def _check_duplicate_headings(self, result: CheckResult) -> None:
        """检查重复标题"""
        for (text, level), locations in self._heading_index.items():
            if len(locations) <= 1:
                continue
            if level >= 4 and len(locations) <= 2:
                continue
            display_text = text if len(text) <= 30 else text[:27] + "..."
            loc_list = "; ".join(f"{f}:{ln}" for f, ln in locations)
            first_file, first_line = locations[0]
            self._all_issues_before_filter.append(Issue(
                type=IssueType.DUPLICATE_HEADING,
                severity=Severity.WARNING if level <= 3 else Severity.INFO,
                message=f"重复标题 (H{level}): \"{display_text}\" 出现了{len(locations)}次",
                file_path=first_file,
                line_number=first_line,
                context=loc_list,
                suggestion="考虑给重复标题添加限定词或重新组织结构以避免歧义",
                metadata={
                    "heading": text,
                    "level": level,
                    "count": len(locations),
                    "all_locations": [(str(f), ln) for f, ln in locations],
                },
            ))

    def _collect_terms(self, doc: Document) -> None:
        """从文档中收集专有名词"""
        text = doc.content
        cjk_names = extract_cjk_names(text)

        known_forms = self.terms.all_known_forms()
        for form in known_forms.keys():
            self._find_term_occurrences(doc, form)

        for name in cjk_names:
            if name.lower() not in {k.lower() for k in known_forms.keys()}:
                self._find_term_occurrences(doc, name)

        uppercase_pattern = re.compile(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,3}\b")
        for match in uppercase_pattern.finditer(text):
            name = match.group(0).strip()
            if 2 <= len(name.split()) <= 4:
                self._find_term_occurrences(doc, name)

    def _find_term_occurrences(self, doc: Document, term: str) -> None:
        """查找术语在文档中的所有出现位置"""
        if not term or len(term) < 2:
            return
        pattern = re.compile(re.escape(term))
        for line_no, line in enumerate(doc.lines, 1):
            for match in pattern.finditer(line):
                start = max(0, match.start() - 10)
                end = min(len(line), match.end() + 10)
                context = line[start:end].strip()
                if len(context) < len(term) + 5:
                    context = line.strip()[:50]
                self._term_occurrences[term].append((doc.file_path, line_no, context))

    def _analyze_terms(self, result: CheckResult) -> None:
        """分析收集到的术语"""
        occurrence_counts: Dict[str, int] = {}

        for term, occurrences in self._term_occurrences.items():
            if not occurrences:
                continue
            occurrence_counts[term] = len(occurrences)
            first_file, first_line, first_ctx = occurrences[0]
            result.first_occurrences[term] = FirstOccurrence(
                term=term,
                file_path=first_file,
                line_number=first_line,
                context=first_ctx,
            )

        checkable = self.terms.all_checkable_variants()

        for variant, variant_info in checkable.items():
            occurrences = self._term_occurrences.get(variant, [])
            if not occurrences:
                continue

            severity_map = {
                "error": Severity.ERROR,
                "warning": Severity.WARNING,
                "info": Severity.INFO,
                "suggestion": Severity.SUGGESTION,
            }
            sev = severity_map.get(variant_info.severity, Severity.WARNING)

            if variant_info.variant_type == "forbidden":
                issue_type = IssueType.TERM_FORBIDDEN
                label = "禁用写法"
            elif variant_info.variant_type == "allowed":
                issue_type = IssueType.TERM_ALIAS_FOUND
                label = "允许变体"
            elif variant_info.variant_type == "alias":
                issue_type = IssueType.TERM_ALIAS_FOUND
                label = "别名"
            else:
                issue_type = IssueType.TERM_ALIAS_FOUND
                label = "非标准写法"

            needs_flag = "必须修改" if variant_info.needs_edit else "建议统一"

            total = len(occurrences)
            msg = f"术语{label}: \"{variant}\" {needs_flag} → 标准写法为 \"{variant_info.canonical}\""
            if total > 1:
                msg += f"（共{total}处）"

            first_file, first_line, first_ctx = occurrences[0]

            all_locs = "; ".join(
                f"{Path(fp).name}:{ln}" for fp, ln, _ in occurrences[:5]
            )

            suggestion = f"将 \"{variant}\" 替换为标准写法 \"{variant_info.canonical}\""
            if variant_info.description:
                suggestion += f"（{variant_info.description}）"

            self._all_issues_before_filter.append(Issue(
                type=issue_type,
                severity=sev,
                message=msg,
                file_path=first_file,
                line_number=first_line,
                context=all_locs if total > 1 else first_ctx,
                suggestion=suggestion,
                metadata={
                    "variant": variant,
                    "canonical": variant_info.canonical,
                    "category": variant_info.category,
                    "variant_type": variant_info.variant_type,
                    "needs_edit": variant_info.needs_edit,
                    "total_occurrences": total,
                    "all_locations": [(str(fp), ln) for fp, ln, _ in occurrences],
                },
            ))

        all_terms = [t for t, c in occurrence_counts.items() if c >= 2]
        similar_pairs = find_similar_strings(all_terms, threshold=0.8, min_length=2)

        synonym_groups: Dict[str, List[str]] = {}
        processed = set()
        known_keys = set()
        for variant in checkable.keys():
            known_keys.add(variant.lower())
        for tdef in self.terms.terms.values():
            known_keys.add(tdef.canonical.lower())

        for a, b, sim in similar_pairs:
            if a in processed or b in processed:
                continue
            if a.lower() in known_keys or b.lower() in known_keys:
                info_a = self.terms.classify_any_variant(a)
                info_b = self.terms.classify_any_variant(b)
                if info_a and info_b and info_a.canonical.lower() == info_b.canonical.lower():
                    continue
                if info_a and info_a.variant_type in ("canonical", "substring_canonical"):
                    continue
                if info_b and info_b.variant_type in ("canonical", "substring_canonical"):
                    continue
            key = min(a.lower(), b.lower())
            if key not in synonym_groups:
                synonym_groups[key] = [a, b]
                processed.add(a)
                processed.add(b)

        for key, terms_list in synonym_groups.items():
            sim_scores = []
            for i, a in enumerate(terms_list):
                for b in terms_list[i + 1:]:
                    sim_scores.append(string_similarity(a, b))
            avg_sim = sum(sim_scores) / len(sim_scores) if sim_scores else 0.0

            occurrences_count = {t: occurrence_counts.get(t, 0) for t in terms_list}
            first_occs = {}
            for t in terms_list:
                if t in result.first_occurrences:
                    first_occs[t] = result.first_occurrences[t]
                elif self._term_occurrences.get(t):
                    f, l, c = self._term_occurrences[t][0]
                    first_occs[t] = FirstOccurrence(term=t, file_path=f, line_number=l, context=c)

            result.suspected_synonyms.append(SuspectedSynonym(
                terms=sorted(terms_list),
                similarity=round(avg_sim, 3),
                first_occurrences=first_occs,
                occurrences_count=occurrences_count,
            ))

            canonical_candidate = max(terms_list, key=lambda t: occurrence_counts.get(t, 0))
            for t in terms_list:
                if t == canonical_candidate:
                    continue
                occs = self._term_occurrences.get(t, [])
                if occs:
                    file_path, line_no, ctx = occs[0]
                    self._all_issues_before_filter.append(Issue(
                        type=IssueType.SUSPECTED_SYNONYM,
                        severity=Severity.INFO,
                        message=f"疑似同义词: \"{t}\" 与 \"{canonical_candidate}\" 可能是同一概念的不同写法",
                        file_path=file_path,
                        line_number=line_no,
                        context=ctx,
                        suggestion=f"确认是否为同一概念，如是，建议统一使用 \"{canonical_candidate}\"（出现频率更高）",
                        metadata={
                            "term": t,
                            "candidate": canonical_candidate,
                            "similarity": round(avg_sim, 3),
                        },
                    ))

        result.term_stats = {
            "total_terms_found": len(occurrence_counts),
            "terms_with_multiple_occurrences": sum(1 for c in occurrence_counts.values() if c >= 2),
            "occurrence_counts": dict(sorted(occurrence_counts.items(), key=lambda x: -x[1])),
        }
