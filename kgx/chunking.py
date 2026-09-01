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


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def cn2int(s: str):
    """中文数字→整数（一百十九=119，二十=20，一百零五=105）；无法解析返回 None"""
    total, num, seen = 0, 0, False
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]; seen = True
        elif ch == "十":
            total += (num if num else 1) * 10; num = 0; seen = True
        elif ch == "百":
            total += (num if num else 1) * 100; num = 0; seen = True
        elif ch == "千":
            total += (num if num else 1) * 1000; num = 0; seen = True
        else:
            return None
    return total + num if seen else None


_NUM_IN_TITLE = re.compile(r"第([零〇一二两三四五六七八九十百千]+|\d+)[回章节卷]")


def _title_number(title: str):
    m = _NUM_IN_TITLE.match(title)
    if not m:
        return None
    g = m.group(1)
    return int(g) if g.isdigit() else cn2int(g)


def split_chapters(text: str, chapter_pattern: str, sequential: bool = True) -> List[Chapter]:
    """按章节标题正则（行首）切分；标题前的内容（序/书名）并入第 1 章或丢弃。
    sequential=True 时要求章号连续递增：正文里形如"第四回中既将……"的行会被正则误判为标题，
    其章号与上一章不衔接，予以跳过（红楼梦第五回开头即有此例，曾导致第五回起章号全部偏移 1）。"""
    matches = list(re.finditer(chapter_pattern, text, flags=re.M))
    if sequential and matches:
        kept, expect = [], None
        for m in matches:
            n = _title_number(m.group(0).strip())
            if n is None:                 # 标题里没有可解析的章号：不做校验
                kept.append(m); continue
            if expect is None or n == expect:
                kept.append(m); expect = n + 1
            # 否则视为正文中的伪标题，跳过
        matches = kept
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
