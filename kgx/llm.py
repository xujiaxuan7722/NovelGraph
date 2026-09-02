# -*- coding: utf-8 -*-
"""LLM 客户端：两个 provider 同接口 generate(prompt, response_schema, ...) -> (result, meta)。

- Gemini：generateContent，RPM/RPD（太平洋午夜重置）速率控制；免费档每日配额小，仅作备用。
- OpenAICompat：OpenAI 兼容 chat/completions（默认商汤 Token Plan 网关 token.sensenova.cn/v1，
  模型 deepseek-v4-flash）。无日配额，不设固定 RPM，429 休眠退避不退出；默认关思考
  （reasoning_effort=none，否则默认思考会把 max_tokens 全烧在 reasoning 上、正文为空）。
共同点：所有调用按 (模型+payload) 哈希落盘缓存——重跑下游不重复付费；用量统计一致。
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


def _salvage_truncated(text):
    """截断输出抢救：从最后一个完整对象 '}' 处截断，补上 ']}' / '}' 等收尾再解析；成功返回 dict"""
    s = text.find("{")
    if s == -1:
        return None
    body = text[s:]
    for _ in range(40):                       # 逐个回退到上一个 '}'
        e = body.rfind("}")
        if e <= 0:
            return None
        head = body[:e + 1]
        for tail in ("]}", "}", "]}]}", "}]}", ""):
            try:
                r = json.loads(head + tail)
                if isinstance(r, dict):
                    return r
            except json.JSONDecodeError:
                pass
        body = body[:e]
    return None


def _parse_json_text(text, meta):
    """强制 JSON 时的解析：先整体，失败则抠 {…} 主体，再尝试抢救截断输出；仍失败 → meta.parse_error"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        try:
            result = json.loads(text[s:e + 1]) if s != -1 else None
        except json.JSONDecodeError:
            result = None
    if result is None:
        result = _salvage_truncated(text)
        if result is not None:
            meta["salvaged"] = True
    if result is None:
        meta["parse_error"] = True
        meta["raw"] = text[:2000]
    return result


def make_llm(provider, model=None, cache_dir="runs/cache", rpm=None, rpd=240, thinking_calls=False,
             tpm=40000, tpm_window=75, temperature=0.2):
    """cli 用：按 provider 构造客户端；model/rpm 为 None 时用各自默认"""
    if provider == "gemini":
        return Gemini(model=model or "gemini-3.7-flash", cache_dir=cache_dir,
                      rpm=rpm or 9, rpd=rpd)
    if provider == "sensenova":
        return OpenAICompat(model=model or "deepseek-v4-flash", cache_dir=cache_dir,
                            rpm=rpm or 0, thinking_calls=thinking_calls, tpm=tpm, tpm_window=tpm_window,
                            temperature=(None if (model or "").startswith("kimi") else temperature))
    raise SystemExit(f"未知 provider: {provider}")


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
        result = _parse_json_text(text, meta) if response_schema else text
        if not meta.get("parse_error"):          # 截断/解析失败不落缓存，重跑时会重新调用
            json.dump({"result": result, "meta": meta}, open(cpath, "w", encoding="utf-8"),
                      ensure_ascii=False)
        return result, meta

    def report(self):
        s = self.stats
        return (f"LLM调用 {s['calls']} 次（缓存命中 {s['cached']}）｜输入 {s['prompt_tokens']} "
                f"输出 {s['output_tokens']} 思考 {s['thought_tokens']} tokens｜今日剩余配额 {self.quota_left()}")


