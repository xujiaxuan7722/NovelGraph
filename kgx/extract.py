# -*- coding: utf-8 -*-
"""通读抽取：每个打包块 = 当前登记簿 + 原文 → 模型输出（实体增量 + 关系断言 + 证据）。
过程式 prompt（借 kg-gen）：先登记本段人物 → 逐实体找关系并引证据 → 检查遗漏。
gleaning（借 GraphRAG）：追加一轮"还有遗漏，请补充"。
"""
import json
import time

from .chunking import Chapter, Pack, split_sentences

LOW_YIELD_PER_KCHAR = 1.2      # 关系密度低于此值（条/千字）视为异常低产出，换措辞再通读一次
MIN_SPLIT_CHARS = 1500         # 被拦文本短于此不再切半

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "new_entities": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "type": {"type": "string"},
            "aliases": {"type": "array", "items": {"type": "string"}},
            "identity": {"type": "string"}},
            "required": ["name", "type"]}},
        "alias_updates": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "aliases": {"type": "array", "items": {"type": "string"}}},
            "required": ["name", "aliases"]}},
        "identity_updates": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "identity": {"type": "string"}},
            "required": ["name", "identity"]}},
        "relations": {"type": "array", "items": {"type": "object", "properties": {
            "head": {"type": "string"}, "relation": {"type": "string"},
            "tail": {"type": "string"}, "evidence": {"type": "string"},
            "confidence": {"type": "string"}},
            "required": ["head", "relation", "tail", "evidence"]}},
    },
    "required": ["new_entities", "alias_updates", "identity_updates", "relations"],
}


def build_prompt(schema, registry, pack, gleaning_prev=None):
    known = registry.render()
    head = f"""你是《{schema.name}》知识图谱的阅读抽取员。下面给出【已登记实体】和【原文】（{pack.label}），请完成两项工作。

{schema.describe_for_prompt()}

【已登记实体】（规范名（别名）［类型］：身份）
{known if known else "（尚无）"}

工作一：登记簿维护
- new_entities：原文中出现、但登记簿里没有的实体（用文中最正式的称呼作 name；把文中对其的其他称呼放进 aliases；identity 写一句从文本能看出的身份）。
- alias_updates：已登记实体在本段出现的新称呼（如"二奶奶"指王熙凤）。
- identity_updates：本段让你更清楚某已登记实体身份时更新。
- 只登记文中确实出现的名字；泛称（"众人""小丫头"）不登记。

工作二：关系抽取（按步骤做）
1. 逐个实体通读，找出本段文本能直接支持的关系；
2. 每条关系的 head/tail 必须是登记簿里的规范名，或本段 new_entities 里的 name；
3. evidence 必须是本段原文的逐字引用（20–60 字），且这句话里要同时能看出两方；
4. 关系类型只能用上面列出的；方向按定义；拿不准类型或证据不足的不要输出；
5. 不要凭你对这本书的记忆补充文中没有的关系——你只负责"读出来"，不负责"想起来"；
6. 最后检查：有没有出场但一条关系都没给的重要人物？若文中确有关系就补上。
confidence 填 high/medium。

只输出 JSON。"""
    if gleaning_prev is not None:
        head += f"""

【上一轮抽取结果】（共 {len(gleaning_prev)} 条）
{json.dumps(gleaning_prev, ensure_ascii=False)[:6000]}

上一轮很可能有遗漏。请再通读一遍原文，只输出新增的关系（不要重复上面已有的）和新增的登记信息；没有就返回空数组。"""
    return head + f"""

【原文】
{pack.text}"""


def _ground(name, text):
    return bool(name) and name in text


def apply_registry_updates(registry, result, pack_text, chapter_idx):
    """把模型的登记簿增量并入 registry；只接受文本中真实出现的名字。"""
    accepted, rejected = 0, 0
    for e in result.get("new_entities", []) or []:
        name = (e.get("name") or "").strip()
        if not _ground(name, pack_text):
            rejected += 1
            continue
        aliases = [a for a in (e.get("aliases") or []) if _ground(a, pack_text)]
        registry.add(name, e.get("type", ""), aliases, e.get("identity", ""), chapter_idx)
        accepted += 1
    for u in result.get("alias_updates", []) or []:
        canon = registry.resolve(u.get("name", ""))
        if not canon:
            continue
        for a in u.get("aliases") or []:
            if _ground(a, pack_text):
                registry.add_alias(canon, a)
    for u in result.get("identity_updates", []) or []:
        canon = registry.resolve(u.get("name", ""))
        if canon and u.get("identity"):
            registry.entities[canon]["identity"] = u["identity"]
    return accepted, rejected


