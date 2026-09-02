# -*- coding: utf-8 -*-
"""别名归并：抽取结束后，把登记簿里可能指同一实体的条目合并。
规则层：同类型、一方是另一方的后缀/前缀且长度差≤1（如"宝玉"/"贾宝玉"）——仅作候选提示；
LLM 层：一次调用把全部实体（名+别名+身份）交给模型给出合并组（借 kg-gen 的 entity_clusters 思路，
       但不分簇——上下文够大）。只接受同类型、且名字都在登记簿里的合并。
"""
MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {"type": "array", "items": {"type": "object", "properties": {
            "canonical": {"type": "string"},
            "members": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"}},
            "required": ["canonical", "members"]}},
    },
    "required": ["groups"],
}


def program_merge(schema, registry, text, log=print):
    """零 LLM 的两步归并（全部以原文为据）：
    1. 全名补全：已登记人物 A（2–3 字）若"姓+A"在原文出现 ≥2 次且未登记 → 作为 A 的别名（如 宝玉←贾宝玉）；
    2. 重复实体合并：登记簿里同时有 A 与 姓+A（同类型）→ 合并，保留全名为规范名（如 凤姐+王熙凤）。
    姓氏表来自 schema.prescan.surnames。返回 (补全别名数, 合并数)。"""
    surnames = schema.surnames
    if not surnames:
        return 0, 0
    generic = set(schema.generic_names)
    added = merged = 0
    for canon in list(registry.entities):
        ent = registry.entities.get(canon)
        if not ent or ent["type"] != "人物" or not (2 <= len(canon) <= 3) or canon in generic:
            continue
        # 收集全部候选全名：登记簿里已有的 姓+名 实体，或原文出现 ≥2 次的 姓+名。
        # 候选不止一个 → 歧义（如 宝玉 ↔ {贾宝玉, 甄宝玉} 是两个人），整体跳过——这正是
        # 09-01 把宝玉并进甄宝玉的事故根因：同名不同姓不等于同人。
        cands = []
        for sur in surnames:
            full = sur + canon
            if full == canon or canon.startswith(sur):
                continue
            target = registry.resolve(full)
            if target is not None:
                cands.append(("reg", full, target))
            elif text.count(full) >= 2:
                cands.append(("txt", full, None))
        if len(cands) != 1:
            if len(cands) > 1:
                log(f"  程序归并：{canon} 歧义 {[c[1] for c in cands]}，跳过")
            continue
        kind, full, target = cands[0]
        if kind == "txt":
            registry.add_alias(canon, full); added += 1
        elif target != canon and registry.entities[target]["type"] == ent["type"]:
            # 只有当 full 本身就是对方的规范名才合并；经别名间接解析到第三个名字的不动
            if target == full:
                registry.merge(target, canon); merged += 1
                log(f"  程序归并：{canon} → {target}")
    return added, merged


def rule_hints(registry):
    names = list(registry.entities)
    hints = []
    for a in names:
        for b in names:
            if a != b and len(a) < len(b) and (b.endswith(a) or b.startswith(a)) \
                    and registry.entities[a]["type"] == registry.entities[b]["type"]:
                hints.append((a, b))
    return hints


def llm_merge(llm, schema, registry):
    listing = registry.render()
    hints = rule_hints(registry)
    hint_txt = "；".join(f"{a}~{b}" for a, b in hints[:80]) or "（无）"
    prompt = f"""你是《{schema.name}》知识图谱的实体归并员。下面是抽取过程中登记的全部实体（规范名（别名）［类型］：身份）。
有些条目其实是同一实体的不同称呼（例如"宝玉"与"贾宝玉"、"凤姐"与"王熙凤"），请把它们合并。

规则：
- 只合并你有把握是同一实体的；同姓不等于同人；亲属不是同一人；
- 合并组的 canonical 用最正式的全名（优先姓+名）；members 列出要并入的其他条目名（必须是下面列表里的规范名）；
- 不同类型的不能合并；
- 程序提示的疑似对仅供参考：{hint_txt}

实体列表：
{listing}

只输出 JSON。"""
    result, meta = llm.generate(prompt, response_schema=MERGE_SCHEMA, max_output_tokens=20000,
                                tag="[别名归并]", thinking="high")
    applied = []
    if not result:
        return applied, meta
    for g in result.get("groups", []):
        canon = registry.resolve(g.get("canonical", ""))
        if not canon:
            continue
        for m in g.get("members", []):
            mc = registry.resolve(m)
            if not mc or mc == canon:
                continue
            if registry.entities[mc]["type"] != registry.entities[canon]["type"]:
                continue
            registry.merge(canon, mc)
            applied.append((mc, canon, g.get("reason", "")))
    return applied, meta
