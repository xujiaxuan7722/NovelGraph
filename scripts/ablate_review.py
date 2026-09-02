# -*- coding: utf-8 -*-
"""复核消融：同一候选集(全书 612)上比较
  B = 护栏(guardrails) + 关思考
  D = 护栏 + 开思考(--review-thinking 等效)
A(基线=无护栏关思考) 直接引用 09-02 正式跑的结果，不重跑。
每个变体：重建 cands → review → functional → infer(文本层前提) → eval(v2 金标准)。
"""
import json, importlib, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kgx.schema import load_schema
from kgx.registry import Registry
from kgx.validate import Validator
from kgx.aggregate import aggregate, resolve_functional
from kgx.review import review
from kgx.infer import infer
from kgx.evaluate import eval_against_gold
from kgx.llm import make_llm
from kgx.chunking import split_chapters, pack_chapters

schema=load_schema("schemas/hongloumeng.yaml")
text=open("data/hongloumeng11.txt",encoding="utf-8").read()
packs=pack_chapters(split_chapters(text, schema.chunking["chapter_pattern"]),3,24000); ptext={p.id:p.text for p in packs}
reg=Registry.load("runs/hlm_v2_full/registry.json")
gm=importlib.import_module("gold.hongloumeng"); gold,al=gm.predefined_relations,gm.aliases
raw=[r for l in open("runs/hlm_v2_full/raw.jsonl",encoding="utf-8") for r in json.loads(l)["relations"]]
v=Validator(schema,reg); ok=[]
for r in raw:
    o,out,_=v.validate(r,ptext.get(r["pack"],""))
    if o: ok.append(out)
results={}
for name,thinking in (("B_护栏",False),("D_护栏+思考",True)):
    cands=aggregate(schema,ok)
    llm=make_llm("sensenova",cache_dir="runs/hlm_v2_full/cache",thinking_calls=thinking)
    adds,rstats=review(llm,schema,reg,cands,allow_additions=True,log=print,guardrails=True)
    resolve_functional(schema,cands,log=lambda *a:None)
    trip=[{"head":c["head"],"relation":c["relation"],"tail":c["tail"],"source":"LLM抽取"} for c in cands.values()]
    trip+=[{**a,"source":"模型知识"} for a in adds]
    inf=infer(schema,[(c["head"],c["relation"],c["tail"]) for c in cands.values()])
    trip+=[{"head":i["head"],"relation":i["relation"],"tail":i["tail"],"source":"推导"} for i in inf]
    row={"review":rstats,"triples":len(trip)}
    for lname,layers in (("text",("LLM抽取",)),("full",("LLM抽取","模型知识","推导"))):
        e=eval_against_gold(schema,reg,trip,gold,layers,gold_aliases=al)
        row[lname]={k:e[k] for k in("pred","hit","gold","precision","recall","f1")}
    results[name]=row
    print(f"== {name}: {json.dumps(row,ensure_ascii=False)}", flush=True)
    json.dump(trip, open(f"runs/ablate_{name.split('_')[0]}_triples.json","w",encoding="utf-8"), ensure_ascii=False)
json.dump(results, open("runs/ablate_review_results.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)
print("== 消融完成")