def _halve(ch):
    """把一章按句边界对半切成两个 Chapter（index 不变，title 加 [上]/[下] 标记）"""
    sents = split_sentences(ch.text)
    half, acc, cut = len(ch.text) // 2, 0, None
    for sent in sents:
        acc = ch.text.find(sent, acc) + len(sent)
        if acc >= half:
            cut = acc; break
    cut = cut or half
    tag = ch.title if ch.title.endswith("]") else ch.title
    return (Chapter(ch.index, tag + "[上]", ch.text[:cut], ch.start),
            Chapter(ch.index, tag + "[下]", ch.text[cut:], ch.start + cut))


def extract_pack(llm, schema, registry, pack, gleaning=True, _depth=0):
    """返回 (relations_list, stats)。relations 元素: dict(head, relation, tail, evidence, confidence, pack, chapters)"""
    first_chapter = pack.chapters[0].index
    chapters = [c.index for c in pack.chapters]
    prompt = build_prompt(schema, registry, pack)
    result, meta = llm.generate(prompt, response_schema=EXTRACT_SCHEMA, tag=f"[抽取 {pack.label}]")
    rels, stats = [], {"calls": 1, "entities_added": 0, "entities_rejected": 0, "parse_error": False}
    if not result and meta.get("finish") == "content_filter":
        # 网关内容审查拦截（确定性失败，重试无用）。多章块 → 拆成单章各抽；单章仍被拦 → 记录并跳过。
        if len(pack.chapters) > 1:
            print(f"  {pack.label} 被内容审查拦截，拆成单章分别抽取", flush=True)
            merged, mstats = [], {"calls": 1, "entities_added": 0, "entities_rejected": 0,
                                  "parse_error": False, "split": True, "filtered_chapters": []}
            for ch in pack.chapters:
                sub = Pack(pack.id, [ch])          # 沿用父块 id，保证 raw/校验按父块文本对齐
                srels, sst = extract_pack(llm, schema, registry, sub, gleaning=gleaning)
                for x in srels:
                    x["chapters"] = chapters
                merged.extend(srels)
                for k in ("calls", "entities_added", "entities_rejected"):
                    mstats[k] += sst.get(k, 0)
                mstats["filtered_chapters"] += sst.get("filtered_chapters", [])
            return merged, mstats
        ch = pack.chapters[0]
        if len(ch.text) >= MIN_SPLIT_CHARS and _depth < 2:
            # 单章被拦：触发的通常只是一段，对半切（句边界）各抽；半段仍被拦才放弃（最多切到 1/4）
            print(f"  {pack.label}{ch.title[-6:] if _depth else ''} 被内容审查拦截，切半分别抽取", flush=True)
            a, b = _halve(ch)
            merged, mstats = [], {"calls": 1, "entities_added": 0, "entities_rejected": 0,
                                  "parse_error": False, "split": True, "filtered_chapters": []}
            for part in (a, b):
                srels, sst = extract_pack(llm, schema, registry, Pack(pack.id, [part]),
                                          gleaning=gleaning, _depth=_depth + 1)
                merged.extend(srels)
                for k in ("calls", "entities_added", "entities_rejected"):
                    mstats[k] += sst.get(k, 0)
                mstats["filtered_chapters"] += sst.get("filtered_chapters", [])
            return merged, mstats
        label = f"{first_chapter}{ch.title[ch.title.find('['):] if '[' in ch.title else ''}"
        print(f"  !! {pack.label}{ch.title[-6:] if _depth else ''} 仍被内容审查拦截，跳过该段", flush=True)
        stats["filtered_chapters"] = [label]
        return rels, stats
    if not result and (meta.get("truncated") or meta.get("parse_error") or meta.get("empty")):
        # 输出截断/解析失败：把输出上限加倍重试一次；仍失败则抛错让 cli 保存进度退出（不能静默记 0 条）
        big = (getattr(llm, "max_output_tokens", 16000) or 16000) * 2
        print(f"  抽取输出截断/解析失败（finish={meta.get('finish')}），max_tokens 加倍至 {big} 重试", flush=True)
        result, meta = llm.generate(prompt, response_schema=EXTRACT_SCHEMA, max_output_tokens=big,
                                    tag=f"[抽取 {pack.label} 重试]")
        stats["calls"] += 1
    if not result:
        raise RuntimeError(f"{pack.label} 抽取结果不可解析（finish={meta.get('finish')}），不记为完成")
    a, r = apply_registry_updates(registry, result, pack.text, first_chapter)
    stats["entities_added"] += a
    stats["entities_rejected"] += r
    for x in result.get("relations", []) or []:
        x = dict(x)
        x["pack"] = pack.id
        x["chapters"] = chapters
        x["round"] = 1
        rels.append(x)

    # 低产出守门：按去重后的三元组算密度，明显低于正常块（2.5–3 条/千字）→ 换措辞再通读一次取并集；
    # 原始/去重 > 3 说明模型陷入重复输出循环（第 99–101 回曾 418 条只有 21 种），同样触发二读。
    distinct = {(x.get("head"), x.get("relation"), x.get("tail")) for x in rels}
    density = len(distinct) / max(len(pack.text), 1) * 1000
    looped = len(rels) > 3 * max(len(distinct), 1) and len(rels) >= 30
    if looped:
        print(f"  {pack.label} 疑似重复输出循环：原始 {len(rels)} 条 / 去重 {len(distinct)} 种，去重后再通读一次", flush=True)
        seen_ev = set(); dedup = []
        for x in rels:
            k = (x.get("head"), x.get("relation"), x.get("tail"), x.get("evidence"))
            if k not in seen_ev:
                seen_ev.add(k); dedup.append(x)
        rels[:] = dedup
    if len(pack.text) >= 3000 and (density < LOW_YIELD_PER_KCHAR or looped):
        if not looped:
            print(f"  {pack.label} 关系密度 {density:.1f} 条/千字 偏低，换措辞再通读一次", flush=True)
        prompt_b = build_prompt(schema, registry, pack) + ("\n\n（第二次通读：上一次读得太粗，请逐段仔细找全所有能引证据的关系；"
                                                            "每条关系只输出一次，不要重复；new_entities 仍只填本段新出现且登记簿没有的。）")
        result_b, meta_b = llm.generate(prompt_b, response_schema=EXTRACT_SCHEMA, tag=f"[抽取 {pack.label} 二读]")
        stats["calls"] += 1
        stats["low_yield_retry"] = True
        if result_b:
            a, r = apply_registry_updates(registry, result_b, pack.text, first_chapter)
            stats["entities_added"] += a
            stats["entities_rejected"] += r
            seen = {(x["head"], x["relation"], x["tail"]) for x in rels}
            for x in result_b.get("relations", []) or []:
                k = (x.get("head"), x.get("relation"), x.get("tail"))
                if k in seen:
                    continue
                seen.add(k)
                x = dict(x); x.update({"pack": pack.id, "chapters": chapters, "round": 1})
                rels.append(x)

    if gleaning:
        prev = [{"head": x["head"], "relation": x["relation"], "tail": x["tail"]} for x in rels]
        prompt2 = build_prompt(schema, registry, pack, gleaning_prev=prev)
        try:
            # 补抽正常输出仅数百到 4k token；上限压到 12k，模型陷入重复输出时能早点被截断
            result2, meta2 = llm.generate(prompt2, response_schema=EXTRACT_SCHEMA, max_output_tokens=12000,
                                          tag=f"[补抽 {pack.label}]")
            if result2 is None and (meta2.get("empty") or meta2.get("parse_error")):
                print(f"  补抽返回空响应，30s 后重试一次", flush=True)
                time.sleep(30)
                result2, meta2 = llm.generate(prompt2, response_schema=EXTRACT_SCHEMA, max_output_tokens=12000,
                                              tag=f"[补抽 {pack.label} 重试]")
                stats["calls"] += 1
        except Exception as e:        # 补抽失败不应拖累第一遍结果
            if type(e).__name__ == "QuotaExhausted":
                raise
            print(f"  补抽失败（{str(e)[:60]}），保留第一遍结果", flush=True)
            result2 = None
            stats["gleaning_failed"] = True
        stats["calls"] += 1
        if result2:
            a, r = apply_registry_updates(registry, result2, pack.text, first_chapter)
            stats["entities_added"] += a
            stats["entities_rejected"] += r
            seen = {(x["head"], x["relation"], x["tail"]) for x in rels}
            for x in result2.get("relations", []) or []:
                k = (x.get("head"), x.get("relation"), x.get("tail"))
                if k in seen:
                    continue
                x = dict(x); x["pack"] = pack.id; x["chapters"] = chapters; x["round"] = 2
                rels.append(x)
    return rels, stats
