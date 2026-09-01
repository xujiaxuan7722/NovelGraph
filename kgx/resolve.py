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
