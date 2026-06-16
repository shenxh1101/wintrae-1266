"""引用检查模块 - 文件引用、图片说明检查"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from urllib.parse import unquote, urlparse

from .config import CheckConfig, IgnoreRules
from .document import Document, ImageRef, LinkRef, find_documents, load_document
from .utils import (
    ChapterFilter,
    CheckResult,
    Issue,
    IssueType,
    Severity,
    deduplicate_issues,
    is_recently_modified,
)


class ReferenceChecker:
    """引用检查器"""

    def __init__(
        self,
        config: Optional[CheckConfig] = None,
        ignore_rules: Optional[IgnoreRules] = None,
    ):
        self.config = config or CheckConfig()
        self.ignore_rules = ignore_rules
        self._chapter_filter = ChapterFilter.parse(self.config.chapter_filter_str)
        self._all_files: Set[Path] = set()
        self._all_anchors: Dict[Path, Set[str]] = defaultdict(set)
        self._all_documents: List[Document] = []
        self._all_issues_before_filter: List[Issue] = []

    def check(self, root: Path) -> CheckResult:
        """执行完整的引用检查"""
        result = CheckResult()

        self._collect_files(root)
        documents = self._load_documents(root)
        result.files_scanned = [d.file_path for d in documents]

        for doc in documents:
            self._check_document_images(doc, result)
            self._check_document_links(doc, root, result)

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

    def _collect_files(self, root: Path) -> None:
        """收集目录下的所有文件路径"""
        for dirpath, dirnames, filenames in os.walk(root):
            for exc in self.config.exclude_patterns:
                if exc in dirnames:
                    dirnames.remove(exc)
            for filename in filenames:
                self._all_files.add(Path(dirpath) / filename)

    def _load_documents(self, root: Path) -> List[Document]:
        """加载并索引所有文档"""
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
            if not self._chapter_filter.matches(doc.chapter_index):
                continue
            documents.append(doc)
            self._index_anchors(doc)

        self._all_documents = documents
        return documents

    def _index_anchors(self, doc: Document) -> None:
        """索引文档中的所有锚点（标题ID）"""
        anchors: Set[str] = set()
        for heading in doc.headings:
            anchor = self._heading_to_anchor(heading.text)
            anchors.add(anchor)
            anchors.add(heading.text.strip().lower())
        self._all_anchors[doc.file_path.resolve()] = anchors

    @staticmethod
    def _heading_to_anchor(text: str) -> str:
        """将标题转换为Markdown锚点格式"""
        anchor = text.strip().lower()
        anchor = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", anchor)
        anchor = re.sub(r"[\s_]+", "-", anchor)
        anchor = re.sub(r"-+", "-", anchor).strip("-")
        return anchor

    def _check_document_images(self, doc: Document, result: CheckResult) -> None:
        """检查文档中的图片"""
        for img in doc.images:
            if not img.has_caption:
                self._all_issues_before_filter.append(Issue(
                    type=IssueType.MISSING_IMAGE_ALT,
                    severity=Severity.WARNING,
                    message=f"图片缺少说明文字 (alt text)",
                    file_path=doc.file_path,
                    line_number=img.line_number,
                    context=f"图片源: {img.src}"[:80],
                    suggestion="为图片添加描述性的替代文本，提高可访问性和可读性",
                    metadata={"image_src": img.src},
                ))

            self._check_image_exists(img, doc, result)

    def _check_image_exists(self, img: ImageRef, doc: Document, result: CheckResult) -> None:
        """检查图片文件是否存在"""
        if not img.src:
            return
        if img.src.startswith(("http://", "https://", "data:")):
            return

        try:
            parsed = urlparse(img.src)
            if parsed.scheme:
                return
        except Exception:
            pass

        src_path = unquote(img.src.split("#")[0].split("?")[0])
        candidate_paths: List[Path] = []

        doc_dir = doc.file_path.parent
        candidate_paths.append((doc_dir / src_path).resolve())
        candidate_paths.append((Path.cwd() / src_path).resolve())

        exists = False
        for p in candidate_paths:
            try:
                if p.exists():
                    exists = True
                    break
            except Exception:
                pass

        if not exists:
            self._all_issues_before_filter.append(Issue(
                type=IssueType.BROKEN_LINK,
                severity=Severity.ERROR,
                message=f"图片文件不存在: {img.src}",
                file_path=doc.file_path,
                line_number=img.line_number,
                context=f"已尝试: {', '.join(str(p) for p in candidate_paths[:2])}",
                suggestion=f"确认图片路径是否正确，或创建图片文件 {src_path}",
                metadata={"ref_type": "image", "target": img.src},
            ))

    def _check_document_links(self, doc: Document, root: Path, result: CheckResult) -> None:
        """检查文档中的链接"""
        for link in doc.links:
            if link.is_external:
                continue
            if link.is_anchor:
                self._check_anchor_link(link, doc, result)
            else:
                self._check_file_link(link, doc, root, result)

    def _check_anchor_link(self, link: LinkRef, doc: Document, result: CheckResult) -> None:
        """检查同文档内的锚点链接"""
        anchor = link.target.lstrip("#")
        if not anchor:
            return
        anchors = self._all_anchors.get(doc.file_path.resolve(), set())
        normalized_anchor = self._normalize_anchor(anchor)
        if normalized_anchor not in anchors and anchor not in anchors:
            self._all_issues_before_filter.append(Issue(
                type=IssueType.BROKEN_LINK,
                severity=Severity.WARNING,
                message=f"锚点链接可能失效: #{anchor}",
                file_path=doc.file_path,
                line_number=link.line_number,
                context=f"链接文本: {link.text}"[:60],
                suggestion=f"检查标题拼写，或确认锚点 '{anchor}' 对应的标题是否存在",
                metadata={"ref_type": "anchor", "target": link.target},
            ))

    def _check_file_link(self, link: LinkRef, doc: Document, root: Path, result: CheckResult) -> None:
        """检查跨文档的文件链接"""
        target = link.target.split("#")[0].split("?")[0]
        if not target:
            return

        try:
            parsed = urlparse(target)
            if parsed.scheme or parsed.netloc:
                return
        except Exception:
            pass

        target = unquote(target)
        doc_dir = doc.file_path.parent

        candidate_paths: List[Path] = []
        candidate_paths.append((doc_dir / target).resolve())
        candidate_paths.append((root / target).resolve())
        candidate_paths.append((Path.cwd() / target).resolve())

        resolved_path: Optional[Path] = None
        for p in candidate_paths:
            try:
                if p.exists():
                    resolved_path = p
                    break
            except Exception:
                pass

        if resolved_path is None:
            self._all_issues_before_filter.append(Issue(
                type=IssueType.BROKEN_LINK,
                severity=Severity.ERROR,
                message=f"引用文件不存在: {target}",
                file_path=doc.file_path,
                line_number=link.line_number,
                context=f"链接文本: {link.text}"[:60],
                suggestion=f"确认文件路径是否正确，目标文件可能已被移动或删除",
                metadata={"ref_type": "file", "target": target},
            ))
            return

        if "#" in link.target:
            anchor = link.target.split("#", 1)[1]
            normalized_anchor = self._normalize_anchor(anchor)
            file_anchors = self._all_anchors.get(resolved_path, set())
            if file_anchors and normalized_anchor not in file_anchors and anchor not in file_anchors:
                self._all_issues_before_filter.append(Issue(
                    type=IssueType.BROKEN_LINK,
                    severity=Severity.WARNING,
                    message=f"跨文档锚点可能失效: {target}#{anchor}",
                    file_path=doc.file_path,
                    line_number=link.line_number,
                    context=f"链接文本: {link.text}"[:60],
                    suggestion=f"检查目标文档中是否存在标题对应锚点 '{anchor}'",
                    metadata={"ref_type": "file_anchor", "target": target, "anchor": anchor},
                ))

    @staticmethod
    def _normalize_anchor(anchor: str) -> str:
        """标准化锚点文本用于比较"""
        anchor = anchor.strip().lower()
        anchor = re.sub(r"[^\w\u4e00-\u9fff\s-]", "", anchor)
        anchor = re.sub(r"[\s_]+", "-", anchor)
        return re.sub(r"-+", "-", anchor).strip("-")

    def get_reference_stats(self, root: Path) -> Dict:
        """获取引用统计信息"""
        self._collect_files(root)
        self._load_documents(root)

        stats = {
            "total_documents": len(self._all_documents),
            "total_images": 0,
            "images_without_alt": 0,
            "missing_images": 0,
            "total_links": 0,
            "internal_links": 0,
            "external_links": 0,
            "broken_links": 0,
        }

        temp_result = CheckResult()
        for doc in self._all_documents:
            stats["total_images"] += len(doc.images)
            stats["total_links"] += len(doc.links)
            for link in doc.links:
                if link.is_external:
                    stats["external_links"] += 1
                else:
                    stats["internal_links"] += 1
            self._check_document_images(doc, temp_result)
            self._check_document_links(doc, root, temp_result)

        all_issues = list(self._all_issues_before_filter) + list(temp_result.issues)
        for issue in all_issues:
            if issue.type == IssueType.MISSING_IMAGE_ALT:
                stats["images_without_alt"] += 1
            elif issue.type == IssueType.BROKEN_LINK:
                meta = issue.metadata or {}
                if meta.get("ref_type") == "image":
                    stats["missing_images"] += 1
                else:
                    stats["broken_links"] += 1

        return stats
