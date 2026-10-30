# muse2api egress tunnel: ssh -D SOCKS on 127.0.0.1:1080 -> Aliyun Singapore (47.84.85.100).
# Runs as a sidecar in the muse2api pod; Chromium and requests in the main container use it.
FROM alpine:3.20
RUN apk add --no-cache openssh-client-default curl \
 && adduser -D -u 10001 tunnel
COPY entrypoint.sh /entrypoint.sh
USER tunnel
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
