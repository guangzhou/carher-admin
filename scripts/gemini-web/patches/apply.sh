#!/usr/bin/env bash
# Rebuild the two Gemini web gateways on 198 from upstream + carher patches. Run ON 198 as a sudoer.
#
#   bash apply.sh fastapi     # Nativu5/Gemini-FastAPI @1731445 + p1,p2,p3,p3.1,p3.2 -> deploy/gemini-web
#   bash apply.sh media       # ntthanh2603/gemini-web-to-api @681ca80 + bounds guard -> deploy/gemini-media
#
# Upgrading upstream: change the commit, re-run; every patch asserts its anchor and refuses to apply twice,
# so a drifted upstream fails loudly instead of half-applying. Then rerun probes/cc_regress.py.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TS=$(date +%Y%m%d)

case "${1:-}" in
  fastapi)
    B=/root/gemini-web-build; REV=1731445
    sudo rm -rf $B/src && sudo git clone -q https://github.com/Nativu5/Gemini-FastAPI $B/src && sudo git -C $B/src checkout -q $REV
    # Patch scripts edit fixed paths under /root/gemini-web-build/src/app.
    for p in p1_namedmodel p2_pool_failover p3_tool_nudge p3_1_as_dict p3_2_regex_two_retries; do
      sudo python3 "$HERE/$p.py"
    done
    sudo python3 -m py_compile $B/src/app/server/chat.py $B/src/app/services/pool.py
    T=127.0.0.1:5000/gemini-fastapi:$TS-$REV-carher
    sudo sh -c "cd $B/src && docker build --network host -t $T . >$B/build-$TS.log 2>&1" && sudo docker push $T | tail -1
    echo "built $T  ->  sudo kubectl -n litellm-dev set image deploy/gemini-web gemini-web=$T"
    ;;
  media)
    B=/root/gwt-build; REV=681ca80
    sudo rm -rf $B/src && sudo git clone -q https://github.com/ntthanh2603/gemini-web-to-api $B/src && sudo git -C $B/src checkout -q $REV
    sudo git -C $B/src apply "$HERE/gemini-web-to-api-$REV-carher.diff"
    T=127.0.0.1:5000/gemini-web-to-api:$TS-$REV-carher
    sudo sh -c "cd $B/src && docker build --network host -t $T . >$B/build-$TS.log 2>&1" && sudo docker push $T | tail -1
    echo "built $T  ->  sudo kubectl -n litellm-dev set image deploy/gemini-media gemini-media=$T"
    ;;
  *) echo "usage: $0 fastapi|media" >&2; exit 2 ;;
esac
