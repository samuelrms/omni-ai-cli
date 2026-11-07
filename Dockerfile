# Omni AI server: `docker compose up -d`. Logins and keys live in the volume mounted on /home/omni.
FROM node:25-slim@sha256:81db02c4b671288a03915da9534dbd54f96d0e7c24d80ccc54f5b36b2e684370

ARG CODEX_VERSION=0.166.0
ARG CLAUDE_VERSION=2.1.318
ARG GROK_VERSION=1.0.70
ARG CURSOR_VERSION=2026.11.05-86047df
# What each download must be. After changing a version: scripts/docker-hashes.sh prints the new values.
ARG GROK_SHA256_AMD64=22d281c5964c5444147328096ba2a221f3f0874c6858e27c825656a80f6e58e9
ARG GROK_SHA256_ARM64=3aa36883574b263088e5addffe241f55430e8daaee1a0c4934b02180007a9761
ARG CURSOR_SHA256_AMD64=170b77786cbf4c93064d64fff12b71a178b968730561c64afc2e8e752535442e
ARG CURSOR_SHA256_ARM64=131554bfdbf683d8ebfef83a2bb46ac4e5d060f198711040b07ed063c2471871

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends curl ca-certificates git python3 procps >/dev/null \
    && rm -rf /var/lib/apt/lists/*

# The tools live outside the home folder, so the volume never hides them and an image update replaces them.
ENV HOME=/opt/tools
# Nothing from the internet runs unchecked: npm checks its packages, and the rest must match the hashes above.
RUN mkdir -p /opt/tools/.local/bin /opt/tools/.grok/bin /opt/tools/cursor-agent \
    && npm install -g --silent "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    && case "$(dpkg --print-architecture)" in \
         amd64) grok_arch=x86_64 cursor_arch=x64 grok_sha="$GROK_SHA256_AMD64" cursor_sha="$CURSOR_SHA256_AMD64" ;; \
         arm64) grok_arch=aarch64 cursor_arch=arm64 grok_sha="$GROK_SHA256_ARM64" cursor_sha="$CURSOR_SHA256_ARM64" ;; \
         *) echo "unsupported architecture" >&2; exit 1 ;; \
       esac \
    && curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/grok "https://x.ai/cli/grok-${GROK_VERSION}-linux-${grok_arch}" \
    && echo "${grok_sha}  /tmp/grok" | sha256sum -c - \
    && install -m 755 /tmp/grok /opt/tools/.grok/bin/grok \
    && curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/cursor.tar.gz \
       "https://downloads.cursor.com/lab/${CURSOR_VERSION}/linux/${cursor_arch}/agent-cli-package.tar.gz" \
    && echo "${cursor_sha}  /tmp/cursor.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/cursor.tar.gz --strip-components=1 -C /opt/tools/cursor-agent \
    && rm -f /tmp/grok /tmp/cursor.tar.gz \
    && ln -s /opt/tools/cursor-agent/cursor-agent /opt/tools/.local/bin/cursor-agent \
    && chmod -R a+rX /opt/tools

COPY omni_ai /app/omni_ai
RUN printf '#!/bin/sh\nPYTHONPATH=/app exec python3 -P -m omni_ai "$@"\n' > /usr/local/bin/ai \
    && chmod 755 /usr/local/bin/ai \
    && useradd --uid 10001 --home-dir /home/omni --no-create-home omni \
    && mkdir -p /home/omni \
    && mkdir -p /workspace \
    && chown omni:omni /home/omni /workspace

ENV HOME=/home/omni \
    PATH=/opt/tools/.local/bin:/opt/tools/.grok/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    NO_COLOR=1
USER omni
WORKDIR /home/omni
VOLUME /home/omni /workspace
EXPOSE 8080

# A plain TCP check: there is no public route to call, and it stays out of the request log.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import socket; socket.create_connection(('127.0.0.1', 8080), 3).close()"
CMD ["ai", "serve", "--host", "0.0.0.0", "--port", "8080", "--workdir", "/workspace"]
