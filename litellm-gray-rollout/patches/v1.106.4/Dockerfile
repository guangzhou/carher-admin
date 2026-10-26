# v1.106.4 灰度目标镜像构建配方（2026-10-26）。上一代 ../v1.105.1/Dockerfile。
#
# 与 v1.105.1 的区别：
#   - FROM 换成 vanilla-v1.106.4
#   - 去掉 litellm-patch-stream-alias-pricing.py（上游 #44341 已进 1.106，脚本对 1.106.4 源 SKIP）
#     及它的两条 grep 门禁；改成断言上游自带的 client_facing_stream_model 仍在（防上游再撤）
#   - 其余三个补丁逐字节同 v1.105.1；对 git archive v1.106.4 干跑：capacity / sse / key-alias 各 OK
#
# base：ghcr.io/berriai/litellm:v1.106.4
#   amd64 manifest digest sha256:f6589ec874ed5e37ed90452d0b86613ad07393a1148a8a401639a87533f11b0c
#
# ⚠️ 生产另有 3 个整文件覆盖的 CM 补丁，已按 1.106.4 重做在 overlays/，见 README.md。
#
# 构建（在 198 上）：
#   docker build -f Dockerfile -t 127.0.0.1:5000/litellm-carher:vanilla-v1.106.4.capacity.sse-fix-bare.keyalias-auth-<YYYYMMDD-HHMMSS> .
#   docker push …；部署一律按 digest 钉。
# 验收：两镜像 `cd /app && find . -type f -print0 | sort -z | xargs -0 md5sum` join，只许补丁文件不同、零增删。

FROM 127.0.0.1:5000/litellm-carher:vanilla-v1.106.4

COPY litellm-patch-capacity-ratelimit.py /tmp/p1.py
COPY litellm-patch-sse-lifecycle.py /tmp/p2.py
COPY litellm-patch-key-alias-auth-compat.py /tmp/p4.py

# grep 是构建期门禁：补丁没落地就让 build 红。
RUN python3 /tmp/p1.py \
 && python3 /tmp/p2.py \
 && python3 /tmp/p4.py \
 && grep -q "selected model is at capacity" /app/.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/exception_mapping_utils.py \
 && grep -q "_BARE_STATUS_MAP" /app/.venv/lib/python3.13/site-packages/litellm/responses/streaming_iterator.py \
 && grep -q "client_facing_stream_model" /app/.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/litellm_logging.py \
 && grep -q "carher-patch: key aliases do not narrow auth" /app/.venv/lib/python3.13/site-packages/litellm/proxy/auth/auth_checks.py \
 && rm -f /tmp/p1.py /tmp/p2.py /tmp/p4.py
