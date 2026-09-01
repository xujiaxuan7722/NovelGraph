# -*- coding: utf-8 -*-
"""kgx 流水线入口。

用法：
  python -m kgx.cli run --schema schemas/hongloumeng.yaml --text data/hongloumeng11.txt \
      --out runs/hlm_v2 [--chapters 1-10] [--provider sensenova|gemini] [--model X] \
      [--review-thinking] [--no-prescan] [--no-gleaning] [--no-review] [--no-additions] \
      [--gold gold.hongloumeng]
所有 LLM 调用有磁盘缓存；中间产物落盘，重跑自动续。默认 provider=sensenova（deepseek-v4-flash，
抽取关思考）；--review-thinking 让复核/归并两类调用开思考。
"""
import argparse
import json
import os
import sys
import time

from .aggregate import aggregate, resolve_functional
from .chunking import pack_chapters, split_chapters
from .evaluate import eval_against_gold, sample_for_human
from .export import export
from .extract import extract_pack
from .infer import infer
from .llm import QuotaExhausted, make_llm
from .prescan import phase1, phase2
from .registry import Registry
from .resolve import llm_merge
from .review import review
from .schema import load_schema
from .validate import Validator


def log(msg):
    print(msg, flush=True)


def parse_range(s, n):
    if not s:
        return 1, n
    a, b = s.split("-") if "-" in s else (s, s)
    return int(a), int(b)


