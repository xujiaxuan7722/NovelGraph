# -*- coding: utf-8 -*-
"""导出：面板/问答兼容的三元组 JSON（含来源层）、登记簿、运行摘要。"""
import json
import os


def export(out_dir, cands, additions, inferred, registry, summary):
    os.makedirs(out_dir, exist_ok=True)
    triples = []
    for c in sorted(cands.values(), key=lambda x: (-x["votes"], x["head"])):
        triples.append({"head": c["head"], "relation": c["relation"], "tail": c["tail"],
                        "votes": c["votes"], "evidence": c["evidence"],
                        "evidence_src": c.get("evidence_src"), "evidences": c.get("evidences", []),
                        "chapters": c.get("chapters", []), "source": "LLM抽取",
                        "review": c.get("review", "")})
    for a in additions:
        triples.append({**a, "source": "模型知识"})
    for i in inferred:
        triples.append({"head": i["head"], "relation": i["relation"], "tail": i["tail"],
                        "votes": 0, "evidence": None, "evidence_src": None,
                        "source": "推导", "rule": i["rule"], "support": i["support"]})
    json.dump(triples, open(os.path.join(out_dir, "triples.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    registry.save(os.path.join(out_dir, "registry.json"))
    json.dump(summary, open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return triples
