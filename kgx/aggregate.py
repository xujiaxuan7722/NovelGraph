# -*- coding: utf-8 -*-
"""聚合：规范化方向、计票、证据择优、functional 冲突检测/裁决。"""
from collections import defaultdict


def aggregate(schema, validated):
    """validated: 已通过 Validator 的关系列表 → 候选字典 {canon_key: cand}"""
    cands = {}
    for r in validated:
        key = schema.canon(r["head"], r["relation"], r["tail"])
        c = cands.setdefault(key, {"head": key[0], "relation": key[1], "tail": key[2],
                                   "votes": 0, "evidences": [], "chapters": set(), "packs": set()})
        c["votes"] += 1
        c["evidences"].append((r.get("evidence_src", ""), r["evidence"]))
        c["chapters"] |= set(r.get("chapters", []))
        c["packs"].add(r.get("pack"))
    for c in cands.values():
        # 证据择优：模型引用优先于程序检索，同档取最短
        c["evidences"].sort(key=lambda x: (x[0] != "模型引用", len(x[1])))
        c["evidence_src"], c["evidence"] = c["evidences"][0]
        c["evidences"] = [e for _, e in c["evidences"][:3]]
        c["chapters"] = sorted(c["chapters"])
        c["packs"] = sorted(c["packs"])
        c["source"] = "LLM抽取"
    return cands


def functional_conflicts(schema, cands):
    """按 schema 的 functional_on 找冲突组：返回 {(rel_group_label, anchor): [keys...]}"""
    groups = defaultdict(list)
    for key, c in cands.items():
        spec = schema.relations.get(c["relation"])
        if not spec or not spec.functional_on:
            continue
        anchor = c["tail"] if spec.functional_on == "tail" else c["head"]
        # 父子/父女 共享"父"的唯一性：按 domain+functional 侧+关系组聚合
        grp = _functional_group(schema, c["relation"])
        groups[(grp, anchor)].append(key)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _functional_group(schema, rel):
    """把同一语义的 functional 关系归组（父子/父女→"父"，母子/母女→"母"），按定义首字近似"""
    d = schema.relations[rel].definition
    return d.split("→")[0] if "→" in d else rel


def resolve_functional(schema, cands, log=print):
    """冲突裁决：保留票数最高者（复核阶段未裁决的兜底），其余移入 dropped 返回"""
    dropped = []
    for (grp, anchor), keys in functional_conflicts(schema, cands).items():
        keys.sort(key=lambda k: -cands[k]["votes"])
        for k in keys[1:]:
            log(f"  functional冲突：{anchor} 的{grp} 保留 {keys[0][0]}，丢弃 {k} (票{cands[k]['votes']})")
            dropped.append(cands.pop(k))
    return dropped
