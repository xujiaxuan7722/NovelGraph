# -*- coding: utf-8 -*-
"""评估：
- 有金标准时（如红楼梦的 181 条手工三元组）算 P/R/F1（金标准名字经登记簿归一）；
- 无金标准时：抽样输出供人工核对（面板可勾选）。
"""
import json
import random
from collections import defaultdict


def eval_against_gold(schema, registry, triples, gold_triples, layers=("LLM抽取",)):
    def canon_gold(h, r, t):
        hh, tt = registry.resolve(h) or h, registry.resolve(t) or t
        return schema.canon(hh, r, tt)
    gold = {canon_gold(*g) for g in gold_triples}
    pred = {schema.canon(t["head"], t["relation"], t["tail"])
            for t in triples if t.get("source") in layers}
    tp = pred & gold
    p = len(tp) / len(pred) if pred else 0.0
    r = len(tp) / len(gold) if gold else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    by_rel = defaultdict(lambda: [0, 0])
    for h, rr, t in gold:
        by_rel[rr][1] += 1
        if (h, rr, t) in tp:
            by_rel[rr][0] += 1
    return {"layers": list(layers), "pred": len(pred), "gold": len(gold), "hit": len(tp),
            "precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4),
            "by_relation": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_rel.items(), key=lambda x: -x[1][1])},
            "missed": sorted(gold - pred)[:40], "extra_sample": sorted(pred - gold)[:40]}


def sample_for_human(triples, n=50, seed=7, path=None):
    rng = random.Random(seed)
    pool = [t for t in triples if t.get("source") == "LLM抽取"]
    sample = rng.sample(pool, min(n, len(pool)))
    rows = [{"head": t["head"], "relation": t["relation"], "tail": t["tail"],
             "evidence": t["evidence"], "chapters": t.get("chapters"), "人工判定": ""} for t in sample]
    if path:
        json.dump(rows, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return rows