def run(args):
    t0 = time.time()
    os.makedirs(args.out, exist_ok=True)
    schema = load_schema(args.schema)
    text = open(args.text, encoding="utf-8").read()
    llm = make_llm(args.provider, model=args.model, cache_dir=os.path.join(args.out, "cache"),
                   rpm=args.rpm, rpd=args.rpd, thinking_calls=args.review_thinking,
                   tpm=args.tpm, tpm_window=args.tpm_window)
    chapters = split_chapters(text, schema.chunking["chapter_pattern"])
    lo, hi = parse_range(args.chapters, len(chapters))
    sel = [c for c in chapters if lo <= c.index <= hi]
    packs = pack_chapters(sel, schema.chunking.get("chapters_per_call", 3),
                          schema.chunking.get("max_chars_per_call", 24000))
    log(f"== {schema.name}：章节 {lo}-{hi}（{len(sel)} 章，{len(packs)} 个打包块）模型 {args.provider}/{llm.model}"
        f"{'（复核开思考）' if args.review_thinking else ''}")

    # ---- 1. 预扫描 → 登记簿初始化 ----
    registry = Registry()
    reg_path = os.path.join(args.out, "registry.json")
    dict_path = os.path.join(args.out, "prescan_dict.json")
    if os.path.exists(reg_path):
        registry = Registry.load(reg_path)
        log(f"  载入已有登记簿：{len(registry.entities)} 实体")
    elif not args.no_prescan:
        sel_text = "\n".join(c.text for c in sel)
        if os.path.exists(dict_path):
            dic = json.load(open(dict_path, encoding="utf-8"))
        else:
            cands = phase1(sel_text, schema, [c.title for c in sel])
            log(f"  预扫描 Phase1：{len(cands)} 候选")
            dic, meta = phase2(llm, sel_text, schema, cands)
            json.dump(dic, open(dict_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        for e in dic:
            registry.add(e["name"], e["type"], e.get("aliases", []), e.get("identity", ""))
        log(f"  预扫描 Phase2：词典 {len(dic)} 实体 → 登记簿初始化")

    # ---- 2. 通读抽取（断点续跑：raw.jsonl 记录已完成的 pack）----
    raw_path = os.path.join(args.out, "raw.jsonl")
    done = set()
    raw = []
    if os.path.exists(raw_path):
        for line in open(raw_path, encoding="utf-8"):
            rec = json.loads(line)
            done.add(rec["pack"]); raw.extend(rec["relations"])
    with open(raw_path, "a", encoding="utf-8") as f:
        for p in packs:
            if p.id in done:
                continue
            try:
                rels, st = extract_pack(llm, schema, registry, p, gleaning=not args.no_gleaning)
            except QuotaExhausted as e:
                registry.save(reg_path)
                log(f"!! {e}；已保存进度，重新运行同一命令即可续跑"); sys.exit(2)
            except Exception as e:
                registry.save(reg_path)
                log(f"!! {p.label} 抽取失败：{str(e)[:120]}；已保存进度，稍后重跑会续上"); sys.exit(3)
            f.write(json.dumps({"pack": p.id, "chapters": [c.index for c in p.chapters],
                                "relations": rels, "stats": st}, ensure_ascii=False) + "\n")
            f.flush()
            registry.save(reg_path)
            raw.extend(rels)
            if st.get("filtered_chapters"):
                log(f"  !! {p.label} 内容审查拦截的章：{st['filtered_chapters']}（已跳过）")
            log(f"  抽取 {p.label}：关系 {len(rels)} 条（补抽 {sum(1 for r in rels if r.get('round')==2)}"
                f"{'，二读' if st.get('low_yield_retry') else ''}{'，循环拆章' if st.get('looped') else ''}），"
                f"新实体 +{st['entities_added']}（拒 {st['entities_rejected']}）｜登记簿 {len(registry.entities)}｜{llm.report()}")
    log(f"== 抽取完成：原始关系 {len(raw)} 条，登记簿 {len(registry.entities)} 实体")

    # ---- 3. 别名归并（一次 LLM）----
    if not args.no_merge:
        applied, _ = llm_merge(llm, schema, registry)
        registry.save(reg_path)
        log(f"== 别名归并：合并 {len(applied)} 条 " + "；".join(f"{a}→{b}" for a, b, _ in applied[:15]))

    # ---- 4. 程序校验 ----
    pack_text = {p.id: p.text for p in packs}
    validator = Validator(schema, registry)
    validated, dropped = [], []
    for r in raw:
        ok, out, reason = validator.validate(r, pack_text.get(r.get("pack"), ""))
        (validated.append(out) if ok else dropped.append({**r, "reason": reason}))
    from collections import Counter
    reasons = Counter(d["reason"].split(":")[0] for d in dropped)
    log(f"== 校验：通过 {len(validated)}，丢弃 {len(dropped)} {dict(reasons)}")
    json.dump(dropped, open(os.path.join(args.out, "dropped.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- 5. 聚合 ----
    cands = aggregate(schema, validated)
    log(f"== 聚合：{len(cands)} 种候选三元组")

    # ---- 6. 全局复核 ----
    additions, rstats = [], {}
    if not args.no_review:
        additions, rstats = review(llm, schema, registry, cands,
                                   allow_additions=not args.no_additions, log=log)
        log(f"== 复核：{rstats}")
    fdropped = resolve_functional(schema, cands, log=log)

    # ---- 7. 推理补全 ----
    confirmed = [(c["head"], c["relation"], c["tail"]) for c in cands.values()]
    inferred = infer(schema, confirmed)
    log(f"== 推理补全：+{len(inferred)} 条")

    # ---- 8. 导出 + 评估 ----
    summary = {"schema": schema.name, "chapters": [lo, hi], "packs": len(packs),
               "raw": len(raw), "validated": len(validated), "dropped": dict(reasons),
               "candidates": len(cands), "review": rstats, "functional_dropped": len(fdropped),
               "additions": len(additions), "inferred": len(inferred),
               "registry": len(registry.entities), "llm": llm.stats,
               "seconds": round(time.time() - t0)}
    triples = export(args.out, cands, additions, inferred, registry, summary)
    sample_for_human(triples, path=os.path.join(args.out, "human_check.json"))
    if args.gold:
        import importlib
        mod = importlib.import_module(args.gold)
        gold = getattr(mod, "predefined_relations")
        for layers in (("LLM抽取",), ("LLM抽取", "模型知识"), ("LLM抽取", "模型知识", "推导")):
            ev = eval_against_gold(schema, registry, triples, gold, layers)
            log(f"== 评估 {'+'.join(layers)}：P={ev['precision']:.1%} R={ev['recall']:.1%} F1={ev['f1']:.1%} "
                f"(pred {ev['pred']} / gold {ev['gold']} / hit {ev['hit']})")
            json.dump(ev, open(os.path.join(args.out, f"eval_{len(layers)}.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
    log(f"== 完成，输出目录 {args.out}｜{llm.report()}｜耗时 {summary['seconds']}s")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--schema", required=True)
    r.add_argument("--text", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--chapters", default=None, help="如 1-10")
    r.add_argument("--provider", default="sensenova", choices=["sensenova", "gemini"])
    r.add_argument("--model", default=None, help="默认 sensenova=deepseek-v4-flash / gemini=gemini-3.7-flash")
    r.add_argument("--rpm", type=int, default=None, help="固定每分钟调用上限；sensenova 默认 0=不限，靠 429 退避")
    r.add_argument("--rpd", type=int, default=240, help="仅 gemini：每日调用上限")
    r.add_argument("--tpm", type=int, default=40000, help="仅 sensenova：窗口内 token 预算（撞 429 自动下调）")
    r.add_argument("--tpm-window", type=int, default=75, help="仅 sensenova：节流窗口秒数")
    r.add_argument("--review-thinking", action="store_true", help="复核/别名归并调用开思考（默认全关）")
    r.add_argument("--gold", default=None, help="含 predefined_relations 的模块名，如 gold.hongloumeng")
    r.add_argument("--no-prescan", action="store_true")
    r.add_argument("--no-gleaning", action="store_true")
    r.add_argument("--no-merge", action="store_true")
    r.add_argument("--no-review", action="store_true")
    r.add_argument("--no-additions", action="store_true")
    args = ap.parse_args()
    if args.cmd == "run":
        run(args)


if __name__ == "__main__":
    main()
