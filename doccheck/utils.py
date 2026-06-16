"""通用工具函数"""
from __future__ import annotations

import difflib
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


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

    @property
    def location(self) -> str:
        if self.file_path and self.line_number:
            return f"{self.file_path}:{self.line_number}"
        elif self.file_path:
            return str(self.file_path)
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "message": self.message,
            "file_path": str(self.file_path) if self.file_path else None,
            "line_number": self.line_number,
            "context": self.context,
            "suggestion": self.suggestion,
            "metadata": self.metadata,
        }

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
        )


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
    """检查章节是否在指定范围内"""
    if chapter_range is None or len(chapter_range) == 0:
        return True
    if chapter_idx is None:
        return len(chapter_range) < 2
    if len(chapter_range) == 1:
        return chapter_idx >= chapter_range[0]
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
    """解析章节范围字符串，如 '1-10' 或 '5'"""
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
