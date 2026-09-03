# -*- coding: utf-8 -*-
"""全局复核：按实体分组，把候选关系（含证据、登记簿身份）交给模型推理裁决；
可补充它确知的关系（单独标 source=模型知识）。多个小组打包进一次调用以省配额。
"""
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "integer"}, "ok": {"type": "boolean"}, "reason": {"type": "string"},
            "kind": {"type": "string", "description": "否决类别：方向/类型/证据不足/逻辑矛盾/其他；ok=true 时留空"}},
            "required": ["id", "ok"]}},
        "additions": {"type": "array", "items": {"type": "object", "properties": {
            "head": {"type": "string"}, "relation": {"type": "string"}, "tail": {"type": "string"},
            "reason": {"type": "string"}},
            "required": ["head", "relation", "tail"]}},
    },
    "required": ["verdicts", "additions"],
}


_NEG = ("不成立", "并非", "并不是", "不是同一", "无法支持", "不支持", "证据不足", "不能确定", "无法确定",
        "关系错误", "方向错误", "应为", "实际上是", "不应", "错误")


def _reason_contradicts(reason):
    """ok=true 的理由里出现否定性结论词 → 视为自相矛盾"""
    r = (reason or "").replace(" ", "")
    return any(w in r for w in _NEG)


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


_KINDS = ("方向", "类型", "证据不足", "逻辑矛盾", "其他")


def _guess_kind(v):
    k = (v.get("kind") or "").strip()
    if k in _KINDS:
        return k
    r = (v.get("reason") or "")
    if "方向" in r or "应为" in r and "→" in r:
        return "方向"
    if "矛盾" in r or "两个父亲" in r or "并非" in r or "不是同一" in r:
        return "逻辑矛盾"
    if "证据" in r:
        return "证据不足"
    return ""


def _flip(schema, registry, cands, k, v, log):
    """方向类否决：事实保留、头尾翻转后重新过类型检查，并入（或合并到）翻转键"""
    c = cands[k]
    h, r, t = c["tail"], c["relation"], c["head"]
    spec = schema.relations[r]
    ht, tt = registry.entities[h]["type"], registry.entities[t]["type"]
    if spec.symmetric or not schema.type_ok(r, ht, tt):
        return False                      # 对称关系翻转无意义 / 类型不容许 → 视为普通否决（保留原候选交后续判定）
    cands.pop(k)
    nk = schema.canon(h, r, t)
    if nk in cands:
        cands[nk]["votes"] += c["votes"]
        cands[nk]["evidences"] = (cands[nk]["evidences"] + c["evidences"])[:3]
    else:
        c.update({"head": nk[0], "tail": nk[2], "review": "方向翻转：" + v.get("reason", "")})
        cands[nk] = c
    return True


def review(llm, schema, registry, cands, allow_additions=True, log=print, guardrails=False):
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
否决（ok=false）时必须给 kind：方向（事实对但头尾反了）/类型/证据不足/逻辑矛盾/其他。
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
                # 09-03 守卫：补充理由必须同时提及双方（任一名字形式）。三国抽检发现模型知识层 31% 错误
                # 是"理由正确但头尾写错人"（张鲁之父写成张郃），此检查与证据校验同思路，零成本拦截。
                reason_txt = a.get("reason", "") or ""
                if not (any(n in reason_txt for n in registry.names_of(h)) and
                        any(n in reason_txt for n in registry.names_of(t))):
                    stats["additions_unref"] = stats.get("additions_unref", 0) + 1
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
        elif v.get("ok") and _reason_contradicts(v.get("reason", "")):
            # ok=true 但理由里写"不成立/并非/无法支持"——结论与理由打架，不采信也不删除，标存疑
            stats["doubtful"] = stats.get("doubtful", 0) + 1
            cands[k]["review"] = "存疑（理由与结论矛盾）：" + v.get("reason", "")
            cands[k]["doubtful"] = True
        elif v.get("ok"):
            stats["ok"] += 1
            cands[k]["review"] = v.get("reason", "")
        else:
            if guardrails:
                kind = _guess_kind(v)
                votes = cands[k]["votes"]
                if kind == "方向" and _flip(schema, registry, cands, k, v, log):
                    stats["flipped"] = stats.get("flipped", 0) + 1
                    continue
                if votes >= 3 and kind not in ("逻辑矛盾", "类型"):
                    # 高票保护：多处独立证据的候选，只有逻辑矛盾/类型错误能否决
                    stats["highvote_kept"] = stats.get("highvote_kept", 0) + 1
                    cands[k]["review"] = f"高票保留（{votes}票，否决类别={kind or '未给'}）：" + v.get("reason", "")
                    continue
                if not kind:
                    stats["reject_nokind"] = stats.get("reject_nokind", 0) + 1
                    cands[k]["review"] = "否决未给类别（保留）：" + v.get("reason", "")
                    continue
            stats["reject"] += 1
            cands[k]["review_reject"] = v.get("reason", "")
            del cands[k]
    return additions, stats
