# -*- coding: utf-8 -*-
"""schema.yaml 加载与访问。框架里所有领域知识只从这里进入。"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml


@dataclass
class RelationSpec:
    name: str
    domain: str
    range: str
    directed: bool = True
    symmetric: bool = False
    functional_on: Optional[str] = None     # "tail" 表示一个 tail 至多一个 head（如一父）
    definition: str = ""


@dataclass
class InferenceRule:
    name: str
    conditions: List[List[str]]     # [[h, rel_alternatives, t], ...]，以 "?x" 表示变量
    conclusion: List[str]
    unless: Optional[List[str]] = None


@dataclass
class Schema:
    name: str
    language: str
    entity_types: Dict[str, str]
    relations: Dict[str, RelationSpec]
    disambiguation: List[str]
    inference: List[InferenceRule]
    chunking: dict
    prescan: dict
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def relation_names(self) -> List[str]:
        return list(self.relations)

    @property
    def symmetric_relations(self):
        return {r for r, s in self.relations.items() if s.symmetric}

    def type_ok(self, rel: str, head_type: str, tail_type: str) -> bool:
        s = self.relations.get(rel)
        return bool(s) and s.domain == head_type and s.range == tail_type

    def canon(self, head: str, rel: str, tail: str):
        """对称关系统一方向，便于去重计票"""
        if rel in self.symmetric_relations and head > tail:
            head, tail = tail, head
        return head, rel, tail

    def describe_for_prompt(self) -> str:
        """生成写进 prompt 的 schema 说明（类型、关系定义、方向、易混判据）"""
        lines = ["【实体类型】"]
        for t, d in self.entity_types.items():
            lines.append(f"- {t}：{d}")
        lines.append("【关系类型（只允许这些）】")
        for r, s in self.relations.items():
            direction = "无方向" if s.symmetric else f"{s.domain}→{s.range}，有方向"
            lines.append(f"- {r}：{s.definition}（{direction}）")
        if self.disambiguation:
            lines.append("【易混判据】")
            for d in self.disambiguation:
                lines.append(f"- {d}")
        return "\n".join(lines)


def load_schema(path: str) -> Schema:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    relations = {}
    for name, spec in raw.get("relations", {}).items():
        relations[name] = RelationSpec(
            name=name, domain=spec["domain"], range=spec["range"],
            directed=spec.get("directed", not spec.get("symmetric", False)),
            symmetric=spec.get("symmetric", False),
            functional_on=spec.get("functional_on"),
            definition=spec.get("definition", ""),
        )
    inference = [InferenceRule(name=r["name"], conditions=r["if"],
                               conclusion=r["then"], unless=r.get("unless"))
                 for r in raw.get("inference", [])]
    return Schema(
        name=raw.get("name", ""), language=raw.get("language", "zh"),
        entity_types=raw.get("entity_types", {}), relations=relations,
        disambiguation=raw.get("disambiguation", []), inference=inference,
        chunking=raw.get("chunking", {}), prescan=raw.get("prescan", {}), raw=raw,
    )
