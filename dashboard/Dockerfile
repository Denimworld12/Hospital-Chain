# Build context: repository root (the UI imports ../assets and ../design).
FROM node:22-alpine AS build
WORKDIR /repo/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY assets /repo/assets
COPY design /repo/design
COPY dashboard /repo/dashboard
RUN npm run build

FROM node:22-alpine
ENV NODE_ENV=production
WORKDIR /app
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY dashboard/server ./server
COPY --from=build /repo/dashboard/dist ./dist
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=5 CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["node", "server/index.js"]
