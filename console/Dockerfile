FROM node:24-alpine AS deps
RUN corepack enable
WORKDIR /app
# `--node-linker=hoisted` is required for Next's standalone output: pnpm's default
# symlinked store is not traceable, and the container dies on MODULE_NOT_FOUND for
# a transitive dep. Build-time only; local `pnpm dev` keeps the symlinked layout.
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --node-linker=hoisted

FROM node:24-alpine AS builder
RUN corepack enable
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* is inlined into the client bundle at build time, so these have to
# be present here and not merely at run time. They are the URLs the operator's
# browser dials, so they are localhost-from-the-host, not compose service names.
ARG NEXT_PUBLIC_CUA_API=http://localhost:8000
ARG NEXT_PUBLIC_NOVNC_URL=http://localhost:6080
ENV NEXT_PUBLIC_CUA_API=$NEXT_PUBLIC_CUA_API \
    NEXT_PUBLIC_NOVNC_URL=$NEXT_PUBLIC_NOVNC_URL

RUN pnpm run build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup -g 1001 -S app && adduser -u 1001 -S app -G app

COPY --from=builder --chown=app:app /app/.next/standalone ./
COPY --from=builder --chown=app:app /app/.next/static ./.next/static

USER app
EXPOSE 3000
CMD ["node", "server.js"]
