# The application under automation. Nothing clever — this container is scenery,
# not system. Multi-stage only because the runtime has no reason to carry a
# package manager and a node_modules tree.

FROM node:24-alpine AS deps
RUN corepack enable
WORKDIR /app
# `--frozen-lockfile` fails the build on a stale lockfile rather than quietly
# resolving something other than what was tested.
#
# `--node-linker=hoisted` is required for Next's standalone output. pnpm's default
# symlinked store is not traceable: standalone follows the module graph and copies
# what it finds, and the symlink farm does not survive that, so the container dies
# on a MODULE_NOT_FOUND for a transitive dep like @swc/helpers. Applied here only —
# local `pnpm dev` keeps the fast symlinked layout.
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --node-linker=hoisted

FROM node:24-alpine AS builder
RUN corepack enable
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm run build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    PORT=8080 \
    HOSTNAME=0.0.0.0

# Don't run the app as root. It is a mock, but it is also the one container the
# automation is pointed at, and "the target ran as root" is a bad sentence to
# have to write later.
RUN addgroup -g 1001 -S app && adduser -u 1001 -S app -G app

# `standalone` is a self-contained server plus only the traced dependencies.
# Static assets are not traced and must be copied alongside it.
COPY --from=builder --chown=app:app /app/.next/standalone ./
COPY --from=builder --chown=app:app /app/.next/static ./.next/static

USER app
EXPOSE 8080
CMD ["node", "server.js"]
