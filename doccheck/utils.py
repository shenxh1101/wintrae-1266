"""通用工具函数"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple


class Severity(str, Enum):
    """问题严重级别"""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    SUGGESTION = "suggestion"


class IssueType(str, Enum):
    """问题类型"""
    CHAPTER_NUMBER_GAP = "chapter_number_gap"
    CHAPTER_NUMBER_DUPLICATE = "chapter_number_duplicate"
    DUPLICATE_HEADING = "duplicate_heading"
    MISSING_IMAGE_ALT = "missing_image_alt"
    BROKEN_LINK = "broken_link"
    TERM_INCONSISTENT = "term_inconsistent"
    TERM_ALIAS_FOUND = "term_alias_found"
    TERM_FORBIDDEN = "term_forbidden"
    SUSPECTED_SYNONYM = "suspected_synonym"
    UNKNOWN_TERM = "unknown_term"


@dataclass
class Issue:
    """检查发现的问题"""
    type: IssueType
    severity: Severity
    message: str
    file_path: Optional[Path] = None
    line_number: Optional[int] = None
    context: str = ""
    suggestion: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    _stable_id: Optional[str] = None
    _dedup_key: Optional[str] = None

    def __post_init__(self):
        if self._stable_id is None:
            self._stable_id = self._compute_stable_id()
        if self._dedup_key is None:
            self._dedup_key = self._compute_dedup_key()

    @property
    def stable_id(self) -> str:
        """稳定唯一ID，同一问题在多次扫描中保持不变"""
        if self._stable_id is None:
            self._stable_id = self._compute_stable_id()
        return self._stable_id

    @property
    def dedup_key(self) -> str:
        """用于去重的键，同一类相同问题会合并"""
        if self._dedup_key is None:
            self._dedup_key = self._compute_dedup_key()
        return self._dedup_key

    def _compute_stable_id(self) -> str:
        """基于问题关键属性生成稳定哈希ID"""
        components = [
            self.type.value,
            str(self.file_path.name if self.file_path else ""),
            str(self.line_number or ""),
            self._normalize_for_hash(self.message),
            self._normalize_for_hash(str(self.metadata.get("canonical") or "")),
            self._normalize_for_hash(str(self.metadata.get("variant") or "")),
            self._normalize_for_hash(str(self.metadata.get("heading") or "")),
            self._normalize_for_hash(str(self.metadata.get("missing_chapter") or "")),
            self._normalize_for_hash(str(self.metadata.get("target") or "")),
            self._normalize_for_hash(str(self.metadata.get("term") or "")),
        ]
        raw = "|".join(c for c in components if c)
        return "ISS-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12].upper()

    def _compute_dedup_key(self) -> str:
        """生成去重键 - 更粗粒度的合并键"""
        meta = self.metadata
        type_val = self.type.value
        fp_name = self.file_path.name if self.file_path else ""

        if self.type == IssueType.CHAPTER_NUMBER_GAP:
            chapter = meta.get("missing_chapter")
            return f"{type_val}::chapter_gap::{chapter}"
        elif self.type == IssueType.CHAPTER_NUMBER_DUPLICATE:
            chapter = meta.get("chapter")
            return f"{type_val}::chapter_dup::{chapter}"
        elif self.type == IssueType.DUPLICATE_HEADING:
            heading = meta.get("heading", "").lower()
            level = meta.get("level", 0)
            return f"{type_val}::{level}::{self._normalize_for_hash(heading)}"
        elif self.type in (IssueType.TERM_INCONSISTENT, IssueType.TERM_ALIAS_FOUND, IssueType.TERM_FORBIDDEN):
            canonical = meta.get("canonical", meta.get("term", ""))
            variant = meta.get("variant", "")
            return f"{type_val}::term::{canonical.lower()}::{variant.lower()}"
        elif self.type == IssueType.BROKEN_LINK:
            target = meta.get("target", "")
            ref_type = meta.get("ref_type", "")
            return f"{type_val}::{ref_type}::{fp_name}::{target.lower()}"
        elif self.type == IssueType.MISSING_IMAGE_ALT:
            target = meta.get("image_src", "")
            return f"{type_val}::{fp_name}::{line_no_hash(self.line_number)}::{target.lower()}"
        elif self.type == IssueType.SUSPECTED_SYNONYM:
            t1 = meta.get("term", "").lower()
            t2 = meta.get("candidate", "").lower()
            key_parts = sorted([t1, t2])
            return f"{type_val}::syn::{key_parts[0]}::{key_parts[1]}"
        else:
            return f"{type_val}::{fp_name}::{line_no_hash(self.line_number)}::{self._normalize_for_hash(self.message)}"

    @staticmethod
    def _normalize_for_hash(text: str) -> str:
        """规范化文本用于哈希比较"""
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text.strip().lower())
        return re.sub(r"[^\w\u4e00-\u9fff ]", "", text)

    @property
    def location(self) -> str:
        if self.file_path and self.line_number:
            return f"{self.file_path}:{self.line_number}"
        elif self.file_path:
            return str(self.file_path)
        return ""

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.stable_id,
            "type": self.type.value,
            "severity": self.severity.value,
            "message": self.message,
            "file_path": str(self.file_path) if self.file_path else None,
            "line_number": self.line_number,
            "context": self.context,
            "suggestion": self.suggestion,
            "metadata": self.metadata,
            "_dedup_key": self.dedup_key,
        }
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Issue":
        return cls(
            type=IssueType(data["type"]),
            severity=Severity(data["severity"]),
            message=data["message"],
            file_path=Path(data["file_path"]) if data.get("file_path") else None,
            line_number=data.get("line_number"),
            context=data.get("context", ""),
            suggestion=data.get("suggestion", ""),
            metadata=dict(data.get("metadata", {})),
            _stable_id=data.get("id"),
            _dedup_key=data.get("_dedup_key"),
        )


def line_no_hash(n: Optional[int]) -> str:
    """行号哈希，用于去重键"""
    if n is None:
        return "*"
    return str(((n - 1) // 5) * 5 + 3)


# ==================== 忽略规则 ====================

@dataclass
class IgnoreRule:
    """单条忽略规则"""
    rule_type: str
    pattern: str
    reason: str = ""
    created_at: str = ""
    is_regex: bool = False

    def matches(self, issue: Issue) -> bool:
        """检查问题是否匹配此忽略规则"""
        rule_t = self.rule_type.lower()
        itype = issue.type.value

        if rule_t != "all" and rule_t != itype:
            if rule_t in _RULE_TYPE_ALIASES:
                if itype not in _RULE_TYPE_ALIASES[rule_t]:
                    return False
            else:
                return False

        try:
            if self.is_regex:
                regex = re.compile(self.pattern, re.IGNORECASE)
                return self._search_fields(issue, regex.search)
            else:
                pat_lower = self.pattern.lower()
                return self._search_texts(issue, pat_lower)
        except re.error:
            return False

    def _search_fields(self, issue: Issue, matcher: Callable[[str], Optional[Any]]) -> bool:
        """用正则匹配各字段"""
        fields = [issue.message, issue.suggestion, issue.context]
        for k, v in issue.metadata.items():
            fields.append(str(v))
        if issue.file_path:
            fields.append(str(issue.file_path))
            fields.append(issue.file_path.name)
        for f in fields:
            if f and matcher(str(f)):
                return True
        return False

    def _search_texts(self, issue: Issue, pat: str) -> bool:
        """用子串匹配各字段"""
        fields = [
            issue.message.lower(),
            issue.suggestion.lower(),
            issue.context.lower(),
        ]
        for k, v in issue.metadata.items():
            fields.append(str(v).lower())
        if issue.file_path:
            fields.append(str(issue.file_path).lower())
            fields.append(issue.file_path.name.lower())
        for f in fields:
            if f and pat in f:
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_type": self.rule_type,
            "pattern": self.pattern,
            "reason": self.reason,
            "created_at": self.created_at,
            "is_regex": self.is_regex,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IgnoreRule":
        return cls(
            rule_type=str(data["rule_type"]),
            pattern=str(data["pattern"]),
            reason=str(data.get("reason", "")),
            created_at=str(data.get("created_at", "")),
            is_regex=bool(data.get("is_regex", False)),
        )


_RULE_TYPE_ALIASES: Dict[str, Set[str]] = {
    "chapter": {"chapter_number_gap", "chapter_number_duplicate"},
    "heading": {"duplicate_heading"},
    "link": {"broken_link"},
    "image": {"missing_image_alt"},
    "term": {"term_inconsistent", "term_alias_found", "term_forbidden", "unknown_term"},
    "synonym": {"suspected_synonym"},
}


@dataclass
class IgnoreRules:
    """忽略规则集合"""
    rules: List[IgnoreRule] = field(default_factory=list)

    def add(self, rule: IgnoreRule) -> None:
        self.rules.append(rule)

    def is_ignored(self, issue: Issue) -> Tuple[bool, Optional[IgnoreRule]]:
        """检查问题是否应被忽略，返回(是否忽略, 匹配的规则)"""
        for rule in self.rules:
            if rule.matches(issue):
                return True, rule
        return False, None

    def filter_issues(self, issues: List[Issue]) -> Tuple[List[Issue], Dict[str, int]]:
        """过滤问题列表，返回(保留问题, 忽略统计)"""
        kept: List[Issue] = []
        stats: Dict[str, int] = {"total": 0, "by_type": {}, "by_rule": {}}
        for issue in issues:
            ignored, rule = self.is_ignored(issue)
            if ignored:
                stats["total"] += 1
                stats["by_type"][issue.type.value] = stats["by_type"].get(issue.type.value, 0) + 1
                rule_key = rule.pattern if rule else "unknown"
                stats["by_rule"][rule_key] = stats["by_rule"].get(rule_key, 0) + 1
            else:
                kept.append(issue)
        return kept, stats

    def to_dict(self) -> Dict[str, Any]:
        return {"rules": [r.to_dict() for r in self.rules]}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IgnoreRules":
        rules = [IgnoreRule.from_dict(rd) for rd in data.get("rules", [])]
        return cls(rules=rules)


# ==================== 问题去重 ====================

def deduplicate_issues(issues: List[Issue]) -> Tuple[List[Issue], Dict[str, Any]]:
    """
    对问题列表去重。
    合并同一 dedup_key 的问题，取最具代表性的一个，并在 metadata 中记录合并信息。
    返回 (去重后的问题列表, 去重统计)
    """
    groups: Dict[str, List[Issue]] = defaultdict(list)
    for issue in issues:
        groups[issue.dedup_key].append(issue)

    deduped: List[Issue] = []
    stats = {"original_count": len(issues), "deduped_count": 0, "merged_groups": 0}

    for key, group in groups.items():
        stats["deduped_count"] += 1
        if len(group) == 1:
            deduped.append(group[0])
            continue
        stats["merged_groups"] += 1

        representative = _select_representative(group)
        merged_files = set()
        merged_lines = []
        total_occurrences = len(group)
        for g in group:
            if g.file_path:
                merged_files.add(str(g.file_path))
            if g.line_number:
                merged_lines.append(f"{g.file_path.name if g.file_path else '?'}:{g.line_number}")

        representative.metadata = dict(representative.metadata)
        representative.metadata["_merged"] = {
            "count": total_occurrences,
            "locations": sorted(list(set(merged_lines)))[:10],
            "files": sorted(list(merged_files)),
        }

        if total_occurrences > 1:
            original_msg = representative.message
            if "共" not in original_msg and total_occurrences > 1:
                representative.message = f"{original_msg}（共{total_occurrences}处）"

        deduped.append(representative)

    stats["removed"] = stats["original_count"] - stats["deduped_count"]
    deduped.sort(key=lambda i: (
        {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2, Severity.SUGGESTION: 3}.get(i.severity, 99),
        str(i.file_path or ""),
        i.line_number or 0,
    ))
    return deduped, stats


def _select_representative(group: List[Issue]) -> Issue:
    """从问题组中选择最具代表性的"""
    severity_order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2, Severity.SUGGESTION: 3}
    group_sorted = sorted(
        group,
        key=lambda i: (
            severity_order.get(i.severity, 99),
            0 if i.file_path else 1,
            i.line_number or 999999,
            -len(i.context or ""),
        ),
    )
    return group_sorted[0]


# ==================== 章节范围 ====================

@dataclass
class ChapterFilter:
    """章节筛选器，语义明确"""
    mode: str = "all"
    single: Optional[int] = None
    start: Optional[int] = None
    end: Optional[int] = None

    @classmethod
    def parse(cls, range_str: Optional[str]) -> "ChapterFilter":
        """
        解析章节范围字符串：
        - None/"" -> 全部
        - "5" -> 仅第5章（不是>=5！）
        - "5-10" -> 第5到第10章（含）
        """
        if not range_str or not range_str.strip():
            return cls(mode="all")
        s = range_str.strip()
        if "-" in s:
            parts = s.split("-", 1)
            try:
                start = int(parts[0].strip())
                end = int(parts[1].strip())
                return cls(mode="range", start=start, end=end)
            except ValueError:
                return cls(mode="all")
        try:
            single = int(s)
            return cls(mode="single", single=single)
        except ValueError:
            return cls(mode="all")

    def matches(self, chapter_idx: Optional[int]) -> bool:
        """检查章节是否符合筛选"""
        if self.mode == "all":
            return True
        if chapter_idx is None:
            return False
        if self.mode == "single":
            return chapter_idx == self.single
        if self.mode == "range":
            return self.start <= chapter_idx <= self.end
        return False

    def to_list(self) -> Optional[List[int]]:
        """转换为旧格式列表（向后兼容）"""
        if self.mode == "single":
            return [self.single]
        if self.mode == "range":
            return [self.start, self.end]
        return None

    def describe(self) -> str:
        """人类可读的描述（用于CLI输出）"""
        if self.mode == "all":
            return "全部章节"
        if self.mode == "single":
            return f"仅第 {self.single} 章"
        if self.mode == "range":
            return f"第 {self.start}-{self.end} 章"
        return "未指定"


# ==================== 术语变体分类 ====================

@dataclass
class TermVariantInfo:
    """术语变体信息，明确区分各类变体"""
    canonical: str
    variant: str
    variant_type: str
    category: str = "general"
    description: str = ""
    severity: str = "warning"
    needs_edit: bool = True

    @property
    def type_label(self) -> str:
        return {
            "alias": "别名（允许使用）",
            "allowed": "允许变体（推荐统一）",
            "forbidden": "禁用写法（必须修改）",
            "substring_canonical": "标准写法内含（不提示）",
        }.get(self.variant_type, self.variant_type)


def is_substring_of_canonical(variant: str, canonical: str, all_canonicals: Optional[List[str]] = None) -> bool:
    """
    判断变体是否只是标准写法的子串（包含在标准写法内部的短词）。
    例如：canonical="李明轩"，variant="李明" 或 "明轩" -> True，不应单独报错。
    """
    v = variant.strip()
    c = canonical.strip()
    if not v or not c:
        return False
    if len(v) >= len(c):
        return False
    if v.lower() in c.lower():
        return True
    if all_canonicals:
        for other in all_canonicals:
            if other != canonical and len(v) < len(other) and v.lower() in other.lower():
                return True
    return False


# ==================== 原有工具函数 ====================

@dataclass
class FirstOccurrence:
    """术语首次出现位置"""
    term: str
    file_path: Path
    line_number: int
    context: str


@dataclass
class SuspectedSynonym:
    """疑似同义词组"""
    terms: List[str]
    similarity: float
    first_occurrences: Dict[str, FirstOccurrence] = field(default_factory=dict)
    occurrences_count: Dict[str, int] = field(default_factory=dict)


@dataclass
class CheckResult:
    """一次完整检查的结果"""
    issues: List[Issue] = field(default_factory=list)
    first_occurrences: Dict[str, FirstOccurrence] = field(default_factory=dict)
    suspected_synonyms: List[SuspectedSynonym] = field(default_factory=list)
    term_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)
    files_scanned: List[Path] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    ignore_stats: Dict[str, Any] = field(default_factory=dict)
    dedup_stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        if self.end_time:
            return self.end_time - self.start_time
        return time.time() - self.start_time

    @property
    def issue_count(self) -> Dict[str, int]:
        counts = {"error": 0, "warning": 0, "info": 0, "suggestion": 0}
        for issue in self.issues:
            counts[issue.severity.value] += 1
        return counts

    def add_issue(self, issue: Issue) -> None:
        self.issues.append(issue)

    def sort_issues(self) -> None:
        severity_order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2, Severity.SUGGESTION: 3}
        self.issues.sort(key=lambda i: (
            severity_order.get(i.severity, 99),
            str(i.file_path or ""),
            i.line_number or 0,
        ))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issues": [i.to_dict() for i in self.issues],
            "first_occurrences": {
                k: {
                    "file_path": str(v.file_path),
                    "line_number": v.line_number,
                    "context": v.context,
                }
                for k, v in self.first_occurrences.items()
            },
            "suspected_synonyms": [
                {
                    "terms": s.terms,
                    "similarity": s.similarity,
                    "occurrences_count": s.occurrences_count,
                }
                for s in self.suspected_synonyms
            ],
            "term_stats": self.term_stats,
            "files_scanned": [str(f) for f in self.files_scanned],
            "duration": self.duration,
            "issue_count": self.issue_count,
            "ignore_stats": self.ignore_stats,
            "dedup_stats": self.dedup_stats,
        }


def is_recently_modified(file_path: Path, days: int) -> bool:
    """检查文件是否在指定天数内被修改"""
    if days <= 0:
        return True
    try:
        mtime = os.path.getmtime(file_path)
        cutoff = time.time() - (days * 86400)
        return mtime >= cutoff
    except OSError:
        return False


def in_chapter_range(chapter_idx: Optional[int], chapter_range: Optional[List[int]]) -> bool:
    """
    旧API（向后兼容）。推荐使用 ChapterFilter.matches()。
    注意：chapter_range=[5] 现在表示仅第5章（与旧行为不同！）
    """
    if chapter_range is None or len(chapter_range) == 0:
        return True
    if chapter_idx is None:
        return False
    if len(chapter_range) == 1:
        return chapter_idx == chapter_range[0]
    return chapter_range[0] <= chapter_idx <= chapter_range[1]


def string_similarity(a: str, b: str) -> float:
    """计算两个字符串的相似度 (0-1)"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_lower = a.lower()
    b_lower = b.lower()
    if a_lower == b_lower:
        return 0.95
    return difflib.SequenceMatcher(None, a_lower, b_lower).ratio()


