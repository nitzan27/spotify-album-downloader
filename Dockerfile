# Build the React/TS SPA first. Discarded after `COPY --from=` below pulls
# only the built dist/ into the final image, so the runtime container stays
# Node-free - this stage exists purely to avoid committing frontend/dist to
# git (which would risk deploying a stale UI if someone forgets to rebuild).
FROM node:22-slim AS frontend-build

WORKDIR /frontend

COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci

COPY web/frontend/ .
RUN npm run build

# Builds the bgutil PO-token sidecar server from source (see the
# extractor_args note in album_downloader.py / root CLAUDE.md): YouTube's
# bot-check now requires a proof-of-origin token that the plain
# android/ios/web client spoofing can no longer reliably produce on its own,
# especially from Render's datacenter IPs. This generates that token
# on-demand, without needing a real YouTube account or manually exported
# cookies. Pinned to a release tag (not a floating branch) so a build today
# doesn't silently pick up a breaking change later.
FROM node:22-slim AS bgutil-build

WORKDIR /bgutil

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --single-branch --branch 1.3.1 --depth 1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git .

WORKDIR /bgutil/server
# npm audit fix: the pinned release's lockfile carries known-vulnerable
# transitive deps (form-data, path-to-regexp, ws, etc.) with non-breaking
# fixes available - confirmed this resolves cleanly to 0 vulnerabilities
# without needing --force (which would allow breaking major-version bumps).
RUN npm ci && npm audit fix && npx tsc && npm prune --omit=dev

FROM python:3.12-slim

# Node is only needed at runtime to run the bgutil sidecar server below
# (fetched as a prebuilt tarball, not via NodeSource's curl-piped-to-bash
# installer, so the exact version is pinned and auditable).
ARG NODE_VERSION=22.23.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl xz-utils ca-certificates \
    && curl -fsSLO https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz \
    && tar -xJf node-v${NODE_VERSION}-linux-x64.tar.xz -C /usr/local --strip-components=1 \
    && rm node-v${NODE_VERSION}-linux-x64.tar.xz \
    && apt-get purge -y --auto-remove curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend-build /frontend/dist ./web/frontend/dist
COPY --from=bgutil-build /bgutil/server/build ./bgutil-server/build
COPY --from=bgutil-build /bgutil/server/node_modules ./bgutil-server/node_modules
COPY --from=bgutil-build /bgutil/server/package.json ./bgutil-server/package.json
RUN chmod +x docker/start.sh

EXPOSE 8000

# Runs the bgutil PO-token server in the background (best-effort - see
# docker/start.sh) and uvicorn in the foreground as the container's main
# process, so a uvicorn crash still exits the container the way Render
# expects for a restart.
CMD ["docker/start.sh"]
