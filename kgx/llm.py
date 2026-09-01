# -*- coding: utf-8 -*-
"""Gemini 客户端：速率控制（RPM/RPD）、指数退避、磁盘缓存、结构化 JSON 输出、用量统计。

免费档约束：每分钟 ~10 次、每天 ~250 次。设计为"每次调用吃得多、调用次数少"，
且所有调用按 (模型+prompt+schema+配置) 哈希缓存——重跑下游不重复付费。
"""
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
PACIFIC = timezone(timedelta(hours=-7))   # 免费档配额按太平洋时间午夜重置（近似）


class QuotaExhausted(RuntimeError):
    """每日配额用尽：上层应保存进度退出，次日续跑"""


def _load_key(env_key="GEMINI_API_KEY"):
    key = os.environ.get(env_key)
    if not key and os.path.exists(".env"):
        for line in open(".env", encoding="utf-8"):
            if line.startswith(env_key + "="):
                key = line.split("=", 1)[1].strip()
    if not key:
        raise SystemExit(f"未找到 {env_key}")
    return key


class Gemini:
    def __init__(self, model="gemini-3.7-flash", cache_dir="runs/cache",
                 rpm=9, rpd=240, thinking="low", max_output_tokens=16000, timeout=300):
        self.model = model
        self.key = _load_key()
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.min_interval = 60.0 / rpm
        self.rpd = rpd
        self.thinking = thinking
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self._last_call = 0.0
        self.stats = {"calls": 0, "cached": 0, "prompt_tokens": 0, "output_tokens": 0,
                      "thought_tokens": 0}
        self._quota_path = os.path.join(cache_dir, "daily_quota.json")

    # ---------- 配额 ----------
    def _today(self):
        return datetime.now(PACIFIC).strftime("%Y-%m-%d")

    def _quota_used(self):
        if os.path.exists(self._quota_path):
            q = json.load(open(self._quota_path))
            if q.get("date") == self._today():
                return q.get("used", 0)
        return 0

    def _quota_add(self):
        json.dump({"date": self._today(), "used": self._quota_used() + 1},
                  open(self._quota_path, "w"))

    def quota_left(self):
        return self.rpd - self._quota_used()

    # ---------- 调用 ----------
    def _cache_key(self, payload):
        raw = json.dumps({"m": self.model, "p": payload}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def generate(self, prompt, response_schema=None, system=None, max_tries=6,
                 max_output_tokens=None, tag="", thinking=None):
        """返回 (parsed_json_or_text, meta)。response_schema 非空时强制 JSON 并解析。"""
        gen = {"maxOutputTokens": max_output_tokens or self.max_output_tokens,
               "thinkingConfig": {"thinkingLevel": thinking or self.thinking},
               "temperature": 0.2}
        if response_schema:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = response_schema
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": gen}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        ck = self._cache_key(payload)
        cpath = os.path.join(self.cache_dir, ck + ".json")
        if os.path.exists(cpath):
            self.stats["cached"] += 1
            rec = json.load(open(cpath, encoding="utf-8"))
            return rec["result"], {**rec["meta"], "cached": True}

        # 重试策略：503（服务端高负载）最多 10 次、退避 15→120s；429（限速/配额）最多 6 次、
        # 退避 30→180s；其余错误最多 max_tries 次。429 连续多次且明确是每日配额 → 抛出让上层续跑。
        n503 = n429 = nother = 0
        while True:
            if self.quota_left() <= 0:
                raise QuotaExhausted(f"今日配额已用尽（{self.rpd}），请明天继续（断点续跑自动接上）")
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            try:
                r = requests.post(f"{BASE}/{self.model}:generateContent",
                                  headers={"x-goog-api-key": self.key,
                                           "Content-Type": "application/json"},
                                  json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                nother += 1
                print(f"  [网络重试{nother}]{tag} {str(e)[:80]}", flush=True)
                if nother >= max_tries:
                    raise RuntimeError(f"网络失败{tag}")
                time.sleep(min(10 * nother, 60))
                continue
            if r.status_code == 200:
                self._quota_add()
                d = r.json()
                break
            msg = r.text[:160].replace("\n", " ")
            if r.status_code == 503:
                n503 += 1
                if n503 > 10:
                    raise RuntimeError(f"服务端持续高负载{tag}")
                pause = min(15 * n503, 120)
            elif r.status_code == 429:
                n429 += 1
                self._quota_add()
                if "PerDay" in r.text or "per day" in r.text.lower():
                    raise QuotaExhausted(f"每日配额已用尽{tag}")
                if n429 > 6:
                    raise RuntimeError(f"持续限速{tag}")
                pause = min(30 * n429, 180)
            else:
                nother += 1
                if nother >= max_tries:
                    raise RuntimeError(f"调用失败{tag} {msg}")
                pause = min(10 * nother, 60)
            print(f"  [HTTP {r.status_code}]{tag} 等待{pause}s后重试… {msg[:100]}", flush=True)
            time.sleep(pause)

        self.stats["calls"] += 1
        usage = d.get("usageMetadata", {})
        self.stats["prompt_tokens"] += usage.get("promptTokenCount", 0)
        self.stats["output_tokens"] += usage.get("candidatesTokenCount", 0)
        self.stats["thought_tokens"] += usage.get("thoughtsTokenCount", 0) or 0
        cand = (d.get("candidates") or [{}])[0]
        parts = cand.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        meta = {"finish": cand.get("finishReason"), "usage": usage, "cached": False}
        if response_schema:
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                # 截断等情况：尽力抠出 JSON 主体
                s, e = text.find("{"), text.rfind("}")
                try:
                    result = json.loads(text[s:e + 1]) if s != -1 else None
                except json.JSONDecodeError:
                    result = None
            if result is None:
                meta["parse_error"] = True
                meta["raw"] = text[:2000]
        else:
            result = text
        json.dump({"result": result, "meta": meta}, open(cpath, "w", encoding="utf-8"),
                  ensure_ascii=False)
        return result, meta

    def report(self):
        s = self.stats
        return (f"LLM调用 {s['calls']} 次（缓存命中 {s['cached']}）｜输入 {s['prompt_tokens']} "
                f"输出 {s['output_tokens']} 思考 {s['thought_tokens']} tokens｜今日剩余配额 {self.quota_left()}")
