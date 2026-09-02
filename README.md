# kgx — 面向长文本的关系抽取与知识图谱构建框架

Knowledge Graph eXtraction。从长篇小说原文抽取人物关系并构建知识图谱：schema 驱动（换书只换 `schemas/<书>.yaml`）、实体完全开放、证据强制校验、规则推导补全推理型关系、单模型（默认商汤 Token Plan 网关 deepseek-v4-flash，Gemini 备用）。以《红楼梦》验证，迁移验证计划用《三国演义》。

前身是课程项目 `~/hongloumeng-kg/`（GLM 管线 + 面板，F1 31.9%），本框架是按"可迁移"重做的第二版；演进史、实验数据、决策记录见 `docs/项目现状与问题.md`。

## 目录

| 路径 | 说明 |
|---|---|
| `kgx/` | 框架包：schema / llm / chunking / prescan / registry / extract / validate / resolve / aggregate / review / infer / evaluate / export / cli |
| `schemas/` | 每本书一个 yaml（实体类型、关系定义与约束、易混判据、推导规则） |
| `gold/` | 评估金标准（`hongloumeng.py` 181 条手工三元组） |
| `data/` | 原文 `hongloumeng11.txt` |
| `runs/` | 每次运行一个目录：登记簿、原始抽取、缓存、评估、人工抽检样本 |
| `docs/` | 项目现状与问题、参考项目借鉴笔记 |
| `scripts/` | `probe_pack.py` 单块探针（JSON 合规 / 证据命中率 / 用量，可与另一 run 的缓存对照） |
| `.env` | `SENSENOVA_API_KEY`（默认）、`GEMINI_API_KEY`（备用），勿提交 |

## 运行

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m kgx.cli run --schema schemas/hongloumeng.yaml \
    --text data/hongloumeng11.txt --out runs/hlm_v2 \
    [--chapters 1-10] --gold gold.hongloumeng      # 断点续跑、磁盘缓存
    [--provider sensenova|gemini] [--model deepseek-v4-flash] [--review-thinking]
./.venv/bin/python scripts/probe_pack.py --out runs/probe_ds \
    --ref-cache runs/hlm_v2_c1-3/cache/<hash>.json     # 单块探针 + 对照
```

Provider：默认 `sensenova`（OpenAI 兼容 chat/completions，抽取关思考 `reasoning_effort=none`——默认思考会把
输出上限全烧在 reasoning 上；`--review-thinking` 仅让复核/归并开思考）。网关会返回 `inference tpm exhausted`
429，客户端按 token 节流并退避重试，不退出。`gemini` 免费档每日 20 次仅作备用。
09-02 全书结果（deepseek-v4-flash，实体完全开放、零手写先验）：对 v2 金标准（256 条+评估别名表）**F1 47.0%**（P40.0/R57.0，含推导层；v1 旧尺 45.1），超过 v1 手写先验版的 45.4。漏斗 2,767→1,901→612→303→371。金标准 v2 组成与范围声明见 `gold/hongloumeng.py` 文件头；结果、事故复盘、审计与下一步见 `docs/待办-2026-09-01.md` §6–7。
