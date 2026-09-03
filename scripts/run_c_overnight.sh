#!/bin/bash
# C 方案夜跑：pro 抽取+pro 复核，守卫版登记簿，两书串行，自愈重拉
cd /home/dwt/kgx
run_book(){
  for i in $(seq 1 40); do
    echo "== [$1] 外壳第 $i 次 $(date +%F_%T)" >> runs/c_overnight.log
    ./.venv/bin/python -m kgx.cli run --schema "$2" --text "$3" --out "$4" --gold "$5" \
      --model deepseek-v4-pro --no-merge --review-guardrails --review-thinking >> runs/c_overnight.log 2>&1
    rc=$?; echo "== [$1] EXIT=$rc $(date +%F_%T)" >> runs/c_overnight.log
    [ $rc -eq 0 ] && return 0
    sleep 300
  done
}
run_book 红楼 schemas/hongloumeng.yaml data/hongloumeng11.txt runs/hlm_v3_full gold.hongloumeng
run_book 三国 schemas/sanguo.yaml data/sanguo.txt runs/sanguo_v3_full gold.sanguo
echo "== 全部完成 $(date +%F_%T)" >> runs/c_overnight.log
