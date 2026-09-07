# NovelGraph

**面向长篇小说的人物关系抽取与知识图谱构建框架。** Schema 驱动、证据强制、零手写人物先验——换一份 YAML 即迁移一本新书。以《红楼梦》《三国演义》全书验证。

代码包名沿用 `kgx`（Knowledge Graph eXtraction）；前身为课程项目 [hongloumeng-kg](https://github.com/xujiaxuan7722/hongloumeng-kg)（手写实体表版，F1 45.4）。

## 效果

| | 红楼梦（120回） | 三国演义（120回） |
|---|---|---|
| 图谱 | 565 关系 / 1,016 实体 | 1,495 关系 / 1,920 实体 |
| 召回（对人工金标准） | 76.3% | **83.3%**（君臣 83%、结义 3/3） |
| **真实精度**（随机抽检，Wilson CI） | **96%**（48/50） | **95.9%**（47/49） |
| 迁移成本 | — | 仅新增 2 个数据文件，`kgx/` 包零修改 |

零手写人物先验，两项指标均反超手写先验基线（F1 45.4）。

![红楼梦人物关系面板](docs/images/panel-hongloumeng.png)
![三国演义人物关系面板](docs/images/panel-sanguo.png)

## 方法

九步管线，**领域知识只从 `schemas/<书>.yaml` 单点进入**：

```
切章打包 → 实体预扫描 → 滚动登记簿(别名守卫) → 整块抽取+补抽 → 程序校验(证据逐字/闭集/类型/方向翻转)
→ 程序化归并 → 聚合计票 → LLM分组复核(护栏+思考) → 规则推导(带推导链+置信度) → 图谱(三层来源标注)
```

**防幻觉三板斧**：关系闭集约束、证据强制（每条关系逐字引用原文并程序验证）、投票聚合。
**三层可解释来源**：文本抽取（带证据句）/ 模型知识（复核补充）/ 推导（带前提链）——分层精度实测 96% / 100% / 94%。

## 运行

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
# 全书抽取+评估（provider 默认商汤 OpenAI 兼容网关，见 kgx/llm.py）
./.venv/bin/python -m kgx.cli run --schema schemas/hongloumeng.yaml --text data/hongloumeng11.txt \
    --out runs/hlm --gold gold.hongloumeng --review-guardrails --review-thinking
# 关系图谱面板
./.venv/bin/python panel.py    # → http://127.0.0.1:8020
```

## 文档

- `docs/项目总结-2026-09-02.md`：方法 / 消融 / 含金量 / 局限（面试用）
- `docs/待办-2026-09-01.md`：全流程实录（模型选型、13 类问题的现象→根因→方案→效果、消融数据、双书终表）
- `docs/参考项目借鉴笔记.md`：kg-gen / GraphRAG / AI-Reader-V2 / OneKE 借鉴清单

## 布局

| 路径 | 说明 |
|---|---|
| `kgx/` | 框架包（14 模块：schema/llm/chunking/prescan/registry/extract/validate/resolve/aggregate/review/infer/evaluate/export/cli） |
| `schemas/` | 每本书一个 YAML（实体类型、关系闭集、易混判据、推导规则） |
| `gold/` | 评估金标准 + 评估别名表（仅供 evaluate，绝不进管线） |
| `runs/<书>_v3_full/` | 最终图谱、评估、抽检、审校记录 |
| `panel.py` + `static/` | 零依赖关系图谱面板 |
| `scripts/` | 单块探针 / 消融 / 字号归并 |
