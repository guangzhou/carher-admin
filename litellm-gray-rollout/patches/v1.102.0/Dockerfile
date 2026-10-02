# v1.102.0 灰度目标镜像构建配方。
#
# 与 v1.100.1/Dockerfile 的唯一区别是 FROM 那一行的 base tag。两个补丁脚本
# 逐字节相同（md5 e7173f40266f842bc274d40442a0e36a / 07a96c9cb8c23d34eec9b76358125c5e），
# 因为锚点在 1.100.1 → 1.102.0 之间一次都没漂。
#
# 锚点实测（2026-09-21，拿 raw.githubusercontent 上 v1.102.0 的两个文件干跑）：
#   capacity  ANCHOR                        命中 1 次（要求恰好 1）
#   sse       ANCHOR_CLASS / ANCHOR_INIT    各命中 1 次
#   干跑结果  exception_mapping_utils.py +15/-0
#             streaming_iterator.py      +394/-8
#   两个文件 py_compile 通过；重跑得 SKIP（幂等）
#   补丁块引用的 7 个名字在新 base 的 `_map_openai_exception` 作用域里全部可解析
#   （exception_provider/model/custom_llm_provider/original_exception/extra_information/
#    error_str 是形参，message 是局部变量；`exception_mapping_worked` 未被引用）
#   补丁块用到的 9 个 ResponsesAPIStreamEvents 成员在 v1.102.0 的 types/llms/openai.py 里全在
#   ANCHOR_INIT 仍落在 BaseResponsesAPIStreamingIterator.__init__ 内
#
# 上游在 1.102.0 仍然没有修这两件事，所以补丁仍然必需：
#   - "Selected model is at capacity" 全仓 0 命中；is_error_str_rate_limit 三种形状都不匹配
#   - _ResponsesLifecycleGapFiller / _BARE_STATUS_MAP 上游 0 命中；
#     lifecycle 事件计数与 1.100.1 完全一致（content_part.added 仍为 0）
#
# 为什么是就地打补丁而不是整文件 COPY：整文件 COPY 会把 base 自己在这两个文件里的
# 上游改动一起静默回退掉。1.100.1 → 1.102.0 这两个文件真的改过
# （exception_mapping_utils.py 11 行、streaming_iterator.py 335 行）。锚点不唯一时
# 脚本直接非零退出，什么都不写。
#
# 构建（在 198 上，镜像必须走本地 registry 别名 127.0.0.1:5000）：
#   先把 vanilla base 拉进本地 registry（ghcr 的 v1.102.0 amd64 manifest digest
#   sha256:9d60771c86a42ced1b918f23dd940a1ac9905ddb02a4fae779cfe1937477c9a0，
#   index digest sha256:32cfd7a427f6470033b4dcc9e75dfbea2aa32dbc298b0b511a40e962ce818362），
#   tag 成 127.0.0.1:5000/litellm-carher:vanilla-v1.102.0 后 push。
#   docker build -f Dockerfile -t 127.0.0.1:5000/litellm-carher:vanilla-v1.102.0.capacity.sse-fix-bare-<YYYYMMDD-HHMMSS> .
#   docker push 127.0.0.1:5000/litellm-carher:vanilla-v1.102.0.capacity.sse-fix-bare-<...>
# 部署一律按 digest 钉，不按 tag。
#
# 烘完必须做的验收（tag 名不是证据）：
#   两个镜像各自 `cd /app && find . -type f -print0 | sort -z | xargs -0 md5sum` 后 join，
#   只允许这两个文件 md5 不同、不允许任何文件增删。跑验收的 Pod 必须带
#   label litellm.carher.io/role: version-test，否则 NetworkPolicy 连 DNS 都不给。
#   注意：文件总数不再是 19524（1.100.1 的值）—— 1.102.0 base 多了 2 个镜像层，
#   数目要以新 base 自己的 `find | wc -l` 为基准，两侧相等即可。

FROM 127.0.0.1:5000/litellm-carher:vanilla-v1.102.0

COPY litellm-patch-capacity-ratelimit.py /tmp/p1.py
COPY litellm-patch-sse-lifecycle.py /tmp/p2.py

# 两个 grep 是构建期门禁：补丁没落地就让 build 红，绝不允许烘出一个「看起来打了
# 补丁、实际上是原味」的镜像。tag 名字不是证据，这两行才是。
RUN python3 /tmp/p1.py \
 && python3 /tmp/p2.py \
 && grep -q "selected model is at capacity" /app/.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py \
 && grep -q "_BARE_STATUS_MAP" /app/.venv/lib/python3.13/site-packages/litellm/responses/streaming_iterator.py \
 && rm -f /tmp/p1.py /tmp/p2.py
