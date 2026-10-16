# Solver tools over the unchanged Rucio test runtime; no source or credentials.
FROM docker.io/library/node:20-bookworm-slim AS node-runtime
FROM localhost/patchbench-rucio-historical:tests-v2

ARG CLAUDE_CODE_VERSION=2.1.197
LABEL org.patchbench.claude-code.version="${CLAUDE_CODE_VERSION}"

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates git libatomic1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && useradd --create-home --uid 1000 --user-group --shell /bin/bash node \
    && python -m pip check \
    && test "$(claude --version | awk '{print $1}')" = "${CLAUDE_CODE_VERSION}"

ENV HOME=/home/node
USER node
WORKDIR /workspace
ENTRYPOINT []
CMD ["bash"]
