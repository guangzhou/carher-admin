# v1.105.1 灰度目标镜像构建配方（2026-10-16）。
#
# 第四个补丁 litellm-patch-key-alias-auth-compat.py = 上游 #43049 只在鉴权上回退（key 别名不再收窄白名单；
#   不回退 ⇒ 2116 把 key 里 1323 把对白名单内的名字 403，2026-10-17 灰度实测）。
# 第三个补丁 litellm-patch-stream-alias-pricing.py = 上游 #44341 原样回移（1.103.3 有、1.105.1 没有，流式经别名少计费）。
#
# 与 v1.103.2/Dockerfile 的区别：FROM 换成 vanilla-v1.105.1；capacity 补丁逐字节同 v1.103.2；
# sse 补丁重定了两个 loop-tail 锚点（上游把 `_yielded_first_chunk = True` 换成
# `_note_yielded_event(result)`），并补一处 `_carher_adopt_bare_terminal`（见脚本头注释）。
#
# 锚点实测（2026-10-16，拿 198 /root/litellm-fork 的 v1.105.1 源干跑）：
#   capacity ANCHOR 命中 1，+15/-0；sse 全部锚点各命中 1，+458/-8；py_compile 通过；重跑 SKIP（幂等）
#   上游 1.105.1 仍 0 命中 "selected model is at capacity" / _BARE_STATUS_MAP
#
# base：ghcr.io/berriai/litellm:v1.105.1
#   amd64 manifest digest sha256:b4239fbb9be0d915c312632ccdcccea441db9994a341536fdbf32cae82e15f9c
#   （198 本地 registry vanilla-v1.105.1 push 回来的 digest 与之相同，2026-10-16 实测）
#
# ⚠️ 这个镜像只解决「烘进去的两个补丁」。生产另有 3 个整文件覆盖的 CM 补丁，
#    它们已按 1.105.1 重做在 overlays/，**必须按新版重做**，见本目录 README.md。
#
# 为什么是就地打补丁而不是整文件 COPY：整文件 COPY 会把 base 自己在这两个文件里的
# 上游改动一起静默回退掉。1.100.1 → 1.102.0 这两个文件真的改过
# （exception_mapping_utils.py 11 行、streaming_iterator.py 335 行）。锚点不唯一时
# 脚本直接非零退出，什么都不写。
#
# 构建（在 198 上，镜像必须走本地 registry 别名 127.0.0.1:5000）：
#   docker build -f Dockerfile -t 127.0.0.1:5000/litellm-carher:vanilla-v1.105.1.capacity.sse-fix-bare-<YYYYMMDD-HHMMSS> .
#   docker push 127.0.0.1:5000/litellm-carher:vanilla-v1.105.1.capacity.sse-fix-bare-<...>
# 部署一律按 digest 钉，不按 tag。
#
# 烘完必须做的验收（tag 名不是证据）：
#   两个镜像各自 `cd /app && find . -type f -print0 | sort -z | xargs -0 md5sum` 后 join，
#   只允许这两个文件 md5 不同、不允许任何文件增删。跑验收的 Pod 必须带
#   label litellm.carher.io/role: version-test，否则 NetworkPolicy 连 DNS 都不给。
#   注意：文件总数不再是 19524（1.100.1 的值）—— 1.102.0 base 多了 2 个镜像层，
#   数目要以新 base 自己的 `find | wc -l` 为基准，两侧相等即可。

FROM 127.0.0.1:5000/litellm-carher:vanilla-v1.105.1

COPY litellm-patch-capacity-ratelimit.py /tmp/p1.py
COPY litellm-patch-sse-lifecycle.py /tmp/p2.py
COPY litellm-patch-stream-alias-pricing.py /tmp/p3.py
COPY litellm-patch-key-alias-auth-compat.py /tmp/p4.py

# 两个 grep 是构建期门禁：补丁没落地就让 build 红，绝不允许烘出一个「看起来打了
# 补丁、实际上是原味」的镜像。tag 名字不是证据，这两行才是。
RUN python3 /tmp/p1.py \
 && python3 /tmp/p2.py \
 && python3 /tmp/p3.py \
 && python3 /tmp/p4.py \
 && grep -q "selected model is at capacity" /app/.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py \
 && grep -q "_BARE_STATUS_MAP" /app/.venv/lib/python3.13/site-packages/litellm/responses/streaming_iterator.py \
 && grep -q "client_facing_stream_model" /app/.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/litellm_logging.py \
 && grep -q "client_facing_stream_model" /app/.venv/lib/python3.13/site-packages/litellm/proxy/proxy_server.py \
 && grep -q "carher-patch: key aliases do not narrow auth" /app/.venv/lib/python3.13/site-packages/litellm/proxy/auth/auth_checks.py \
 && rm -f /tmp/p1.py /tmp/p2.py /tmp/p3.py /tmp/p4.py
