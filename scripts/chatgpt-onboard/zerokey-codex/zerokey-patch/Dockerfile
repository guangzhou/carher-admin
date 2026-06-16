FROM node:22-slim

WORKDIR /app

# Copy the whole repo (node_modules included from the build context to avoid a
# network install; falls back to npm install if absent).
COPY . .
RUN test -d node_modules && echo "using vendored node_modules" \
    || npm install --omit=dev --no-audit --no-fund

ENV PORT=8123
EXPOSE 8123

CMD ["node", "zerokey-serve-codex.js"]
