# -*- coding: utf-8 -*-
"""实体预扫描（借鉴 AI-Reader-V2 EntityPreScanner）：
Phase 1 纯程序多路候选：jieba 词性 / 字符 n-gram / 说话人归属 / 章节标题 / 命名模式；
Phase 2 一次 LLM 调用分类 + 别名分组（只接受候选里存在的名字，防幻觉加人）。
产物：实体词典 [{name, type, aliases, freq, sources}]，作为登记簿的初始内容（标"待文本确认"）。
"""
import re
from collections import Counter, defaultdict

import jieba.posseg as pseg

STOP = set("""
这个 那个 什么 怎么 如今 今日 一个 两个 咱们 我们 你们 他们 自己 众人 大家 如此 这里 那里 说话 只见 只得
不知 不过 只是 所以 因此 于是 然后 这样 那样 一面 一时 一回 此时 当下 原来 果然 却说 且说 话说 下回分解
老爷 太太 奶奶 姑娘 小姐 丫头 丫鬟 小厮 婆子 媳妇 姐姐 妹妹 哥哥 弟弟 兄弟 姊妹 爷爷 父亲 母亲 儿子 女儿
夫人 先生 公子 大爷 二爷 相公 娘子 老太太 老祖宗 老人家 婶子 姨妈 舅舅 姑妈 嫂子
""".split())

GENERIC_SUFFIX = ("们", "等", "辈")


def _dialogue_names(text, patterns):
    c = Counter()
    for pat in patterns:
        for m in re.finditer(pat, text):
            name = m.group(1)
            if name and name not in STOP and not name.endswith(GENERIC_SUFFIX):
                c[name] += 1
    return c


def _naming_names(text, patterns):
    c = Counter()
    for pat in patterns:
        for m in re.finditer(pat, text):
            name = m.group(1)
            if name and len(name) >= 2 and name not in STOP:
                c[name] += 1
    return c


def _pos_names(text, max_chars=1_200_000):
    c = Counter()
    for w, flag in pseg.cut(text[:max_chars]):
        if flag in ("nr", "ns", "nz", "nt") and 2 <= len(w) <= 4 and w not in STOP:
            c[w] += 1
    return c


FUNC_CHARS = set("了的不也是去来道说着就这那我你他她它们之乎者与和把被在有无没很都又再还只便即因此其")


def _ngram_names(text, known, min_freq=8, cap=120):
    """2-3 字高频片段：只保留不含虚词字、不被已知词覆盖的（人名/地名候选）"""
    c = Counter()
    for n in (2, 3):
        for i in range(len(text) - n + 1):
            g = text[i:i + n]
            if re.fullmatch(r"[一-龥]+", g) and not (set(g) & FUNC_CHARS):
                c[g] += 1
    out = Counter()
    for g, f in c.most_common():
        if f < min_freq:
            break
        if g in STOP or g in known or any(g in k for k in known if len(k) > len(g)):
            continue
        out[g] = f
        if len(out) >= cap:
            break
    return out


def phase1(text, schema, chapter_titles):
    pre = schema.prescan
    min_freq = pre.get("min_freq", 3)
    dial = _dialogue_names(text, pre.get("dialogue_patterns", []))
    naming = _naming_names(text, pre.get("naming_patterns", []))
    pos = _pos_names(text)
    title = Counter()
    for t in chapter_titles:
        for w, flag in pseg.cut(t):
            if flag in ("nr", "ns", "nz") and len(w) >= 2:
                title[w] += 1
    known = set(dial) | set(naming) | set(pos) | set(title)
    ngram = _ngram_names(text, known, min_freq=max(min_freq * 3, 8))

    cands = defaultdict(lambda: {"freq": 0, "sources": set()})
    for src, cnt in (("dialogue", dial), ("naming", naming), ("pos", pos),
                     ("title", title), ("ngram", ngram)):
        for name, f in cnt.items():
            cands[name]["freq"] = max(cands[name]["freq"], f)
            cands[name]["sources"].add(src)
    # 全文真实频次
    for name in cands:
        cands[name]["freq"] = text.count(name)
    keep = {n: v for n, v in cands.items()
            if v["freq"] >= min_freq or "naming" in v["sources"] or "dialogue" in v["sources"]}
    ranked = sorted(keep.items(), key=lambda x: -x[1]["freq"])
    return [{"name": n, "freq": v["freq"], "sources": sorted(v["sources"])} for n, v in ranked[:400]]


def _sample_context(text, name, n=2, width=18):
    out, start = [], 0
    for _ in range(n):
        i = text.find(name, start)
        if i == -1:
            break
        out.append(text[max(0, i - width): i + len(name) + width].replace("\n", " "))
        start = i + len(name)
    return " ｜ ".join(out)


PHASE2_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "type": {"type": "string"},
            "identity": {"type": "string"}}, "required": ["name", "type"]}},
        "alias_groups": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        "rejected": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["entities", "alias_groups", "rejected"],
}


def phase2(llm, text, schema, candidates, top_n=300):
    types = "/".join(schema.entity_types)
    rows = "\n".join(
        f"| {c['name']} | {c['freq']} | {','.join(c['sources'])} | {_sample_context(text, c['name'])} |"
        for c in candidates[:top_n])
    prompt = f"""你是长篇小说实体词典构建专家。下面是程序从《{schema.name}》全文中统计出的候选词（含频次、来源、上下文片段）。

实体类型：{types}
{chr(10).join(f'- {t}：{d}' for t, d in schema.entity_types.items())}

任务：
1. 判断每个候选词是否为实体及其类型；明显不是实体的词（虚词、泛称、动作短语）放入 rejected。
2. 给出别名分组：同一实体的不同称呼放一组（如 [王熙凤, 凤姐, 凤丫头]）。
   - 同姓不等于同人；亲属关系（父子、叔侄）不是别名关系；只有明确是同一人的不同称呼才放一组。
   - 分组成员必须全部来自候选词表。
3. 对人物可附一句话 identity（只写从上下文能看出的身份，不确定就留空）。

候选词表：
| 词 | 频次 | 来源 | 上下文 |
{rows}

只输出 JSON。"""
    result, meta = llm.generate(prompt, response_schema=PHASE2_SCHEMA, max_output_tokens=30000,
                                tag="[预扫描]")
    if not result:
        return [], meta
    cand_names = {c["name"] for c in candidates}
    ents = [e for e in result.get("entities", [])
            if e.get("name") in cand_names and e.get("type") in schema.entity_types]
    groups = [[n for n in g if n in cand_names] for g in result.get("alias_groups", [])]
    groups = [g for g in groups if len(g) >= 2]
    # 合并别名组 → 以频次最高者为规范名
    freq = {c["name"]: c["freq"] for c in candidates}
    etype = {e["name"]: e["type"] for e in ents}
    ident = {e["name"]: e.get("identity", "") for e in ents}
    merged, used = [], set()
    for g in groups:
        g = sorted(set(g), key=lambda n: -freq.get(n, 0))
        canon = g[0]
        merged.append({"name": canon, "type": etype.get(canon) or next((etype[n] for n in g if n in etype), "人物"),
                       "aliases": g[1:], "freq": sum(freq.get(n, 0) for n in g),
                       "identity": ident.get(canon, ""), "sources": ["prescan"]})
        used |= set(g)
    for e in ents:
        if e["name"] not in used:
            merged.append({"name": e["name"], "type": e["type"], "aliases": [],
                           "freq": freq.get(e["name"], 0), "identity": e.get("identity", ""),
                           "sources": ["prescan"]})
    merged.sort(key=lambda x: -x["freq"])
    return merged, meta
