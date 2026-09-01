# -*- coding: utf-8 -*-
"""程序校验（零 LLM）：
1. 实体归一：head/tail 经登记簿解析为规范名；
2. schema 约束：关系在闭集内、head/tail 类型匹配；
3. 证据定位：evidence 必须出自该打包块原文（精确，否则去空白后模糊），
   且同时提到 head 与 tail（含各自别名）——否则回该块原文检索一句同时提到双方的句子补证；
4. 不合格者进 dropped（带原因），供审计。
"""
import re

from .chunking import split_sentences


def _norm(s):
    return re.sub(r"\s+", "", s or "")


class Validator:
    def __init__(self, schema, registry):
        self.schema = schema
        self.registry = registry

    def _mentions(self, canon, text):
        return any(n in text for n in self.registry.names_of(canon))

    def _search_evidence(self, pack_text, h, t, max_len=80):
        best = None
        for s in split_sentences(pack_text):
            if len(s) > max_len:
                continue
            if self._mentions(h, s) and self._mentions(t, s):
                if best is None or len(s) < len(best):
                    best = s
        return best

    def validate(self, rel, pack_text):
        """返回 (ok: bool, rel_or_None, reason)"""
        h_raw, t_raw, r = rel.get("head", ""), rel.get("tail", ""), rel.get("relation", "")
        h, t = self.registry.resolve(h_raw), self.registry.resolve(t_raw)
        if not h or not t:
            return False, None, f"实体未登记:{h_raw if not h else t_raw}"
        if h == t:
            return False, None, "头尾相同"
        if r not in self.schema.relations:
            return False, None, f"关系不在闭集:{r}"
        ht, tt = self.registry.entities[h]["type"], self.registry.entities[t]["type"]
        if not self.schema.type_ok(r, ht, tt):
            return False, None, f"类型不符:{r}({ht}→{tt})"
        ev = (rel.get("evidence") or "").strip()
        ev_src = "模型引用"
        grounded = ev and (ev in pack_text or _norm(ev) in _norm(pack_text))
        if not grounded or not (self._mentions(h, ev) and self._mentions(t, ev)):
            found = self._search_evidence(pack_text, h, t)
            if not found:
                return False, None, "无合格证据（证据不在原文或未同时提及双方）"
            ev, ev_src = found, "程序检索"
        out = dict(rel)
        out.update({"head": h, "tail": t, "evidence": ev, "evidence_src": ev_src})
        return True, out, ""
