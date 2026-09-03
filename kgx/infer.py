# -*- coding: utf-8 -*-
"""推理补全：按 schema.inference 规则前向推理，每条结论带推导链。"""
from itertools import product


def _index(schema, triples):
    """关系名 -> set of (h, t)；对称关系两个方向都放"""
    idx = {}
    for h, r, t in triples:
        idx.setdefault(r, set()).add((h, t))
        if r in schema.symmetric_relations:
            idx[r].add((t, h))
    return idx


def _match(idx, conditions, binding=None):
    """回溯匹配：conditions [[h, "r1|r2", t], ...]，变量以 ? 开头。yield 绑定字典。"""
    binding = binding or {}
    if not conditions:
        yield binding
        return
    h, rels, t = conditions[0]
    rest = conditions[1:]
    for r in rels.split("|"):
        for (hh, tt) in idx.get(r, ()):
            b = dict(binding)
            ok = True
            for var, val in ((h, hh), (t, tt)):
                if var.startswith("?"):
                    if var in b and b[var] != val:
                        ok = False; break
                    b[var] = val
                elif var != val:
                    ok = False; break
            if ok:
                yield from _match(idx, rest, b)


def _dominant_filter(idx, premise, votes):
    """对 dominant_premise 指向的前提：同一主语在该前提关系上只保留票数最高的一条事实。
    返回覆盖了这些关系的 idx 副本（09-03 拍板 A：曹操双隶属只传票数最高的势力）。"""
    idx2 = dict(idx)
    _, rels, _ = premise
    for r in rels.split("|"):
        best = {}
        for (h, t) in idx.get(r, ()):
            v = votes.get((h, r, t), votes.get((t, r, h), 1))
            if h not in best or v > best[h][1]:
                best[h] = (t, v)
        idx2[r] = {(h, tv[0]) for h, tv in best.items()}
    return idx2


def infer(schema, triples, max_rounds=3, votes=None):
    """triples: iterable of (h, r, t)（已确认的）。返回新增列表 [{head, relation, tail, rule, support}]"""
    votes = votes or {}
    known = set(triples)
    new_all = []
    for _ in range(max_rounds):
        idx = _index(schema, known)
        added = []
        added_pairs = {}   # rel -> set of (h,t)，本轮新增（含对称反向），供 unless 与去重参考
        def _has(r, h, t):
            return (h, t) in idx.get(r, ()) or (h, t) in added_pairs.get(r, set())
        for rule in schema.inference:
            ch, cr, ct = rule.conclusion
            rule_idx = idx
            if rule.dominant_premise is not None:
                rule_idx = _dominant_filter(idx, rule.conditions[rule.dominant_premise], votes)
            for b in _match(rule_idx, rule.conditions):
                h, t = b.get(ch, ch), b.get(ct, ct)
                if h == t:
                    continue
                if rule.unless:
                    uh, ur, ut = rule.unless
                    uh, ut = b.get(uh, uh), b.get(ut, ut)
                    if any(_has(r, uh, ut) for r in ur.split("|")):
                        continue
                key = schema.canon(h, cr, t)
                if key in known or _has(cr, h, t):
                    continue
                added_pairs.setdefault(cr, set()).add((h, t))
                if cr in schema.symmetric_relations:
                    added_pairs[cr].add((t, h))
                support = []
                for (sh, sr, st) in rule.conditions:
                    support.append((b.get(sh, sh), sr, b.get(st, st)))
                added.append((key[0], key[1], key[2], rule.name, support))
        if not added:
            break
        for h, r, t, name, support in added:
            known.add((h, r, t))
            new_all.append({"head": h, "relation": r, "tail": t, "source": "推导",
                            "rule": name, "support": [f"{a} {b} {c}" for a, b, c in support]})
    return new_all
