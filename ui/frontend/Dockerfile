# Build-time backend URLs — Vite inlines import.meta.env.VITE_* at BUILD time,
# not runtime, so the three backend Cloud Run services must already be
# deployed (their URLs known) before this image is built. See deploy steps.
ARG VITE_STATE_SERVICE_URL
ARG VITE_ORCHESTRATOR_URL
ARG VITE_SURGBOT_SERVICE_URL

FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_STATE_SERVICE_URL
ARG VITE_ORCHESTRATOR_URL
ARG VITE_SURGBOT_SERVICE_URL
ENV VITE_STATE_SERVICE_URL=${VITE_STATE_SERVICE_URL}
ENV VITE_ORCHESTRATOR_URL=${VITE_ORCHESTRATOR_URL}
ENV VITE_SURGBOT_SERVICE_URL=${VITE_SURGBOT_SERVICE_URL}
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
