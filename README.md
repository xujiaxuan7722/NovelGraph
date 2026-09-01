# kgx — 面向长文本的关系抽取与知识图谱构建框架

Knowledge Graph eXtraction。从长篇小说原文抽取人物关系并构建知识图谱：schema 驱动（换书只换 `schemas/<书>.yaml`）、实体完全开放、证据强制校验、规则推导补全推理型关系、Gemini 单模型。以《红楼梦》验证，迁移验证计划用《三国演义》。

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
| `.env` | `GEMINI_API_KEY`，勿提交 |

## 运行

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m kgx.cli run --schema schemas/hongloumeng.yaml \
    --text data/hongloumeng11.txt --out runs/hlm_v2 \
    [--chapters 1-10] --gold gold.hongloumeng      # 断点续跑、磁盘缓存、配额耗尽自动退出
```

当前阻塞：Gemini 3.7 Flash 免费档每日 20 次请求，全书约需 130 次；出路见 `docs/项目现状与问题.md` §4.5。
