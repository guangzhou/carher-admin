# 10-27：现役 1.106.4+dssearch 镜像上只换 uvloop 0.22.1 → 0.23.0（其它一个字节不动）。
#
# 为什么：uvloop < 0.23.0 有 fd 双关 bug（MagicStack/uvloop#738，修复 #740 进 0.23.0 2026-09-30）：
#   create_connection(sock=) 被取消 ⇒ libuv 关了 fd 但 Python socket 不知道 ⇒ GC 时再关一次 ⇒ 关掉别人刚复用到这个号的 fd。
#   被关的若是 glibc getaddrinfo 线程里的 netlink socket ⇒ `Unexpected error 9 on netlink descriptor N` + abort()
#   ⇒ uvicorn worker 整个死掉，在途请求全断（pod 不重启）。aiohttp 3.14 正是走 aiohappyeyeballs → create_connection(sock=)。
#   上游跟踪：BerriAI/litellm#44623（uv.lock 仍钉 0.22.1）。
# 证据：10-27 06:32:33.78Z l258r 打出 netlink error 9，0.38s 后 Child process [79] died；225 apport 同秒记 host pid crashed。
#   188 受控复现（python:3.13-slim，同脚本只换 uvloop 版本）：0.22.1 三次三崩、0.23.0 三次 0 泄漏、stdlib asyncio 0 泄漏。
#
# 构建（198）：docker build -t 127.0.0.1:5000/litellm-carher:vanilla-v1.106.4.capacity.sse-fix-bare.keyalias-auth.dssearch.uvloop023-<ts> .
# 验收：md5 join 对 5461b2d6 只许 uvloop 包内文件变；行为断言见 RUN。
FROM 127.0.0.1:5000/litellm-carher@sha256:5461b2d68a7f9df2aa072882b522792697081748e3ccd1e1aa288a670e6b23c5

COPY uvloop-0.23.0-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl /tmp/u.whl
COPY assert_uvloop.py /tmp/assert_uvloop.py
# venv 里没有 pip ⇒ wheel 就是 zip：删旧包目录 + 旧 dist-info，原地解压新包（只动 uvloop 自己的文件）
RUN echo "e2cba180d6451822763eda8364f342435a873bcfb3849cbd82fdeca248ca65eb  /tmp/u.whl" | sha256sum -c - \
 && SP=/app/.venv/lib/python3.13/site-packages \
 && test -d $SP/uvloop && test -d $SP/uvloop-0.22.1.dist-info \
 && rm -rf $SP/uvloop $SP/uvloop-0.22.1.dist-info \
 && /app/.venv/bin/python -c "import zipfile; zipfile.ZipFile('/tmp/u.whl').extractall('$SP')" \
 && test -d $SP/uvloop-0.23.0.dist-info \
 && /app/.venv/bin/python /tmp/assert_uvloop.py \
 && rm -f /tmp/u.whl /tmp/assert_uvloop.py
