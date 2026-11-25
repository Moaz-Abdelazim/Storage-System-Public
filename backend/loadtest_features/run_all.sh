#!/bin/bash
# تشغيل Load Test كامل ومتسلسل (بيستخدم نفس المتغيرات): seed -> profile -> run_load لكل حجم بيانات، وبعدها benchmark استيراد Excel.
# الاستخدام: source loadenv.sh  ثم  bash loadtest_features/run_all.sh   (من فولدر backend). النتايج في loadtest_features/results/
set -u
cd "$(dirname "$0")/.."
log() { echo "[$(date +%H:%M:%S)] $*"; }

# SPECS="عدد:اسم:مستويات:ثواني ..." (الافتراضي: الثلاثة أحجام)
SPECS=${SPECS:-"1000:1k:1,10,25:25 5000:5k:1,5,10,25,50:30 10000:10k:10,25,50:30"}
for spec in $SPECS; do
  IFS=: read n label levels dur <<< "$spec"
  log "=== seed $n products ($label)"
  python -m loadtest_features.seed --products "$n" --reset 2>&1 | grep --line-buffered -v "INFO\|SECRET\|starlette" | tail -2
  log "=== profile queries ($label)"
  python -m loadtest_features.profile_queries "$label" 2>&1 | grep --line-buffered -v "INFO\|SECRET\|Warning\|starlette\|^$" | tail -14
  log "=== load $label levels=$levels"
  python -m loadtest_features.run_load --label "$label" --scenarios A,B,C,E --levels "$levels" --duration "$dur" 2>&1 | grep --line-buffered -v "INFO\|SECRET\|Warning\|starlette"
done
if [ "${SKIP_IMPORT:-0}" != "1" ]; then
  log "=== excel import benchmark"
  python -m loadtest_features.bench_import --rows 500 2>&1 | grep --line-buffered -v "INFO\|SECRET\|Warning\|starlette"
fi
log "=== DONE"
