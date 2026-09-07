# -*- coding: utf-8 -*-
"""评估：
- 有金标准时（如红楼梦的 181 条手工三元组）算 P/R/F1（金标准名字经登记簿归一）；
- 无金标准时：抽样输出供人工核对（面板可勾选）。
"""
import json
import os
import random
from collections import defaultdict


def resolve_gold_name(schema, registry, name):
    """金标准名字→登记簿规范名。金标准按全名/带括号写（"贾探春"、"贾家（宁国府）"），
    文本里可能从不出现全名；依次尝试：原名 → 括号内/外 → 去单字姓 → 原样返回（不命中）。"""
    r = registry.resolve(name)
    if r:
        return r
    if "（" in name and name.endswith("）"):
        outer, inner = name.split("（", 1)[0], name[name.index("（") + 1:-1]
        for cand in (inner, outer):
            r = registry.resolve(cand)
            if r:
                return r
    if len(name) >= 3 and name[0] in schema.surnames:
        r = registry.resolve(name[1:])
        if r:
            return r
    return name


def _variants(schema, registry, name, gold_aliases):
    """金标准名字的全部可接受形：本名 + 别名表各形 + 各自的登记簿解析 + 括号/去姓兜底"""
    out = {name}
    for form in [name] + list(gold_aliases.get(name, ())):
        out.add(form)
        r = registry.resolve(form)
        if r:
            out.add(r)
    r = resolve_gold_name(schema, registry, name)
    out.add(r)
    return out


def eval_against_gold(schema, registry, triples, gold_triples, layers=("LLM抽取",), gold_aliases=None):
    """按条匹配：每条金标准展开成变体三元组集合，预测命中任一变体即中；
    精度按"预测是否命中任何一条金标准的任一变体"计。gold_aliases 来自金标准模块的 aliases 表（评估专用）。"""
    gold_aliases = gold_aliases or {}
    pred = {schema.canon(t["head"], t["relation"], t["tail"])
            for t in triples if t.get("source") in layers}
    items = []
    all_variants = set()
    for h, rr, t in gold_triples:
        vs = {schema.canon(hh, rr, tt)
              for hh in _variants(schema, registry, h, gold_aliases)
              for tt in _variants(schema, registry, t, gold_aliases)}
        items.append(((h, rr, t), vs))
        all_variants |= vs
    matched = [(g, bool(vs & pred)) for g, vs in items]
    hit = sum(1 for _, m in matched if m)
    tp_pred = pred & all_variants
    p = len(tp_pred) / len(pred) if pred else 0.0
    r = hit / len(items) if items else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    by_rel = defaultdict(lambda: [0, 0])
    for (h, rr, t), m in matched:
        by_rel[rr][1] += 1
        if m:
            by_rel[rr][0] += 1
    return {"layers": list(layers), "pred": len(pred), "gold": len(items), "hit": hit,
            "precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4),
            "by_relation": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_rel.items(), key=lambda x: -x[1][1])},
            "missed": sorted(str(g) for g, m in matched if not m)[:40],
            "extra_sample": sorted(pred - all_variants)[:40]}


def sample_for_human(triples, n=50, seed=7, path=None):
    rng = random.Random(seed)
    pool = [t for t in triples if t.get("source") == "LLM抽取"]
    sample = rng.sample(pool, min(n, len(pool)))
    rows = [{"head": t["head"], "relation": t["relation"], "tail": t["tail"],
             "evidence": t["evidence"], "chapters": t.get("chapters"), "人工判定": ""} for t in sample]
    if path:
        # 已有人工判定的抽检文件不覆盖（曾在定稿重放时把填好的判定冲掉），改写到 .new.json
        if os.path.exists(path):
            try:
                old = json.load(open(path, encoding="utf-8"))
                if any(r.get("人工判定") for r in old):
                    path = path[:-5] + ".new.json" if path.endswith(".json") else path + ".new"
            except Exception:
                pass
        json.dump(rows, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return rows
