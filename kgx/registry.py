# -*- coding: utf-8 -*-
"""滚动实体登记簿：跨章节的实体记忆（规范名、别名、类型、一句话身份、出现章节）。

- 抽取时全量渲染进 prompt（Gemini 上下文足够，不做活动窗口裁剪）；
- 模型每次返回对登记簿的增量更新（新实体/新别名/身份修正），程序校验后并入；
- 所有名字必须在文本中出现过（grounding），防止幻觉加人。
"""
import json
from collections import OrderedDict


class Registry:
    def __init__(self):
        self.entities = OrderedDict()   # canon -> dict(type, aliases:set, identity, chapters:set, mentions:int)
        self.alias_index = {}           # 任一名字 -> canon
        self._guard = None              # 别名守卫配置（set_guard 后生效）
        self.guard_rejects = 0

    def set_guard(self, schema, text):
        """09-03 别名抢注守卫（三国实测 266 个实体被污染的教训）：
        ① 泛称拒收；② 人物同姓二字异名拒收（张辽≠张郃）；③ 强名拒收（别名自身频次逼近规范名，
        如孙策挂孙权、汉中挂荆州）；单字简称仅允许挂到"以它结尾且高频(≥100)"的规范名（操→曹操✓ 凌操✗）。"""
        self._guard = {"surnames": set(schema.surnames), "generic": set(schema.generic_names),
                       "compound": set(schema.prescan.get("compound_surnames", []) or []),
                       "text": text, "freq": {}}

    def _freq(self, n):
        f = self._guard["freq"]
        if n not in f:
            f[n] = self._guard["text"].count(n)
        return f[n]

    def _alias_ok(self, canon, alias):
        g = self._guard
        if g is None:
            return True
        if alias in g["generic"] or alias in g["surnames"]:
            return False
        if len(alias) == 1:
            return canon.endswith(alias) and self._freq(canon) >= 100
        ct = self.entities[canon]["type"]
        if ct == "人物" and len(alias) == 2 and 2 <= len(canon) <= 3 \
                and alias[0] == canon[0] and alias[0] in g["surnames"] and alias[1:] != canon[1:]:
            return False                                    # 同姓二字异名：几乎总是另一个人
        # 强名抢注检查。人物只对"带姓氏结构的独立人名"启用——字/号/俗称（玄德/孔明/凤姐）
        # 在古典小说里常比本名更高频，属正当别名；地点/势力/家族一律启用。
        surnamed = alias[0] in g["surnames"] or alias[:2] in g["compound"]
        canon_sur = canon[:2] if canon[:2] in g["compound"] else (canon[0] if canon[0] in g["surnames"] else None)
        alias_sur = alias[:2] if alias[:2] in g["compound"] else (alias[0] if alias[0] in g["surnames"] else None)
        if ct == "人物" and canon_sur and alias_sur and canon_sur != alias_sur:
            return False                                    # 双方均带姓且姓不同（董卓←李傕）：换姓别名不存在
        strength = (lambda fa: fa >= 20 and fa >= 0.5 * max(self._freq(canon), 1)
                    and not canon.endswith(alias))
        if (ct != "人物" or surnamed) and strength(self._freq(alias)):
            return False
        return True

    # ---------- 基本操作 ----------
    def add(self, name, etype, aliases=(), identity="", chapter=None, grounded=True):
        name = name.strip()
        if not name:
            return None
        canon = self.alias_index.get(name)
        if canon is None:
            canon = name
            self.entities[canon] = {"type": etype, "aliases": set(), "identity": identity or "",
                                    "chapters": set(), "mentions": 0, "grounded": grounded}
            self.alias_index[canon] = canon
        ent = self.entities[canon]
        if etype and not ent["type"]:
            ent["type"] = etype
        for a in aliases:
            self.add_alias(canon, a)
        if identity and (not ent["identity"] or len(identity) > len(ent["identity"])):
            ent["identity"] = identity
        if chapter is not None:
            ent["chapters"].add(chapter)
        ent["mentions"] += 1
        return canon

    def add_alias(self, canon, alias):
        alias = alias.strip()
        if not alias or alias == canon:
            return
        other = self.alias_index.get(alias)
        if other is None:
            if not self._alias_ok(canon, alias):
                self.guard_rejects += 1
                self.entities[canon].setdefault("alias_conflicts", set()).add(alias)
                return
            self.entities[canon]["aliases"].add(alias)
            self.alias_index[alias] = canon
        elif other != canon:
            # 别名已属于另一实体：记冲突，不强并（留给别名归并阶段）
            self.entities[canon].setdefault("alias_conflicts", set()).add(alias)

    def resolve(self, name):
        return self.alias_index.get(name.strip())

    def merge(self, keep, drop):
        """把 drop 并入 keep（别名归并阶段调用）"""
        if keep == drop or drop not in self.entities or keep not in self.entities:
            return
        k, d = self.entities[keep], self.entities[drop]
        k["aliases"] |= {drop} | d["aliases"]
        k["chapters"] |= d["chapters"]
        k["mentions"] += d["mentions"]
        if not k["identity"]:
            k["identity"] = d["identity"]
        for a in {drop} | d["aliases"]:
            self.alias_index[a] = keep
        del self.entities[drop]

    def names_of(self, canon):
        return {canon} | self.entities[canon]["aliases"]

    # ---------- 渲染 ----------
    def render(self, types=None, max_items=None):
        lines = []
        items = [(c, e) for c, e in self.entities.items() if not types or e["type"] in types]
        items.sort(key=lambda x: -x[1]["mentions"])
        if max_items:
            items = items[:max_items]
        for c, e in items:
            al = "｜".join(sorted(e["aliases"])) if e["aliases"] else ""
            ident = e["identity"] or ""
            lines.append(f"- {c}" + (f"（{al}）" if al else "") + f"［{e['type']}］" + (f"：{ident}" if ident else ""))
        return "\n".join(lines)

    # ---------- 持久化 ----------
    def to_json(self):
        return {c: {"type": e["type"], "aliases": sorted(e["aliases"]), "identity": e["identity"],
                    "chapters": sorted(e["chapters"]), "mentions": e["mentions"]}
                for c, e in self.entities.items()}

    def save(self, path):
        json.dump(self.to_json(), open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    @classmethod
    def load(cls, path):
        r = cls()
        for c, e in json.load(open(path, encoding="utf-8")).items():
            r.add(c, e["type"], e.get("aliases", []), e.get("identity", ""))
            r.entities[c]["chapters"] = set(e.get("chapters", []))
            r.entities[c]["mentions"] = e.get("mentions", 0)
        return r
