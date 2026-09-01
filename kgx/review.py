# -*- coding: utf-8 -*-
"""全局复核：按实体分组，把候选关系（含证据、登记簿身份）交给模型推理裁决；
可补充它确知的关系（单独标 source=模型知识）。多个小组打包进一次调用以省配额。
"""
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "integer"}, "ok": {"type": "boolean"}, "reason": {"type": "string"}},
            "required": ["id", "ok"]}},
        "additions": {"type": "array", "items": {"type": "object", "properties": {
            "head": {"type": "string"}, "relation": {"type": "string"}, "tail": {"type": "string"},
            "reason": {"type": "string"}},
            "required": ["head", "relation", "tail"]}},
    },
    "required": ["verdicts", "additions"],
}


def _group_by_entity(cands):
    groups = {}
    for key, c in cands.items():
        groups.setdefault(c["head"], []).append(key)
        if c["tail"] not in groups or key not in groups.get(c["tail"], []):
            groups.setdefault(c["tail"], []).append(key)
    return groups


def _batches(groups, max_items=45):
    """把实体组打包：每批候选总数 ≤ max_items（同一候选可能在两组重复，去重）"""
    batch, seen, batches = [], set(), []
    for ent, keys in sorted(groups.items(), key=lambda x: -len(x[1])):
        fresh = [k for k in keys if k not in seen]
        if not fresh:
            continue
        if batch and len(seen) + len(fresh) > max_items and len(fresh) <= max_items:
            batches.append(batch); batch, seen = [], set()
        batch.append((ent, fresh)); seen |= set(fresh)
    if batch:
        batches.append(batch)
    return batches


def review(llm, schema, registry, cands, allow_additions=True, log=print):
    """修改 cands（就地删除被否决的），返回 (additions_list, stats)"""
    groups = _group_by_entity(cands)
    batches = _batches(groups)
    log(f"  复核：{len(cands)} 候选，{len(groups)} 实体组，打包 {len(batches)} 次调用")
    all_keys = list(cands)
    id_of = {k: i + 1 for i, k in enumerate(all_keys)}
    verdicts, additions = {}, []
    stats = {"calls": 0, "ok": 0, "reject": 0, "unjudged": 0, "additions": 0, "additions_invalid": 0}

    for bi, batch in enumerate(batches, 1):
        lines, ents = [], []
        for ent, keys in batch:
            e = registry.entities.get(ent, {})
            ents.append(f"- {ent}［{e.get('type','')}］：{e.get('identity','')}")
            for k in keys:
                c = cands[k]
                lines.append(f"{id_of[k]}. {c['head']} —{c['relation']}→ {c['tail']}　"
                             f"（{c['votes']}处证据，如「{c['evidence']}」）")
        prompt = f"""你是《{schema.name}》知识图谱的复核员。下面是程序从原文抽取的候选关系（附票数与一条证据），以及相关实体的登记信息。

{schema.describe_for_prompt()}

【相关实体】
{chr(10).join(ents)}

【候选关系】
{chr(10).join(lines)}

请逐条裁决（先推理再下结论）：证据是否真的支持该关系、方向是否正确、与其他候选有无矛盾（一人不会有两个父亲；辈分、身份是否讲得通）、是否把到访当居住/把服侍当主仆。ok=true 表示成立。
{"另外，若你确知这些实体之间还有候选里没有的重要关系，可在 additions 中补充（head/tail 用上面出现过的规范名，关系类型限上面所列）。不确定的不要写。" if allow_additions else "不要补充新关系。"}

只输出 JSON。"""
        result, meta = llm.generate(prompt, response_schema=REVIEW_SCHEMA, max_output_tokens=30000,
                                    tag=f"[复核 {bi}/{len(batches)}]", thinking="high")
        stats["calls"] += 1
        if not result:
            log(f"  复核批次{bi}解析失败，该批保守保留")
            continue
        for v in result.get("verdicts", []):
            verdicts[v.get("id")] = v
        if allow_additions:
            for a in result.get("additions", []):
                h, t = registry.resolve(a.get("head", "")), registry.resolve(a.get("tail", ""))
                r = a.get("relation", "")
                if not h or not t or h == t or r not in schema.relations or \
                        not schema.type_ok(r, registry.entities[h]["type"], registry.entities[t]["type"]):
                    stats["additions_invalid"] += 1
                    continue
                key = schema.canon(h, r, t)
                if key in cands:
                    continue
                additions.append({"head": key[0], "relation": key[1], "tail": key[2],
                                  "source": "模型知识", "reason": a.get("reason", ""),
                                  "votes": 0, "evidence": None, "evidence_src": None})
                stats["additions"] += 1

    for k in all_keys:
        v = verdicts.get(id_of[k])
        if v is None:
            stats["unjudged"] += 1
            cands[k]["review"] = "未裁决（保留）"
        elif v.get("ok"):
            stats["ok"] += 1
            cands[k]["review"] = v.get("reason", "")
        else:
            stats["reject"] += 1
            cands[k]["review_reject"] = v.get("reason", "")
            del cands[k]
    return additions, stats