def find_similar_strings(
    strings: Iterable[str],
    threshold: float = 0.7,
    min_length: int = 2,
) -> List[Tuple[str, str, float]]:
    """找出相似字符串对"""
    str_list = [s for s in strings if len(s) >= min_length]
    results = []
    for i, a in enumerate(str_list):
        for b in str_list[i + 1:]:
            sim = string_similarity(a, b)
            if sim >= threshold:
                results.append((a, b, sim))
    return results


_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_NAME_PATTERNS = [
    re.compile(r"[\u4e00-\u9fff]{2,4}(?:先生|女士|小姐|老师|教授|博士|医生)"),
    re.compile(r"[·\u4e00-\u9fff]{2,8}(?:·[\u4e00-\u9fff]{2,8})+"),
]


def extract_cjk_names(text: str) -> List[str]:
    """从文本中提取可能的中文人名/地名"""
    names = set()
    for pattern in _NAME_PATTERNS:
        for match in pattern.finditer(text):
            names.add(match.group(0))
    return sorted(names)


def count_occurrences(text: str, term: str, case_sensitive: bool = False) -> int:
    """统计术语在文本中出现的次数"""
    if not term:
        return 0
    if not case_sensitive:
        return len(re.findall(re.escape(term), text, re.IGNORECASE))
    return len(re.findall(re.escape(term), text))


