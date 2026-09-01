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
