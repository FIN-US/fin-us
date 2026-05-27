# ── 1단계: 빌드 ───────────────────────────────────────────────
FROM node:24-slim AS build

WORKDIR /app

# 의존성만 먼저 복사해 레이어 캐시 활용
COPY package*.json ./
RUN npm ci

# 소스 전체 복사 후 프로덕션 번들 생성
COPY . .
RUN npm run build

# ── 2단계: 서빙 ───────────────────────────────────────────────
FROM nginx:alpine

# 빌드 결과물을 nginx 정적 파일 경로로 복사
COPY --from=build /app/dist /usr/share/nginx/html

# /api, /health 경로를 backend 서비스로 프록시하는 nginx 설정 적용
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