def format_time(seconds: float) -> str:
    """格式化时间显示"""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m{secs:.1f}s"


def relative_path(path: Path, base: Optional[Path] = None) -> Path:
    """获取相对路径"""
    try:
        if base is None:
            base = Path.cwd()
        return path.relative_to(base)
    except ValueError:
        return path


def ensure_parent_dir(path: Path) -> None:
    """确保父目录存在"""
    path.parent.mkdir(parents=True, exist_ok=True)


def parse_chapter_range(range_str: str) -> List[int]:
    """
    旧API（向后兼容）。推荐使用 ChapterFilter.parse()。
    - "5" -> [5] （表示仅第5章）
    - "5-10" -> [5, 10]
    """
    if not range_str:
        return []
    if "-" in range_str:
        parts = range_str.split("-", 1)
        try:
            start = int(parts[0].strip())
            end = int(parts[1].strip())
            return [start, end]
        except ValueError:
            return []
    try:
        return [int(range_str.strip())]
    except ValueError:
        return []


# ==================== 任务状态存储 ====================

class TaskStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"          # 待处理
    IN_PROGRESS = "in_progress"  # 处理中
    IGNORED = "ignored"          # 已确认忽略
    FIXED = "fixed"              # 已修复


TASK_STATUS_LABELS = {
    TaskStatus.PENDING: "待处理",
    TaskStatus.IN_PROGRESS: "处理中",
    TaskStatus.IGNORED: "已忽略",
    TaskStatus.FIXED: "已修复",
}


