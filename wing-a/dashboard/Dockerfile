# wing-a/dashboard/Dockerfile — the React dashboard, built once and served by nginx.
#
# Multi-stage like the engine image: the Node stage (npm + the whole
# toolchain) compiles the app to plain static files; the final image is just
# nginx plus those files, with no Node in it at all.
#
# nginx also forwards /api/* to the wing-a container, so the browser only
# ever talks to ONE address. That's what makes this work without the
# localhost:8000 / CORS problems of running the dev server against containers.
#
# Build from this directory:  docker build -t dualis-frontend .

# ── Build stage ────────────────────────────────────────────────────────────
FROM node:20-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Empty = "call /api/... on whatever host served this page" (src/api/client.ts
# treats an empty VITE_API_URL as same-origin). The dev server still defaults
# to http://localhost:8000 because this ARG is only set for the image build.
ARG VITE_API_URL=""
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

# ── Runtime stage ──────────────────────────────────────────────────────────
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
