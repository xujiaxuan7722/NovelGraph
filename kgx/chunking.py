# -*- coding: utf-8 -*-
"""按章节切分并打包；每个打包块保留章节编号与字符偏移，供证据溯源。"""
import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class Chapter:
    index: int          # 1-based
    title: str
    text: str           # 含标题行
    start: int          # 在全文中的起始偏移


@dataclass
class Pack:
    id: str
    chapters: List[Chapter] = field(default_factory=list)

    @property
    def text(self):
        return "\n\n".join(c.text for c in self.chapters)

    @property
    def label(self):
        return f"第{self.chapters[0].index}–{self.chapters[-1].index}章" \
            if len(self.chapters) > 1 else f"第{self.chapters[0].index}章"


def split_chapters(text: str, chapter_pattern: str) -> List[Chapter]:
    """按章节标题正则（行首）切分；标题前的内容（序/书名）并入第 1 章或丢弃。"""
    matches = list(re.finditer(chapter_pattern, text, flags=re.M))
    if not matches:
        return [Chapter(1, "全文", text, 0)]
    chapters = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chapters.append(Chapter(i + 1, m.group(0).strip(), text[start:end].strip(), start))
    return chapters


def pack_chapters(chapters: List[Chapter], per_call: int, max_chars: int) -> List[Pack]:
    """连续章节打包：最多 per_call 章或 max_chars 字符（至少 1 章）。"""
    packs, buf = [], []
    for ch in chapters:
        if buf and (len(buf) >= per_call or sum(len(c.text) for c in buf) + len(ch.text) > max_chars):
            packs.append(Pack(f"p{len(packs)+1:03d}", buf))
            buf = []
        buf.append(ch)
    if buf:
        packs.append(Pack(f"p{len(packs)+1:03d}", buf))
    return packs


def split_sentences(text: str):
    """粗粒度分句（用于证据检索）"""
    return [s.strip() for s in re.split(r"[。！？；\n]", text) if s.strip()]