@dataclass
class TaskStateRecord:
    """单条任务状态记录"""
    status: str = TaskStatus.PENDING.value
    assignee: str = ""
    note: str = ""
    first_seen_at: str = ""
    updated_at: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "assignee": self.assignee,
            "note": self.note,
            "first_seen_at": self.first_seen_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskStateRecord":
        return cls(
            status=data.get("status", TaskStatus.PENDING.value),
            assignee=data.get("assignee", ""),
            note=data.get("note", ""),
            first_seen_at=data.get("first_seen_at", ""),
            updated_at=data.get("updated_at", ""),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class TaskStateStore:
    """
    任务状态存储。
    以 stable_id 为键，保存编辑生命周期状态，便于多次跑报告时沿用。
    """
    states: Dict[str, TaskStateRecord] = field(default_factory=dict)

    def get(self, stable_id: str) -> Optional[TaskStateRecord]:
        return self.states.get(stable_id)

    def get_status(self, stable_id: str) -> str:
        rec = self.states.get(stable_id)
        return rec.status if rec else TaskStatus.PENDING.value

    def set_status(self, stable_id: str, status: str,
                   assignee: str = None, note: str = None,
                   metadata: Dict[str, Any] = None) -> TaskStateRecord:
        """设置任务状态，返回更新后的记录"""
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")

        rec = self.states.get(stable_id)
        if rec is None:
            rec = TaskStateRecord(first_seen_at=now)
            self.states[stable_id] = rec

        rec.status = status
        rec.updated_at = now
        if assignee is not None:
            rec.assignee = assignee
        if note is not None:
            rec.note = note
        if metadata is not None:
            rec.metadata.update(metadata)
        return rec

    def merge_with_current(self, result: CheckResult) -> List[Dict[str, Any]]:
        """
        将当前检查结果与存储的状态合并。
        返回列表：每条包含 issue 基本信息 + status/assignee/note
        """
        merged = []
        for issue in result.issues:
            sid = issue.stable_id
            rec = self.states.get(sid)
            if rec:
                status = rec.status
                assignee = rec.assignee
                note = rec.note
            else:
                status = TaskStatus.PENDING.value
                assignee = ""
                note = ""
            merged.append({
                "stable_id": sid,
                "issue": issue,
                "status": status,
                "assignee": assignee,
                "note": note,
            })
        return merged

    def to_dict(self) -> Dict[str, Any]:
        return {
            sid: rec.to_dict()
            for sid, rec in self.states.items()
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskStateStore":
        states = {}
        for sid, rec_data in (data or {}).items():
            states[sid] = TaskStateRecord.from_dict(rec_data)
        return cls(states=states)

    def merge(self, other: "TaskStateStore", overwrite: bool = False) -> int:
        """合并另一个状态存储。

        Args:
            other: 要合并的状态存储
            overwrite: 是否覆盖已有状态（默认 False，保留已有状态）

        Returns:
            新增/更新的记录数
        """
        count = 0
        for sid, rec in other.states.items():
            if sid not in self.states or overwrite:
                self.states[sid] = TaskStateRecord(
                    status=rec.status,
                    assignee=rec.assignee,
                    note=rec.note,
                    first_seen_at=rec.first_seen_at or self.states[sid].first_seen_at if sid in self.states else "",
                    updated_at=rec.updated_at,
                    metadata=dict(rec.metadata),
                )
                count += 1
        return count

    def summary_by_assignee(self) -> Dict[str, Dict[str, int]]:
        """按负责人汇总统计。

        返回: { assignee: { pending: N, in_progress: N, fixed: N, ignored: N, total: N }, ... }
        """
        summary: Dict[str, Dict[str, int]] = {}
        for rec in self.states.values():
            who = rec.assignee or "未分配"
            if who not in summary:
                summary[who] = {
                    "pending": 0,
                    "in_progress": 0,
                    "fixed": 0,
                    "ignored": 0,
                    "total": 0,
                }
            status_key = rec.status if rec.status in summary[who] else "pending"
            summary[who][status_key] += 1
            summary[who]["total"] += 1
        return summary

    def summary_by_status(self) -> Dict[str, int]:
        """按状态汇总统计。"""
        counts: Dict[str, int] = {}
        for rec in self.states.values():
            counts[rec.status] = counts.get(rec.status, 0) + 1
        return counts


# ==================== 报告快照 & 对比 ====================

@dataclass
class ReportSnapshotIssue:
    """快照中保存的精简 Issue 信息"""
    stable_id: str
    type: str
    severity: str
    message: str
    file_path: str = ""
    line_number: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "type": self.type,
            "severity": self.severity,
            "message": self.message,
            "file_path": self.file_path,
            "line_number": self.line_number,
        }


@dataclass
class ReportSnapshot:
    """
    报告快照，用于两次检查之间做对比。
    只存精简信息，文件体积小，加载快。
    """
    created_at: str = ""
    files_scanned: List[str] = field(default_factory=list)
    issue_count: Dict[str, int] = field(default_factory=dict)
    issues: List[ReportSnapshotIssue] = field(default_factory=list)
    ignore_stats: Dict[str, Any] = field(default_factory=dict)
    dedup_stats: Dict[str, Any] = field(default_factory=dict)

    def issue_ids(self) -> Set[str]:
        return {i.stable_id for i in self.issues}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created_at": self.created_at,
            "files_scanned": self.files_scanned,
            "issue_count": self.issue_count,
            "issues": [
                {
                    "stable_id": i.stable_id,
                    "type": i.type,
                    "severity": i.severity,
                    "message": i.message,
                    "file_path": i.file_path,
                    "line_number": i.line_number,
                }
                for i in self.issues
            ],
            "ignore_stats": self.ignore_stats,
            "dedup_stats": self.dedup_stats,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReportSnapshot":
        issues = []
        for item in data.get("issues", []):
            issues.append(ReportSnapshotIssue(
                stable_id=item.get("stable_id", ""),
                type=item.get("type", ""),
                severity=item.get("severity", ""),
                message=item.get("message", ""),
                file_path=item.get("file_path", ""),
                line_number=item.get("line_number"),
            ))
        return cls(
            created_at=data.get("created_at", ""),
            files_scanned=list(data.get("files_scanned", [])),
            issue_count=dict(data.get("issue_count", {})),
            issues=issues,
            ignore_stats=dict(data.get("ignore_stats", {})),
            dedup_stats=dict(data.get("dedup_stats", {})),
        )

    @classmethod
    def from_result(cls, result: CheckResult) -> "ReportSnapshot":
        """从 CheckResult 生成快照"""
        from datetime import datetime
        issues = []
        for issue in result.issues:
            issues.append(ReportSnapshotIssue(
                stable_id=issue.stable_id,
                type=issue.type.value,
                severity=issue.severity.value,
                message=issue.message,
                file_path=str(issue.file_path) if issue.file_path else "",
                line_number=issue.line_number,
            ))
        return cls(
            created_at=datetime.now().isoformat(timespec="seconds"),
            files_scanned=[str(f) for f in result.files_scanned],
            issue_count=dict(result.issue_count),
            issues=issues,
            ignore_stats=dict(result.ignore_stats or {}),
            dedup_stats=dict(result.dedup_stats or {}),
        )


@dataclass
class ReportDiffResult:
    """报告对比结果"""
    new_issues: List[ReportSnapshotIssue] = field(default_factory=list)
    resolved_issues: List[ReportSnapshotIssue] = field(default_factory=list)
    unchanged_issues: List[ReportSnapshotIssue] = field(default_factory=list)
    previous: Optional[ReportSnapshot] = None
    current: Optional[ReportSnapshot] = None

    @property
    def new_count(self) -> int:
        return len(self.new_issues)

    @property
    def resolved_count(self) -> int:
        return len(self.resolved_issues)

    @property
    def unchanged_count(self) -> int:
        return len(self.unchanged_issues)

    @property
    def total_current(self) -> int:
        return len(self.unchanged_issues) + len(self.new_issues)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": {
                "new_count": self.new_count,
                "resolved_count": self.resolved_count,
                "unchanged_count": self.unchanged_count,
                "total_current": self.total_current,
            },
            "previous": self.previous.to_dict() if self.previous else None,
            "current": self.current.to_dict() if self.current else None,
            "new_issues": [i.to_dict() for i in self.new_issues],
            "resolved_issues": [i.to_dict() for i in self.resolved_issues],
            "unchanged_issues": [i.to_dict() for i in self.unchanged_issues],
        }


