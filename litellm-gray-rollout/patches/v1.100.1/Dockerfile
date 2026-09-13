# 灰度目标镜像的构建配方 —— 这是「已经烘出来那一版」的原样记录，不是新写的。
#
# 与 image history 的对应关系（可自行核对）：
#   COPY p1.py → litellm-patch-capacity-ratelimit.py
#   COPY p2.py → litellm-patch-sse-lifecycle.py（v1.100.1 版本自带 bare-handler，
#                不需要再单独跑 litellm-patch-streaming-bare-handler.py，那份是
#                v1.90.2 时代的热补丁脚本，已经被折进来了）
#
# 为什么是就地打补丁而不是整文件 COPY：整文件 COPY 会把 base 自己在这两个文件里的
# 上游改动一起静默回退掉。锚点不唯一时脚本直接非零退出，什么都不写。
#
# 实测证据（2026-09-13，两个镜像各 19524 个文件全量 md5 对比）：
#   只有这两个文件的 md5 不同，没有任何文件增删：
#     .venv/lib/python3.13/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py
#     .venv/lib/python3.13/site-packages/litellm/responses/streaming_iterator.py
#
#   base    vanilla-v1.100.1
#           sha256:d85c0b36e2fdd64719e7fc2692431cd5aee0814925e96c2892d1491b13189c23
#   target  vanilla-v1.100.1.capacity.sse-fix-bare-20260913-163142
#           sha256:abdf613339ad36c21fe8b03e53792795fd1f9969ac1d4f390e37735cdb52325e
#
# 构建（在 198 上，镜像必须走本地 registry 别名 127.0.0.1:5000）：
#   docker build -f Dockerfile -t 127.0.0.1:5000/litellm-carher:vanilla-v1.100.1.capacity.sse-fix-bare-<YYYYMMDD-HHMMSS> .
#   docker push 127.0.0.1:5000/litellm-carher:vanilla-v1.100.1.capacity.sse-fix-bare-<...>
# 部署一律按 digest 钉，不按 tag。

FROM 127.0.0.1:5000/litellm-carher:vanilla-v1.100.1

COPY litellm-patch-capacity-ratelimit.py /tmp/p1.py
COPY litellm-patch-sse-lifecycle.py /tmp/p2.py

# 两个 grep 是构建期门禁：补丁没落地就让 build 红，绝不允许烘出一个「看起来打了
# 补丁、实际上是原味」的镜像。tag 名字不是证据，这两行才是。
RUN python3 /tmp/p1.py \
 && python3 /tmp/p2.py \
 && grep -q "selected model is at capacity" /app/.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py \
 && grep -q "_BARE_STATUS_MAP" /app/.venv/lib/python3.13/site-packages/litellm/responses/streaming_iterator.py \
 && rm -f /tmp/p1.py /tmp/p2.py
