# v1.103.2 灰度目标镜像构建配方（2026-10-02）。
#
# 与 v1.102.0/Dockerfile 的唯一区别是 FROM 的 base tag。两个补丁脚本逐字节相同
# （md5 e7173f40266f842bc274d40442a0e36a / 07a96c9cb8c23d34eec9b76358125c5e）。
#
# 锚点实测（2026-10-01，拿 raw.githubusercontent 上 v1.103.2 的两个文件干跑）：
#   capacity  ANCHOR 命中 1；sse ANCHOR_CLASS / ANCHOR_INIT 各命中 1
#   exception_mapping_utils.py +15/-0（2758 行）；streaming_iterator.py +440/-8（2894 行）
#   py_compile 通过；重跑得 SKIP（幂等）；上游仍 0 命中 "selected model is at capacity" / _BARE_STATUS_MAP
#
# base：ghcr.io/berriai/litellm:v1.103.2
#   amd64 manifest digest sha256:5414e701cd8abce8713e7ca16d3e016345ca48a2e5cfe4efe3d2f22c42e45b77
#   （198 本地 registry 的 vanilla-v1.103.2 与之逐字节相同，2026-10-02 实测）
#
# ⚠️ 这个镜像只解决「烘进去的两个补丁」。生产另有 3 个整文件覆盖的 CM 补丁，
#    它们按 1.100.1 写的，**必须按新版重做**，见本目录 README.md。
#
# 为什么是就地打补丁而不是整文件 COPY：整文件 COPY 会把 base 自己在这两个文件里的
# 上游改动一起静默回退掉。1.100.1 → 1.102.0 这两个文件真的改过
# （exception_mapping_utils.py 11 行、streaming_iterator.py 335 行）。锚点不唯一时
# 脚本直接非零退出，什么都不写。
#
# 构建（在 198 上，镜像必须走本地 registry 别名 127.0.0.1:5000）：
#   先把 vanilla base 拉进本地 registry（ghcr 的 v1.103.2 amd64 manifest digest
#   sha256:9d60771c86a42ced1b918f23dd940a1ac9905ddb02a4fae779cfe1937477c9a0，
#   index digest sha256:32cfd7a427f6470033b4dcc9e75dfbea2aa32dbc298b0b511a40e962ce818362），
#   tag 成 127.0.0.1:5000/litellm-carher:vanilla-v1.103.2 后 push。
#   docker build -f Dockerfile -t 127.0.0.1:5000/litellm-carher:vanilla-v1.103.2.capacity.sse-fix-bare-<YYYYMMDD-HHMMSS> .
#   docker push 127.0.0.1:5000/litellm-carher:vanilla-v1.103.2.capacity.sse-fix-bare-<...>
# 部署一律按 digest 钉，不按 tag。
#
# 烘完必须做的验收（tag 名不是证据）：
#   两个镜像各自 `cd /app && find . -type f -print0 | sort -z | xargs -0 md5sum` 后 join，
#   只允许这两个文件 md5 不同、不允许任何文件增删。跑验收的 Pod 必须带
#   label litellm.carher.io/role: version-test，否则 NetworkPolicy 连 DNS 都不给。
#   注意：文件总数不再是 19524（1.100.1 的值）—— 1.102.0 base 多了 2 个镜像层，
#   数目要以新 base 自己的 `find | wc -l` 为基准，两侧相等即可。

FROM 127.0.0.1:5000/litellm-carher:vanilla-v1.103.2

COPY litellm-patch-capacity-ratelimit.py /tmp/p1.py
COPY litellm-patch-sse-lifecycle.py /tmp/p2.py

# 两个 grep 是构建期门禁：补丁没落地就让 build 红，绝不允许烘出一个「看起来打了
# 补丁、实际上是原味」的镜像。tag 名字不是证据，这两行才是。
RUN python3 /tmp/p1.py \
 && python3 /tmp/p2.py \
 && grep -q "selected model is at capacity" /app/.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py \
 && grep -q "_BARE_STATUS_MAP" /app/.venv/lib/python3.13/site-packages/litellm/responses/streaming_iterator.py \
 && rm -f /tmp/p1.py /tmp/p2.py