def diff_report_snapshots(old: ReportSnapshot, new: ReportSnapshot) -> ReportDiffResult:
    """
    对比两份报告快照，返回 diff 结果。
    - new_issues: 本次新增（上次没有）
    - resolved_issues: 本次已解决（上次有，本次没有）
    - unchanged_issues: 两次都有（仍未处理）
    """
    old_ids = {i.stable_id: i for i in old.issues}
    new_ids = {i.stable_id: i for i in new.issues}

    new_issues = [new_ids[sid] for sid in new_ids if sid not in old_ids]
    resolved_issues = [old_ids[sid] for sid in old_ids if sid not in new_ids]
    unchanged_issues = [new_ids[sid] for sid in new_ids if sid in old_ids]

    new_issues.sort(key=lambda i: (i.severity, i.stable_id))
    resolved_issues.sort(key=lambda i: (i.severity, i.stable_id))
    unchanged_issues.sort(key=lambda i: (i.severity, i.stable_id))

    return ReportDiffResult(
        new_issues=new_issues,
        resolved_issues=resolved_issues,
        unchanged_issues=unchanged_issues,
        previous=old,
        current=new,
    )


# ==================== 进度看板 ====================

@dataclass
class DashboardTrendPoint:
    """看板上的一个数据点（某一天的快照）"""
    date: str
    total: int
    new_count: int
    resolved_count: int
    error_count: int
    warning_count: int
    info_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "total": self.total,
            "new_count": self.new_count,
            "resolved_count": self.resolved_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
        }


