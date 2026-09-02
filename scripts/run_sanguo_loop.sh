#!/bin/bash
cd /home/dwt/kgx
for i in $(seq 1 30); do
  echo "== 外壳第 $i 次启动 $(date +%F_%T)" >> runs/sanguo_full.log
  ./.venv/bin/python -m kgx.cli run --schema schemas/sanguo.yaml --text data/sanguo.txt \
    --out runs/sanguo_full --gold gold.sanguo --no-merge --review-guardrails --review-thinking >> runs/sanguo_full.log 2>&1
  rc=$?
  echo "EXIT=$rc $(date +%F_%T)" >> runs/sanguo_full.log
  [ $rc -eq 0 ] && break
  sleep 300
done