class OpenAICompat:
    """OpenAI 兼容 chat/completions 客户端（默认商汤 Token Plan 网关）。

    与 Gemini 同接口。差异：
    - JSON：response_format=json_object + 把 JSON Schema 写进 prompt（网关不支持强制 schema）；
    - 思考：默认 reasoning_effort=none；仅当 thinking_calls=True 且调用方 thinking="high"（复核/归并）
      时开启（不传 reasoning_effort，走模型默认），并把 max_tokens 抬到 thinking_max_tokens；
    - 配额：无日限。rpm=0 表示不设固定间隔；按 token 节流（tpm/tpm_window，撞 tpm-429 自动下调预算并落盘
      仅避免连发大请求）；tpm-429 视为共享池瞬时限速，退避 45s×n（上限 180s）重试，最多 40 次≈2h；
      其他 429 退避 60→300s；
    - 用量：cache/usage_log.jsonl 逐次记 (时间, 模型, tokens)，report() 给近 5h 累计（对应网关 5h 窗口）。
    """
    def __init__(self, model="deepseek-v4-flash", cache_dir="runs/cache", rpm=0,
                 base_url="https://token.sensenova.cn/v1", env_key="SENSENOVA_API_KEY",
                 max_output_tokens=32000, thinking_calls=False, thinking_max_tokens=48000,
                 timeout=900, tpm=40000, tpm_window=75, temperature=0.2):
        self.model = model
        self.temperature = temperature      # None 表示不传（kimi-k3 只接受 1）
        self.key = _load_key(env_key)
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.min_interval = 60.0 / rpm if rpm else 0.0
        self.max_output_tokens = max_output_tokens
        self.thinking_calls = thinking_calls
        self.thinking_max_tokens = thinking_max_tokens
        self.timeout = timeout
        # 商汤网关是国内服务，绕开环境里的全局代理（09-02 实测代理断连是"网络失败"的元凶之一）；
        # Gemini 类不受影响，仍走环境代理。
        self.session = requests.Session()
        self.session.trust_env = False
        self._last_call = 0.0
        self.stats = {"calls": 0, "cached": 0, "prompt_tokens": 0, "output_tokens": 0,
                      "thought_tokens": 0, "seconds": 0.0}
        self._usage_path = os.path.join(cache_dir, "usage_log.jsonl")
        # ---- 按 token 节流：窗口内已用 token + 本次预估 ≤ tpm 才放行（避免连发大请求）；
        #      tpm-429 视为瞬时限速，退避重试，不调预算（见 _tpm_hit）----
        self.tpm_window = tpm_window
        self._tpm_path = os.path.join(cache_dir, "tpm_state.json")
        self.tpm = tpm
        self._recent = []       # [(完成时间, tokens)]，含 429 的幻影占用

    def quota_left(self):
        return None

    # ---------- TPM 节流 ----------
    def _window_used(self):
        cutoff = time.time() - self.tpm_window
        self._recent = [(t, n) for t, n in self._recent if t >= cutoff]
        return sum(n for _, n in self._recent)

    def _throttle(self, need, tag):
        """阻塞到窗口内余量够用；返回等待秒数"""
        waited = 0.0
        while True:
            used = self._window_used()
            if used + need <= self.tpm or not self._recent:
                return waited
            oldest = min(t for t, _ in self._recent)
            pause = max(1.0, oldest + self.tpm_window - time.time() + 0.5)
            if waited == 0:
                print(f"  [TPM节流]{tag} 窗口内已用 {used}+预估 {need} > {self.tpm}，等待约 {pause:.0f}s", flush=True)
            time.sleep(min(pause, 30))
            waited += min(pause, 30)

    def _tpm_hit(self, need, n):
        """撞到 tpm-429。09-01 实测：出现时机与本方用量不成函数关系（闲置 90s 后小请求也会撞、
        大请求 3–4 分钟后才过），判断为公测共享池的瞬时容量限制，不能靠下调本方预算解决。
        处理：记一次幻影占用（让节流器至少隔一个窗口），并按 45s×n（上限 180s）退避。
        次数落盘 cache/tpm_state.json 供事后统计。"""
        self._recent.append((time.time(), need))
        st = {"tpm": self.tpm, "window": self.tpm_window, "hits": 0}
        if os.path.exists(self._tpm_path):
            st = json.load(open(self._tpm_path))
        st["hits"] = st.get("hits", 0) + 1
        st["last_hit"] = time.time()
        json.dump(st, open(self._tpm_path, "w"))
        return min(45 * n, 180)

    def _cache_key(self, payload):
        raw = json.dumps({"m": self.model, "p": payload}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _log_usage(self, usage, seconds, tag):
        with open(self._usage_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "model": self.model, "tag": tag,
                                "seconds": round(seconds, 1), **usage}, ensure_ascii=False) + "\n")

    def usage_window(self, hours=5):
        """近 hours 小时的调用次数与 token 合计（网关 5h 滚动窗口对照用）"""
        n = pt = ct = rt = 0
        if os.path.exists(self._usage_path):
            cutoff = time.time() - hours * 3600
            for line in open(self._usage_path, encoding="utf-8"):
                rec = json.loads(line)
                if rec["t"] >= cutoff:
                    n += 1; pt += rec.get("prompt_tokens", 0); ct += rec.get("completion_tokens", 0)
                    rt += rec.get("reasoning_tokens", 0)
        return {"calls": n, "prompt_tokens": pt, "completion_tokens": ct, "reasoning_tokens": rt}

    def generate(self, prompt, response_schema=None, system=None, max_tries=6,
                 max_output_tokens=None, tag="", thinking=None):
        """返回 (parsed_json_or_text, meta)。response_schema 非空时强制 JSON 并解析。"""
        think_on = bool(self.thinking_calls and thinking in ("high", "medium"))
        max_tokens = max_output_tokens or self.max_output_tokens
        if think_on:
            max_tokens = max(max_tokens, self.thinking_max_tokens)
        user = prompt
        if response_schema:
            user += ("\n\n输出必须是且只是一个 JSON 对象，不要 markdown 代码围栏；用紧凑格式（不要缩进和换行）；"
                     "结构须符合以下 JSON Schema：\n" + json.dumps(response_schema, ensure_ascii=False))
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": user}]
        payload = {"model": self.model, "messages": messages,
                   "max_tokens": max_tokens, "stream": False}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if response_schema:
            payload["response_format"] = {"type": "json_object"}
        if not think_on:
            payload["reasoning_effort"] = "none"

        ck = self._cache_key(payload)
        cpath = os.path.join(self.cache_dir, ck + ".json")
        if os.path.exists(cpath):
            self.stats["cached"] += 1
            rec = json.load(open(cpath, encoding="utf-8"))
            return rec["result"], {**rec["meta"], "cached": True}

        # 预估本次占用：中文约 0.8 token/字 + 预期输出（按 max_tokens 六成）
        need = int(len(user) * 0.8 + (len(system) * 0.8 if system else 0) + max_tokens * 0.6)
        n429 = n5xx = nother = 0
        while True:
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._throttle(need, tag)
            self._last_call = time.time()
            t0 = time.time()
            try:
                r = self.session.post(f"{self.base_url}/chat/completions",
                                  headers={"Authorization": f"Bearer {self.key}",
                                           "Content-Type": "application/json"},
                                  json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                nother += 1
                print(f"  [网络重试{nother}]{tag} {str(e)[:80]}", flush=True)
                if nother >= max_tries:
                    raise RuntimeError(f"网络失败{tag}")
                time.sleep(min(15 * nother, 90))
                continue
            elapsed = time.time() - t0
            if r.status_code == 200:
                d = r.json()
                break
            msg = r.text[:160].replace("\n", " ")
            if r.status_code == 429:
                n429 += 1
                if n429 > 40:
                    raise RuntimeError(f"持续限速{tag}（已连续 40 次 429，约 2 小时）")
                if "tpm" in r.text.lower():
                    pause = self._tpm_hit(need, n429)
                    print(f"  [HTTP 429 tpm]{tag} 第{n429}次，等待{pause}s后重试", flush=True)
                    time.sleep(pause)
                    continue
                pause = min(60 * n429, 300)
            elif r.status_code >= 500:
                n5xx += 1
                if n5xx > 10:
                    raise RuntimeError(f"服务端持续错误{tag}")
                pause = min(15 * n5xx, 120)
            elif r.status_code in (400, 401, 403, 404):     # 请求本身有问题，重试无意义
                raise RuntimeError(f"调用失败{tag} HTTP {r.status_code} {msg}")
            else:
                nother += 1
                if nother >= max_tries:
                    raise RuntimeError(f"调用失败{tag} HTTP {r.status_code} {msg}")
                pause = min(10 * nother, 60)
            print(f"  [HTTP {r.status_code}]{tag} 等待{pause}s后重试… {msg[:100]}", flush=True)
            time.sleep(pause)

        usage_raw = d.get("usage") or {}
        details = usage_raw.get("completion_tokens_details") or {}
        usage = {"prompt_tokens": usage_raw.get("prompt_tokens", 0),
                 "completion_tokens": usage_raw.get("completion_tokens", 0),
                 "reasoning_tokens": details.get("reasoning_tokens", 0) or 0,
                 "total_tokens": usage_raw.get("total_tokens", 0)}
        self.stats["calls"] += 1
        self.stats["prompt_tokens"] += usage["prompt_tokens"]
        self.stats["output_tokens"] += usage["completion_tokens"]
        self.stats["thought_tokens"] += usage["reasoning_tokens"]
        self.stats["seconds"] += elapsed
        self._recent.append((time.time(), usage["prompt_tokens"] + usage["completion_tokens"]))
        self._log_usage(usage, elapsed, tag)
        choice = (d.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        meta = {"finish": choice.get("finish_reason"), "usage": usage, "cached": False,
                "model": d.get("model"), "seconds": round(elapsed, 1), "thinking": think_on}
        if not text.strip():
            meta["empty"] = True
        result = _parse_json_text(text, meta) if response_schema else text
        if meta.get("finish") == "length":
            meta["truncated"] = True
        if not (meta.get("parse_error") or meta.get("empty")):   # 截断/解析失败/空输出不落缓存
            json.dump({"result": result, "meta": meta}, open(cpath, "w", encoding="utf-8"),
                      ensure_ascii=False)
        return result, meta

    def report(self):
        s, w = self.stats, self.usage_window()
        return (f"LLM调用 {s['calls']} 次（缓存命中 {s['cached']}）｜输入 {s['prompt_tokens']} "
                f"输出 {s['output_tokens']} 思考 {s['thought_tokens']} tokens｜近5h {w['calls']} 次 "
                f"{w['prompt_tokens'] + w['completion_tokens']} tokens｜TPM预算 {self.tpm}")
