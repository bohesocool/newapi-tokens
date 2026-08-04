#!/usr/bin/env bash
# newapi-tokens 一键启动脚本
# 自动探测 NewAPI 的 PostgreSQL 连接信息，生成 .env，构建并启动 Docker 容器
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 颜色 ──
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

log() { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $1"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)]${NC} $1"; }
err() { echo -e "${RED}[$(date +%H:%M:%S)]${NC} $1" >&2; }
info() { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $1"; }

# ── 前置检查 ──
command -v docker >/dev/null 2>&1 || { err "docker 未安装"; exit 1; }
docker compose version >/dev/null 2>&1 || { err "docker compose 不可用"; exit 1; }

# ── 探测 NewAPI PostgreSQL 连接信息 ──
log "探测 NewAPI 数据库连接信息..."

PG_HOST=""
PG_PORT="5432"
PG_USER="root"
PG_DB="new-api"
PG_PASSWORD=""

# 方法 1: 从 new-api 容器环境变量读取
if docker inspect new-api >/dev/null 2>&1; then
    SQL_DSN=$(docker inspect new-api --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^SQL_DSN=' | head -1 || true)
    if [[ -n "$SQL_DSN" ]]; then
        # 解析 postgresql://user:password@host:port/db
        if [[ "$SQL_DSN" =~ postgresql://([^:]+):([^@]+)@([^:]+):([0-9]+)/([^?]+) ]]; then
            PG_USER="${BASH_REMATCH[1]}"
            PG_PASSWORD="${BASH_REMATCH[2]}"
            PG_HOST="${BASH_REMATCH[3]}"
            PG_PORT="${BASH_REMATCH[4]}"
            PG_DB="${BASH_REMATCH[5]}"
        elif [[ "$SQL_DSN" =~ postgresql://([^:]+):([^@]+)@([^/]+)/(.+) ]]; then
            PG_USER="${BASH_REMATCH[1]}"
            PG_PASSWORD="${BASH_REMATCH[2]}"
            PG_HOST="${BASH_REMATCH[3]}"
            PG_DB="${BASH_REMATCH[4]}"
        fi
        info "从 new-api 容器 SQL_DSN 探测到: host=$PG_HOST user=$PG_USER db=$PG_DB"
    fi
fi

# 方法 2: 从 postgres 容器环境变量读取密码
if [[ -z "$PG_PASSWORD" ]]; then
    if docker inspect postgres >/dev/null 2>&1; then
        PG_PASSWORD=$(docker inspect postgres --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^POSTGRES_PASSWORD=' | cut -d= -f2- || true)
        PG_USER=$(docker inspect postgres --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^POSTGRES_USER=' | cut -d= -f2- || true)
        PG_USER="${PG_USER:-root}"
        PG_DB=$(docker inspect postgres --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | grep '^POSTGRES_DB=' | cut -d= -f2- || true)
        PG_DB="${PG_DB:-new-api}"
        PG_HOST="postgres"
        info "从 postgres 容器探测到: host=$PG_HOST user=$PG_USER db=$PG_DB"
    fi
fi

# 方法 3: 从现有 .env 读取（如果已有）
if [[ -z "$PG_PASSWORD" ]] && [[ -f .env ]]; then
    PG_PASSWORD=$(grep '^PG_PASSWORD=' .env 2>/dev/null | cut -d= -f2- || true)
    PG_HOST=$(grep '^PG_HOST=' .env 2>/dev/null | cut -d= -f2- || true)
    PG_USER=$(grep '^PG_USER=' .env 2>/dev/null | cut -d= -f2- || true)
    PG_DB=$(grep '^PG_DB=' .env 2>/dev/null | cut -d= -f2- || true)
    if [[ -n "$PG_PASSWORD" ]]; then
        info "从现有 .env 读取到: host=$PG_HOST user=$PG_USER db=$PG_DB"
    fi
fi

if [[ -z "$PG_PASSWORD" ]]; then
    err "无法自动探测 NewAPI PostgreSQL 密码"
    err "请手动编辑 .env 并设置 PG_PASSWORD"
    err "或运行: $0 --pg-password <密码>"
    exit 1
fi

PG_HOST="${PG_HOST:-postgres}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-root}"
PG_DB="${PG_DB:-new-api}"

log "数据库连接: $PG_USER@$PG_HOST:$PG_PORT/$PG_DB"

# ── 探测 Docker 网络 ──
NETWORK="new-api_new-api-network"
if ! docker network ls --format '{{.Name}}' | grep -q "^${NETWORK}$"; then
    warn "Docker 网络 $NETWORK 不存在"
    # 尝试查找包含 new-api 的网络
    FOUND_NET=$(docker network ls --format '{{.Name}}' | grep -i 'new-api' | head -1 || true)
    if [[ -n "$FOUND_NET" ]]; then
        NETWORK="$FOUND_NET"
        info "找到替代网络: $NETWORK"
    else
        warn "未找到 NewAPI Docker 网络，将自动创建 newapi-tokens-net"
        docker network create newapi-tokens-net >/dev/null 2>&1 || true
        NETWORK="newapi-tokens-net"
    fi
fi
log "Docker 网络: $NETWORK"

# ── 探测 Telegram Bot Token（可选） ──
TG_BOT_TOKEN=""
TG_CHAT_ID=""
if [[ -f .env ]]; then
    TG_BOT_TOKEN=$(grep '^TG_BOT_TOKEN=' .env 2>/dev/null | cut -d= -f2- || true)
    TG_CHAT_ID=$(grep '^TG_CHAT_ID=' .env 2>/dev/null | cut -d= -f2- || true)
fi

# 如果没有 Telegram 配置，交互式询问（非交互模式跳过）
if [[ -z "$TG_BOT_TOKEN" ]] && [[ -t 0 ]]; then
    echo ""
    info "Telegram 推送配置（可选，直接回车跳过）"
    read -rp "  Bot Token: " INPUT_TOKEN
    if [[ -n "$INPUT_TOKEN" ]]; then
        TG_BOT_TOKEN="$INPUT_TOKEN"
        read -rp "  Chat ID:   " INPUT_CHAT
        TG_CHAT_ID="$INPUT_CHAT"
        log "Telegram 已配置: chat_id=$TG_CHAT_ID"
    else
        warn "跳过 Telegram 配置（可稍后手动编辑 .env）"
    fi
    echo ""
fi

# ── 生成 .env ──
log "生成 .env..."

# 保留现有 ADMIN_PASSWORD 或生成默认
ADMIN_PASSWORD="bohesobad123."
if [[ -f .env ]]; then
    EXISTING_ADMIN_PW=$(grep '^ADMIN_PASSWORD=' .env 2>/dev/null | cut -d= -f2- || true)
    [[ -n "$EXISTING_ADMIN_PW" ]] && ADMIN_PASSWORD="$EXISTING_ADMIN_PW"
fi

# 保留现有 TOKEN_NAME
TOKEN_NAME="ducker"
if [[ -f .env ]]; then
    EXISTING_TOKEN=$(grep '^TOKEN_NAME=' .env 2>/dev/null | cut -d= -f2- || true)
    [[ -n "$EXISTING_TOKEN" ]] && TOKEN_NAME="$EXISTING_TOKEN"
fi

cat > .env <<EOF
# NewAPI Monitor .env — 由 $0 自动生成于 $(date '+%Y-%m-%d %H:%M:%S')

# PostgreSQL direct connection (psycopg, read-only pool over docker network)
PG_HOST=$PG_HOST
PG_PORT=$PG_PORT
PG_USER=$PG_USER
PG_DB=$PG_DB
PG_PASSWORD=$PG_PASSWORD

# Token to track
TOKEN_NAME=$TOKEN_NAME

# Initial admin password (only used on first run)
ADMIN_PASSWORD=$ADMIN_PASSWORD
# Telegram webhook relay
TG_BOT_TOKEN=$TG_BOT_TOKEN
TG_CHAT_ID=$TG_CHAT_ID
EOF

log ".env 已生成"

# ── 更新 docker-compose.yml 中的网络名称（如果需要） ──
if [[ "$NETWORK" != "new-api_new-api-network" ]]; then
    if grep -q 'new-api_new-api-network' docker-compose.yml; then
        sed -i "s/new-api_new-api-network/$NETWORK/g" docker-compose.yml
        info "docker-compose.yml 网络已更新为 $NETWORK"
    fi
fi

# ── 构建 ──
log "构建 Docker 镜像..."
docker compose build --no-cache newapi-monitor 2>&1 | tail -5
docker compose build --no-cache tg-relay 2>&1 | tail -3

# ── 启动 ──
log "启动容器..."
docker compose up -d

# ── 等待健康检查 ──
log "等待健康检查..."
for i in $(seq 1 15); do
    sleep 2
    STATUS=$(docker inspect newapi-monitor --format '{{.State.Health.Status}}' 2>/dev/null || echo "none")
    if [[ "$STATUS" == "healthy" ]]; then
        log "newapi-monitor: healthy ✓"
        break
    fi
    [[ $i -eq 15 ]] && warn "newapi-monitor 健康检查超时，请检查日志: docker logs newapi-monitor"
done

# ── 验证 ──
log "验证服务..."
sleep 1
HEALTH=$(curl -sf http://127.0.0.1:9217/api/health 2>/dev/null || echo "")
if [[ -n "$HEALTH" ]]; then
    log "API 健康检查通过 ✓"
else
    warn "API 暂未响应，可能需要几秒钟启动"
fi

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  newapi-tokens 已启动${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
echo ""
echo "  仪表盘:    http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):9217"
echo "  管理密码:  $ADMIN_PASSWORD"
echo "  数据库:    $PG_USER@$PG_HOST:$PG_PORT/$PG_DB"
echo "  跟踪令牌:  $TOKEN_NAME"
echo "  Telegram: $([[ -n "$TG_BOT_TOKEN" ]] && echo '已配置' || echo '未配置')"
echo ""
echo "  查看日志:  docker logs -f newapi-monitor"
echo "  停止:      cd $SCRIPT_DIR && docker compose down"
echo ""

# ── 配置 webhook（如果 Telegram 已配置） ──
if [[ -n "$TG_BOT_TOKEN" ]] && [[ -n "$TG_CHAT_ID" ]]; then
    log "配置 Telegram webhook..."
    # 登录
    LOGIN_RESP=$(curl -sf -X POST http://127.0.0.1:9217/api/auth/login \
        -H 'Content-Type: application/json' \
        -d "{\"password\":\"$ADMIN_PASSWORD\"}" 2>/dev/null || true)
    if [[ -n "$LOGIN_RESP" ]]; then
        # 提取 cookie
        COOKIE=$(echo "$LOGIN_RESP" | python3 -c "import sys; print('')" 2>/dev/null || true)
        # 用 cookie 配置 webhook
        curl -sf -X POST http://127.0.0.1:9217/api/settings/webhook \
            -H 'Content-Type: application/json' \
            -H "Cookie: pool_session=$COOKIE" \
            -d '{"url":"http://newapi-tg-relay:9218/","push_hourly":true,"push_daily":true,"push_error":true}' \
            >/dev/null 2>&1 || true
        log "Telegram webhook 已配置 ✓"
    fi
fi
