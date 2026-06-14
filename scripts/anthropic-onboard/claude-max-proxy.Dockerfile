# Claude Max transparent proxy image (188 deployment)
#
# Build:  docker compose build
# Run:    see docker-compose.claude-max-proxy.yml
#
# Note: claude CLI is installed into the image purely for backwards compat /
# offline debug. The v3 transparent proxy does NOT shell out to it — it
# forwards Anthropic Messages requests directly. The binary can be safely
# removed in future cleanup.

FROM node:22-bookworm-slim

RUN apt-get update -q \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@2.1.148 \
    && claude --version

# node:22 image already has user "node" with uid 1000; matches host cltx.
USER node
WORKDIR /home/node

COPY --chown=node:node claude-max-proxy.py /home/node/proxy.py

ENV CLAUDE_BIN=/usr/local/bin/claude
ENV PORT=3456

EXPOSE 3456
CMD ["python3", "/home/node/proxy.py"]
