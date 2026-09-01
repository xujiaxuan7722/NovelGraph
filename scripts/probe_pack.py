# -*- coding: utf-8 -*-
"""单块探针：用既有 prescan 词典灌登记簿，对第 1 个打包块跑一次抽取（不补抽、不改 runs 主目录），
报告 JSON 合规 / 关系数 / 新实体数 / evidence 逐字命中率 / 实体可解析率 / usage / 耗时，
并与参考 run 缓存中的同块结果（如 Gemini pack1）用同一口径对照。

用法：
  ./.venv/bin/python scripts/probe_pack.py --provider sensenova [--model deepseek-v4-flash] \
      --dict runs/hlm_v2_c1-3/prescan_dict.json --out runs/probe_ds [--chapters 1-3] \
      [--ref-cache runs/hlm_v2_c1-3/cache/<hash>.json]
"""
import argparse
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kgx.chunking import pack_chapters, split_chapters          # noqa: E402
from kgx.extract import EXTRACT_SCHEMA, build_prompt             # noqa: E402
from kgx.llm import make_llm                                     # noqa: E402
from kgx.registry import Registry                                # noqa: E402
from kgx.schema import load_schema                               # noqa: E402
from kgx.validate import Validator, _norm                        # noqa: E402


def measure(result, schema, registry, pack):
    """同一口径统计一份抽取结果"""
    if not isinstance(result, dict):
        return {"json_ok": False}
    rels = result.get("relations") or []
    ents = result.get("new_entities") or []
    reg = copy.deepcopy(registry)
    grounded_new = 0
    for e in ents:
        if (e.get("name") or "") in pack.text:
            reg.add(e["name"], e.get("type", ""), [a for a in e.get("aliases") or [] if a in pack.text],
                    e.get("identity", ""))
            grounded_new += 1
    v = Validator(schema, reg)
    ev_exact = ev_both = resolvable = closed = passed = 0
    reasons = {}
    for r in rels:
        ev = (r.get("evidence") or "").strip()
        if ev and (ev in pack.text or _norm(ev) in _norm(pack.text)):
            ev_exact += 1
        h, t = reg.resolve(r.get("head", "")), reg.resolve(r.get("tail", ""))
        if h and t:
            resolvable += 1
            if ev and v._mentions(h, ev) and v._mentions(t, ev):
                ev_both += 1
        if r.get("relation") in schema.relations:
            closed += 1
        ok, _, reason = v.validate(r, pack.text)
        if ok:
            passed += 1
        else:
            k = reason.split(":")[0]
            reasons[k] = reasons.get(k, 0) + 1
    n = len(rels) or 1
    return {"json_ok": True, "relations": len(rels), "new_entities": len(ents),
            "new_entities_grounded": grounded_new,
            "evidence_exact": f"{ev_exact}/{len(rels)} ({ev_exact/n:.0%})",
            "evidence_mentions_both": f"{ev_both}/{len(rels)} ({ev_both/n:.0%})",
            "entities_resolvable": f"{resolvable}/{len(rels)} ({resolvable/n:.0%})",
            "relation_in_schema": f"{closed}/{len(rels)} ({closed/n:.0%})",
            "validator_pass": f"{passed}/{len(rels)} ({passed/n:.0%})", "drop_reasons": reasons}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default="schemas/hongloumeng.yaml")
    ap.add_argument("--text", default="data/hongloumeng11.txt")
    ap.add_argument("--dict", default="runs/hlm_v2_c1-3/prescan_dict.json")
    ap.add_argument("--chapters", default="1-3")
    ap.add_argument("--provider", default="sensenova")
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default="runs/probe")
    ap.add_argument("--ref-cache", default=None, help="参考 run 的缓存文件（同块抽取结果）")
    args = ap.parse_args()

    schema = load_schema(args.schema)
    text = open(args.text, encoding="utf-8").read()
    chapters = split_chapters(text, schema.chunking["chapter_pattern"])
    lo, hi = [int(x) for x in args.chapters.split("-")]
    sel = [c for c in chapters if lo <= c.index <= hi]
    packs = pack_chapters(sel, schema.chunking.get("chapters_per_call", 3),
                          schema.chunking.get("max_chars_per_call", 24000))
    pack = packs[0]
    registry = Registry()
    for e in json.load(open(args.dict, encoding="utf-8")):
        registry.add(e["name"], e["type"], e.get("aliases", []), e.get("identity", ""))
    prompt = build_prompt(schema, registry, pack)
    print(f"块 {pack.label}：原文 {len(pack.text)} 字，prompt {len(prompt)} 字，登记簿 {len(registry.entities)} 实体")

    llm = make_llm(args.provider, model=args.model, cache_dir=os.path.join(args.out, "cache"))
    t0 = time.time()
    result, meta = llm.generate(prompt, response_schema=EXTRACT_SCHEMA, tag=f"[探针 {pack.label}]")
    print(f"== {args.provider}/{llm.model}：{time.time()-t0:.0f}s，meta={json.dumps({k: v for k, v in meta.items() if k != 'raw'}, ensure_ascii=False)}")
    if meta.get("parse_error"):
        print("!! JSON 解析失败，原文前 800 字：", meta.get("raw", "")[:800])
    m = measure(result, schema, registry, pack)
    print("== 本次：", json.dumps(m, ensure_ascii=False, indent=1))
    os.makedirs(args.out, exist_ok=True)
    json.dump({"meta": meta, "measure": m, "result": result},
              open(os.path.join(args.out, "probe_result.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    if args.ref_cache:
        ref = json.load(open(args.ref_cache, encoding="utf-8"))
        mr = measure(ref["result"], schema, registry, pack)
        print("== 参考：", json.dumps({"meta": ref["meta"], **mr}, ensure_ascii=False, indent=1))
        if isinstance(result, dict) and isinstance(ref["result"], dict):
            a = {(r["head"], r["relation"], r["tail"]) for r in result.get("relations", [])}
            b = {(r["head"], r["relation"], r["tail"]) for r in ref["result"].get("relations", [])}
            print(f"== 三元组重合：{len(a & b)}（本次独有 {len(a-b)}，参考独有 {len(b-a)}）")
            print("   本次独有样例：", list(a - b)[:8])
            print("   参考独有样例：", list(b - a)[:8])


if __name__ == "__main__":
    main()
