#!/bin/bash
# 全书跑外壳：非 0 退出（限速抛错/网络等）时等 2 分钟续跑，最多重拉 20 次；断点续跑由 cli 保证。
cd /home/dwt/kgx
LOG=runs/hlm_v2_full.log
for i in $(seq 1 20); do
  echo "== 外壳第 $i 次启动 $(date +%F_%T)" >> $LOG
  ./.venv/bin/python -m kgx.cli run --schema schemas/hongloumeng.yaml --text data/hongloumeng11.txt \
     --out runs/hlm_v2_full --gold gold.hongloumeng >> $LOG 2>&1
  rc=$?
  echo "EXIT=$rc $(date +%F_%T)" >> $LOG
  [ $rc -eq 0 ] && break
  sleep 120
done
