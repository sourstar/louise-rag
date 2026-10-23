# The München agent's container on this PC's Docker Desktop (operator, 2026-10-23:
# "do it in desktop docker"), after Perugia's (D:\louise-agent): Claude Code, git,
# ssh to the node, and the docker CLI talking to the host's daemon through the
# mounted socket. See README.md.
FROM docker:27-cli AS dockercli

FROM node:22-bookworm-slim
ENV TZ=Europe/Rome
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl jq python3 openssh-client procps tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
RUN npm install -g @anthropic-ai/claude-code@2.1.309 \
    && git config --system --add safe.directory '*' \
    && git config --system core.autocrlf false
