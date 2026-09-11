# ============================================================================
# Image dashboard ThinkTuning — frontend standalone (Phase 5, découplage).
#
# Sert le build Vite via nginx et proxifie /api/* vers l'API (conteneur
# séparé) : API_UPSTREAM (défaut : service compose « app:8000 »).
#
# Non-root robuste : user nginx (101), listen >1024 (8080), chemins
# temporables /tmp (nginx.main.conf), répertoires inscriptibles chownés au
# build ; l'entrypoint officiel exécute l'envsubst des templates.
# ============================================================================
FROM node:20-alpine@sha256:afdf98210b07b586eb71fa22ba2e432e058e4cd1304d31ed60888755b8c865fb AS build
WORKDIR /app

COPY package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

COPY . .
# Chaîne vide => le client API utilise des chemins relatifs (/api/...)
# et passe par le proxy nginx de CE conteneur (templates/default.conf.template).
ARG VITE_API_URL=
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM nginx:1.27-alpine@sha256:62223d644fa234c3a1cc785ee14242ec47a77364226f1c811d2f669f96dc2ac8

COPY nginx.main.conf /etc/nginx/nginx.conf
COPY templates/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

# L'entrypoint officiel (envsubst) écrit dans conf.d ; nginx écrit cache/logs.
RUN rm -f /etc/nginx/conf.d/default.conf \
    && chown -R 101:101 /etc/nginx/conf.d /var/cache/nginx /var/log/nginx /usr/share/nginx/html

ENV FRONTEND_PORT=8080 \
    API_UPSTREAM=app:8000

USER 101

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["wget", "-q", "--spider", "http://127.0.0.1:8080/"]

CMD ["nginx", "-g", "daemon off;"]
