# -*- coding: utf-8 -*-
"""字号/别称归并：只渲染高频人物给 LLM 判定同一人，输出 merge_map.json（{别名:正名}）。
不改 registry、不进抽取管线；由 cli 在复核之后作为"实体规范化"后处理应用（复核缓存不失效）。
用法：./.venv/bin/python scripts/merge_names.py --out runs/sanguo_v3_full --schema schemas/sanguo.yaml --min-mentions 6
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kgx.schema import load_schema
from kgx.registry import Registry
from kgx.resolve import MERGE_SCHEMA
from kgx.llm import make_llm

ap=argparse.ArgumentParser()
ap.add_argument("--out",required=True); ap.add_argument("--schema",required=True)
ap.add_argument("--model",default="deepseek-v4-pro"); ap.add_argument("--min-mentions",type=int,default=6)
a=ap.parse_args()
schema=load_schema(a.schema); reg=Registry.load(os.path.join(a.out,"registry.json"))
ppl=sorted([(c,e) for c,e in reg.entities.items() if e["type"]=="人物" and e["mentions"]>=a.min_mentions],
           key=lambda x:-x[1]["mentions"])
listing="\n".join(f"- {c}（{'｜'.join(sorted(e['aliases'])[:6])}）：{e['identity'][:40]}" for c,e in ppl)
print(f"渲染高频人物 {len(ppl)} 个")
prompt=f"""你是《{schema.name}》的人物同一性判定专家。下面是从原著抽取的主要人物条目（规范名（别名）：身份）。
其中有些是同一个人的不同称呼——常见于**表字、别号、尊称**（例：刘备=玄德、诸葛亮=孔明、曹操=孟德、关羽=关公）。
请找出这些指同一人的条目并合并。

严格规则：
- 只合并你**确信是同一个人**的（表字/别号/小名/尊称）；同姓不同名是不同人（张辽≠张郃）；亲属不是同一人（孙权≠孙策）；父子兄弟绝不合并。
- canonical 用最正式的姓+名全名（如"刘备"而非"玄德"）；members 列出并入的其他条目名，必须来自下面列表。
- 拿不准就不合并。只输出 JSON。

人物列表：
{listing}
"""
llm=make_llm("sensenova",model=a.model,cache_dir=os.path.join(a.out,"cache"))
res,meta=llm.generate(prompt,response_schema=MERGE_SCHEMA,max_output_tokens=8000,tag="[字号归并]")
names={c for c,_ in ppl}
mp={}
for g in (res or {}).get("groups",[]):
    canon=g.get("canonical","")
    if canon not in reg.entities or reg.entities[canon]["type"]!="人物": continue
    for m in g.get("members",[]):
        if m in reg.entities and m!=canon and reg.entities[m]["type"]=="人物":
            mp[m]=canon
json.dump(mp,open(os.path.join(a.out,"merge_map.json"),"w",encoding="utf-8"),ensure_ascii=False,indent=1)
print(f"merge_map {len(mp)} 条:",{k:v for k,v in list(mp.items())[:20]})
