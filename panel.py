# -*- coding: utf-8 -*-
"""NovelGraph 面板（v2 专用，独立于 v1 项目）：零第三方依赖的只读服务。
用法：./.venv/bin/python panel.py [端口=8020]
  /                 前端页面（static/index.html）
  /api/runs         可用的 run 列表（扫描 runs/*/triples.json）
  /api/run?name=X   该 run 的 triples + registry + summary + eval
"""
import json
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))


def list_runs():
    out = []
    for d in sorted(os.listdir(os.path.join(ROOT, "runs"))):
        p = os.path.join(ROOT, "runs", d)
        if not os.path.exists(os.path.join(p, "triples.json")):
            continue
        book = ""
        sp = os.path.join(p, "summary.json")
        if os.path.exists(sp):
            book = json.load(open(sp, encoding="utf-8")).get("schema", "")
        out.append({"name": d, "book": book, "primary": d.endswith("full")})
    # 每本书只保留一个主 run（*full 优先），测试 run 仍可通过 ?run= 直达
    books = {}
    for r in out:                       # out 已按目录名排序：同书多个主 run 时后者(更新版本)覆盖
        cur = books.get(r["book"])
        if cur is None or r["primary"]:
            books[r["book"]] = r
    return list(books.values())


def load_run(name):
    if "/" in name or ".." in name:
        return None
    p = os.path.join(ROOT, "runs", name)
    if not os.path.exists(os.path.join(p, "triples.json")):
        return None
    def j(fn, default):
        fp = os.path.join(p, fn)
        return json.load(open(fp, encoding="utf-8")) if os.path.exists(fp) else default
    summary = j("summary.json", {})
    # 展示层别名表：借用 gold/<书>.py 的评估别名（如 贾宝玉→宝玉），补齐登记簿缺失的常用全名
    gold_aliases = {}
    mod_name = {"红楼梦": "gold.hongloumeng", "三国演义": "gold.sanguo"}.get(summary.get("schema", ""))
    if mod_name:
        try:
            import importlib, sys
            sys.path.insert(0, ROOT)
            gold_aliases = getattr(importlib.import_module(mod_name), "aliases", {})
        except Exception:
            gold_aliases = {}
    return {"triples": j("triples.json", []), "registry": j("registry.json", {}),
            "summary": summary, "eval": j("eval_3.json", None), "gold_aliases": gold_aliases}


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(ROOT, "static"), **k)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/runs":
            return self._json(list_runs())
        if u.path == "/api/run":
            name = parse_qs(u.query).get("name", [""])[0]
            data = load_run(name)
            return self._json(data if data else {"error": "run 不存在"}, 200 if data else 404)
        if u.path == "/":
            self.path = "/index.html"
        return super().do_GET()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8020
    print(f"NovelGraph 面板: http://127.0.0.1:{port}")
    HTTPServer(("127.0.0.1", port), H).serve_forever()