@dataclass
class DashboardData:
    """进度看板数据"""
    points: List[DashboardTrendPoint] = field(default_factory=list)

    @property
    def latest_total(self) -> int:
        return self.points[-1].total if self.points else 0

    @property
    def latest_new(self) -> int:
        return self.points[-1].new_count if self.points else 0

    @property
    def latest_resolved(self) -> int:
        return self.points[-1].resolved_count if self.points else 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "points": [p.to_dict() for p in self.points],
            "summary": {
                "days": len(self.points),
                "latest_total": self.latest_total,
                "latest_new": self.latest_new,
                "latest_resolved": self.latest_resolved,
            }
        }


def build_dashboard_trend(snapshots: List[ReportSnapshot]) -> DashboardData:
    """从历史快照构建看板趋势数据。

    输入按日期升序排列的快照列表。
    """
    points: List[DashboardTrendPoint] = []
    prev_ids: Set[str] = set()

    for snap in snapshots:
        curr_ids = {i.stable_id for i in snap.issues}
        new_count = len(curr_ids - prev_ids)
        resolved_count = len(prev_ids - curr_ids) if prev_ids else 0

        error_count = snap.issue_count.get("error", 0)
        warning_count = snap.issue_count.get("warning", 0)
        info_count = snap.issue_count.get("info", 0) + snap.issue_count.get("suggestion", 0)

        # 日期从 created_at 中提取
        date_str = snap.created_at[:10] if snap.created_at else "unknown"

        points.append(DashboardTrendPoint(
            date=date_str,
            total=len(curr_ids),
            new_count=new_count,
            resolved_count=resolved_count,
            error_count=error_count,
            warning_count=warning_count,
            info_count=info_count,
        ))
        prev_ids = curr_ids

    return DashboardData(points=points)
