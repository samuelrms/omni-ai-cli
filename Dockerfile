# Omni AI server: `docker compose up -d`. Logins and keys live in the volume mounted on /home/omni.
FROM node:25-slim@sha256:81db02c4b671288a03915da9534dbd54f96d0e7c24d80ccc54f5b36b2e684370

ARG CODEX_VERSION=0.166.0
ARG CLAUDE_VERSION=2.1.318
ARG GROK_VERSION=1.0.70
ARG CURSOR_VERSION=2026.11.05-86047df

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends curl ca-certificates git python3 procps >/dev/null \
    && rm -rf /var/lib/apt/lists/*

# The tools live outside the home folder, so the volume never hides them and an image update replaces them.
ENV HOME=/opt/tools
RUN mkdir -p /opt/tools/.local/bin \
    && npm install -g --silent "@openai/codex@${CODEX_VERSION}" \
    && curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_VERSION}" \
    && curl -fsSL https://x.ai/cli/install.sh | bash -s "${GROK_VERSION}" \
    && arch="$(dpkg --print-architecture | sed 's/amd64/x64/')" \
    && mkdir -p "/opt/tools/cursor-agent" \
    && curl -fsSL "https://downloads.cursor.com/lab/${CURSOR_VERSION}/linux/${arch}/agent-cli-package.tar.gz" \
       | tar -xz --strip-components=1 -C /opt/tools/cursor-agent \
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
