#!/bin/bash
cd /home/dwt/kgx
for i in $(seq 1 30); do
  echo "== 消融外壳第 $i 次启动 $(date +%F_%T)" >> runs/ablate_review.log
  ./.venv/bin/python scripts/ablate_review.py >> runs/ablate_review.log 2>&1 && break
  sleep 600
done
