"""文档解析模块 - 支持 Markdown 等格式的长篇文档解析"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import frontmatter


@dataclass
class Heading:
    """文档标题节点"""
    level: int
    text: str
    line_number: int
    number_text: Optional[str] = None
    number_value: Optional[Tuple[int, ...]] = None

    def __post_init__(self):
        if self.number_text is None:
            self._extract_chapter_number()

    def _extract_chapter_number(self):
        """从标题中提取章节编号"""
        patterns = [
            r"^第\s*([一二三四五六七八九十百千万零〇\d]+)\s*[章节回卷篇]",
            r"^Chapter\s+(\d+(?:\.\d+)*)",
            r"^(\d+(?:\.\d+)*)[\.\、\s]",
            r"^([一二三四五六七八九十百千万零〇]+)[\.\、\s]",
        ]
        for pattern in patterns:
            match = re.match(pattern, self.text.strip())
            if match:
                self.number_text = match.group(1)
                self.number_value = _parse_number(self.number_text)
                break


@dataclass
class ImageRef:
    """图片引用"""
    src: str
    alt_text: str
    line_number: int
    caption: Optional[str] = None

    @property
    def has_caption(self) -> bool:
        return bool(self.alt_text.strip()) or bool(self.caption)


@dataclass
class LinkRef:
    """链接/文件引用"""
    target: str
    text: str
    line_number: int
    is_external: bool = False
    is_anchor: bool = False

    def __post_init__(self):
        if self.target.startswith(("http://", "https://", "mailto:")):
            self.is_external = True
        elif self.target.startswith("#"):
            self.is_anchor = True


@dataclass
class TermOccurrence:
    """术语出现位置"""
    term: str
    line_number: int
    context: str = ""
    file_path: Optional[Path] = None


@dataclass
class Document:
    """单个文档对象"""
    file_path: Path
    content: str
    lines: List[str] = field(default_factory=list)
    headings: List[Heading] = field(default_factory=list)
    images: List[ImageRef] = field(default_factory=list)
    links: List[LinkRef] = field(default_factory=list)
    front_matter: dict = field(default_factory=dict)
    encoding: str = "utf-8"
    mtime: float = 0.0

    def __post_init__(self):
        if not self.lines:
            self.lines = self.content.splitlines()
        if self.mtime == 0:
            try:
                self.mtime = os.path.getmtime(self.file_path)
            except OSError:
                pass

    @property
    def filename(self) -> str:
        return self.file_path.name

    @property
    def title(self) -> str:
        if "title" in self.front_matter:
            return str(self.front_matter["title"])
        for h in self.headings:
            if h.level == 1:
                return h.text
        return self.filename

    @property
    def chapter_index(self) -> Optional[int]:
        """从文件名或front matter推断章节序号"""
        if "chapter" in self.front_matter:
            try:
                return int(self.front_matter["chapter"])
            except (ValueError, TypeError):
                pass
        match = re.search(r"(\d+)", self.file_path.stem)
        if match:
            return int(match.group(1))
        for h in self.headings:
            if h.level <= 2 and h.number_value and len(h.number_value) == 1:
                return h.number_value[0]
        return None

    def iter_text_chunks(self, chunk_size: int = 100) -> Iterator[Tuple[int, str]]:
        """按行块迭代文本"""
        for i in range(0, len(self.lines), chunk_size):
            chunk_lines = self.lines[i:i + chunk_size]
            yield i + 1, "\n".join(chunk_lines)

    def get_line_context(self, line_number: int, context_lines: int = 2) -> str:
        """获取指定行的上下文"""
        start = max(0, line_number - context_lines - 1)
        end = min(len(self.lines), line_number + context_lines)
        lines = []
        for i in range(start, end):
            prefix = ">>>" if i == line_number - 1 else "   "
            lines.append(f"{prefix} {i + 1:4d}: {self.lines[i]}")
        return "\n".join(lines)


def _parse_number(text: str) -> Optional[Tuple[int, ...]]:
    """解析章节编号为数字元组"""
    text = text.strip()
    if re.match(r"^\d+(\.\d+)*$", text):
        return tuple(int(x) for x in text.split("."))
    return _chinese_number_to_tuple(text)


_CN_NUM_MAP = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "百": 100, "千": 1000, "万": 10000, "亿": 100000000,
}


def _chinese_number_to_tuple(text: str) -> Optional[Tuple[int, ...]]:
    """中文数字转整数"""
    try:
        if text.isdigit():
            return (int(text),)
        result = 0
        temp = 0
        last_unit = 1
        for char in text:
            if char in _CN_NUM_MAP:
                val = _CN_NUM_MAP[char]
                if val >= 10:
                    if temp == 0:
                        temp = 1
                    if val > last_unit:
                        result = (result + temp) * val
                    else:
                        result += temp * val
                    temp = 0
                    last_unit = val
                else:
                    temp = val
        result += temp
        if result > 0:
            return (result,)
    except Exception:
        pass
    return None


def _parse_headings(lines: List[str]) -> List[Heading]:
    """解析 Markdown 标题"""
    headings = []
    in_code_block = False
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            level = len(match.group(1))
            text = match.group(2).strip()
            headings.append(Heading(level=level, text=text, line_number=i))
    return headings


def _parse_images(lines: List[str]) -> List[ImageRef]:
    """解析 Markdown 图片"""
    images = []
    img_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    for i, line in enumerate(lines, 1):
        for match in img_pattern.finditer(line):
            alt_text = match.group(1)
            src = match.group(2).strip().split()[0] if match.group(2).strip() else ""
            images.append(ImageRef(src=src, alt_text=alt_text, line_number=i))
    return images


def _parse_links(lines: List[str]) -> List[LinkRef]:
    """解析 Markdown 链接"""
    links = []
    link_pattern = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)")
    for i, line in enumerate(lines, 1):
        for match in link_pattern.finditer(line):
            text = match.group(1)
            target = match.group(2).strip().split()[0] if match.group(2).strip() else ""
            links.append(LinkRef(target=target, text=text, line_number=i))
    return links


def load_document(file_path: Path, encoding: str = "utf-8") -> Document:
    """加载并解析单个文档"""
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        raw_content = f.read()

    fm = {}
    content = raw_content
    if raw_content.startswith(("---", "+++")):
        try:
            post = frontmatter.loads(raw_content)
            fm = dict(post.metadata)
            content = post.content
        except Exception:
            pass

    lines = content.splitlines()
    doc = Document(
        file_path=file_path,
        content=content,
        lines=lines,
        headings=_parse_headings(lines),
        images=_parse_images(lines),
        links=_parse_links(lines),
        front_matter=fm,
        encoding=encoding,
    )
    return doc


def find_documents(
    root: Path,
    extensions: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> List[Path]:
    """递归查找目录下的文档文件"""
    if extensions is None:
        extensions = [".md", ".markdown", ".txt"]
    if exclude_patterns is None:
        exclude_patterns = [".git", "node_modules", "__pycache__"]

    root = Path(root)
    documents = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not any(
            re.search(p, d) for p in exclude_patterns
        )]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in extensions:
                documents.append(path)

    return sorted(documents)
