#!/bin/bash
# 09-01：重跑 1–3 / 4–6（复核重算）→ 全书。断点续跑：中断后重新执行本脚本即可。
cd /home/dwt/kgx
PY=./.venv/bin/python
for r in "1-3" "4-6"; do
  $PY -m kgx.cli run --schema schemas/hongloumeng.yaml --text data/hongloumeng11.txt \
     --chapters $r --out runs/hlm_v2_c${r}_ds --gold gold.hongloumeng > runs/hlm_v2_c${r}_ds.log 2>&1
  echo "EXIT=$?" >> runs/hlm_v2_c${r}_ds.log
done
echo "== 全书开始 $(date +%F_%T)" >> runs/hlm_v2_full.log
$PY -m kgx.cli run --schema schemas/hongloumeng.yaml --text data/hongloumeng11.txt \
   --out runs/hlm_v2_full --gold gold.hongloumeng >> runs/hlm_v2_full.log 2>&1
echo "EXIT=$? $(date +%F_%T)" >> runs/hlm_v2_full.log
