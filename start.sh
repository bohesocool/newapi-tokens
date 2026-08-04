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

# 保留现有 TRACK_GROUP
TRACK_GROUP="default"
if [[ -f .env ]]; then
    EXISTING_GROUP=$(grep '^TRACK_GROUP=' .env 2>/dev/null | cut -d= -f2- || true)
    [[ -n "$EXISTING_GROUP" ]] && TRACK_GROUP="$EXISTING_GROUP"
fi

# ── 从 NewAPI 数据库查出可用分组，交互式选择 ──
if [[ -t 0 ]]; then
    echo ""
    info "正在从 NewAPI 数据库查询可用分组..."

    # 用 docker exec 进 postgres 容器查 distinct group
    # 整段用子 shell 包裹，任何失败都不会让 set -e 杀掉脚本
    GROUPS=""
    {
        if docker inspect postgres >/dev/null 2>&1; then
            CONTAINER_NAME="postgres"
        elif docker inspect "${PG_HOST:-postgres}" >/dev/null 2>&1; then
            CONTAINER_NAME="${PG_HOST}"
        else
            CONTAINER_NAME=""
        fi
        if [[ -n "$CONTAINER_NAME" ]]; then
            docker exec -e PGPASSWORD="${PG_PASSWORD}" "$CONTAINER_NAME" \
                psql -U "${PG_USER:-root}" -d "${PG_DB:-new-api}" -t -A -c \
                "SELECT DISTINCT \"group\" FROM tokens WHERE \"group\" IS NOT NULL AND \"group\" <> '' ORDER BY 1;" 2>/dev/null
        fi
    } | while read -r line; do [[ -n "$line" ]] && echo "$line"; done > /tmp/na_groups.txt 2>/dev/null || true
    GROUPS=$(cat /tmp/na_groups.txt 2>/dev/null || true)
    rm -f /tmp/na_groups.txt 2>/dev/null || true

    if [[ -n "$GROUPS" ]]; then
        # 转成数组
        mapfile -t GROUP_LIST <<< "$GROUPS"
        echo ""
        echo -e "${CYAN}  可用分组:${NC}"
        for i in "${!GROUP_LIST[@]}"; do
            num=$((i + 1))
            marker=""
            [[ "${GROUP_LIST[$i]}" == "$TRACK_GROUP" ]] && marker=" ${GREEN}← 当前${NC}"
            echo -e "    $num) ${GROUP_LIST[$i]}$marker"
        done
        echo ""
        read -rp "  选择追踪分组 (输入序号或直接输入名称，回车保持 [$TRACK_GROUP]): " INPUT_GROUP
        if [[ -n "$INPUT_GROUP" ]]; then
            # 如果是纯数字，按序号选
            if [[ "$INPUT_GROUP" =~ ^[0-9]+$ ]] && [[ "$INPUT_GROUP" -ge 1 ]] && [[ "$INPUT_GROUP" -le "${#GROUP_LIST[@]}" ]]; then
                TRACK_GROUP="${GROUP_LIST[$((INPUT_GROUP - 1))]}"
            else
                TRACK_GROUP="$INPUT_GROUP"
            fi
        fi
    else
        warn "无法从数据库查询分组（postgres 容器不可用或无分组数据）"
        read -rp "  追踪分组 [default]: " INPUT_GROUP
        [[ -n "$INPUT_GROUP" ]] && TRACK_GROUP="$INPUT_GROUP"
    fi
    log "追踪分组: $TRACK_GROUP"
    echo ""
fi

cat > .env <<EOF
# NewAPI Monitor .env — 由 $0 自动生成于 $(date '+%Y-%m-%d %H:%M:%S')

# PostgreSQL direct connection (psycopg, read-only pool over docker network)
PG_HOST=$PG_HOST
PG_PORT=$PG_PORT
PG_USER=$PG_USER
PG_DB=$PG_DB
PG_PASSWORD=$PG_PASSWORD

# Group to track
TRACK_GROUP=$TRACK_GROUP

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

# ── 构建（只在代码变化时重建） ──
# 判断是否需要重建：对比当前镜像和代码是否有变化
NEED_REBUILD=false
CURRENT_IMAGE_ID=$(docker images --format '{{.ID}}' newapi-tokens-newapi-monitor:latest 2>/dev/null || echo "")
if [[ -z "$CURRENT_IMAGE_ID" ]]; then
    NEED_REBUILD=true
    log "首次构建，没有现有镜像"
else
    # 检查 app/ 和 relay/ 目录是否有变化（对比 git status 或文件时间戳）
    # 方法：用 docker inspect 拿当前镜像的创建时间，和 app/ 下最新文件比较
    IMAGE_CREATED=$(docker inspect newapi-tokens-newapi-monitor:latest --format '{{.Created}}' 2>/dev/null | cut -d. -f1 || echo "")
    NEWEST_FILE=$(find app/ relay/ -type f -newer /tmp/dummy_marker 2>/dev/null | head -1 || true)
    # 更简单的方式：比较 git HEAD 和镜像构建时间
    if [[ -d .git ]]; then
        GIT_CHANGED=$(git status --porcelain app/ relay/ requirements.txt Dockerfile 2>/dev/null | head -1 || true)
        if [[ -n "$GIT_CHANGED" ]]; then
            NEED_REBUILD=true
            info "检测到代码变更，将重新构建"
        fi
    fi
    # 如果 git pull 后代码变了但 git status 是 clean 的，对比镜像时间和最新 commit
    if ! $NEED_REBUILD && [[ -d .git ]]; then
        LAST_COMMIT_TIME=$(git log -1 --format='%ct' 2>/dev/null || echo 0)
        IMAGE_TIMESTAMP=$(docker inspect newapi-tokens-newapi-monitor:latest --format '{{.Created}}' 2>/dev/null | python3 -c "import sys; from datetime import datetime; print(int(datetime.fromisoformat(sys.stdin.read().strip().replace('Z','+00:00')).timestamp()))" 2>/dev/null || echo 0)
        if [[ "$LAST_COMMIT_TIME" -gt "$IMAGE_TIMESTAMP" ]]; then
            NEED_REBUILD=true
            info "检测到 git pull 后有新提交，将重新构建"
        fi
    fi
fi

if $NEED_REBUILD; then
    log "构建 Docker 镜像..."
    docker compose build --no-cache newapi-monitor 2>&1 | tail -5
    docker compose build --no-cache tg-relay 2>&1 | tail -3
else
    log "代码无变化，跳过构建"
fi

# ── 启动容器（--force-recreate 确保新的 .env 生效） ──
log "启动容器..."
docker compose up -d --force-recreate 2>&1 | tail -10

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
echo "  追踪分组:  $TRACK_GROUP"
echo "  Telegram: $([[ -n "$TG_BOT_TOKEN" ]] && echo '已配置' || echo '未配置')"
echo ""
echo "  查看日志:  docker logs -f newapi-monitor"
echo "  停止:      cd $SCRIPT_DIR && docker compose down"
echo ""

# ── 自动同步渠道 ──
log "自动同步渠道..."
COOKIE_JAR=$(mktemp)
SYNC_OK=false

# 登录拿 session cookie
LOGIN_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -c "$COOKIE_JAR" \
    -X POST http://127.0.0.1:9217/api/login \
    -H 'Content-Type: application/json' \
    -d "{\"password\":\"$ADMIN_PASSWORD\"}" 2>/dev/null || echo "000")

if [[ "$LOGIN_HTTP" == "200" ]]; then
    # 用 cookie 调同步
    SYNC_HTTP=$(curl -s -o /tmp/channels_sync_resp.json -w "%{http_code}" -b "$COOKIE_JAR" \
        -X POST http://127.0.0.1:9217/api/channels/sync 2>/dev/null || echo "000")

    if [[ "$SYNC_HTTP" == "200" ]]; then
        SYNC_OK=true
        # 解析同步结果
        SYNC_ADDED=$(python3 -c "import json; d=json.load(open('/tmp/channels_sync_resp.json')); print(d.get('added',0))" 2>/dev/null || echo "?")
        SYNC_TOTAL=$(python3 -c "import json; d=json.load(open('/tmp/channels_sync_resp.json')); print(d.get('total',0))" 2>/dev/null || echo "?")
        log "渠道同步成功 ✓  新增 $SYNC_ADDED 个，共 $SYNC_TOTAL 个渠道"
    elif [[ "$SYNC_HTTP" == "400" ]]; then
        warn "渠道同步失败: 未配置 NewAPI 控制凭据"
        warn "请打开仪表盘 → 设置页面，配置 newapi_control_url / newapi_control_token / newapi_control_user"
    else
        warn "渠道同步失败 (HTTP $SYNC_HTTP)，请稍后在仪表盘手动同步"
        cat /tmp/channels_sync_resp.json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('detail',''))" 2>/dev/null || true
    fi

    # 如果同步成功，拉取渠道列表打印
    if $SYNC_OK; then
        echo ""
        info "已同步的渠道列表:"
        curl -sf -b "$COOKIE_JAR" http://127.0.0.1:9217/api/channels 2>/dev/null \
            | python3 -c "
import sys, json
channels = json.load(sys.stdin)
if not channels:
    print('  (无渠道)')
else:
    for ch in channels:
        cid = ch.get('id','?')
        name = ch.get('name','') or ''
        rate = ch.get('rate', 0) or 0
        print(f'  渠道 {cid:>4}  {name:<20}  倍率 {rate:.4f}')
" 2>/dev/null || warn "无法获取渠道列表"
        echo ""
    fi
else
    warn "登录失败 (HTTP $LOGIN_HTTP)，跳过渠道同步"
    warn "请稍后打开仪表盘手动同步渠道"
fi

rm -f "$COOKIE_JAR" /tmp/channels_sync_resp.json 2>/dev/null || true

# ── 配置 webhook（如果 Telegram 已配置） ──
if [[ -n "$TG_BOT_TOKEN" ]] && [[ -n "$TG_CHAT_ID" ]]; then
    log "配置 Telegram webhook..."
    # 登录拿 session cookie
    COOKIE_JAR2=$(mktemp)
    curl -s -o /dev/null -c "$COOKIE_JAR2" \
        -X POST http://127.0.0.1:9217/api/login \
        -H 'Content-Type: application/json' \
        -d "{\"password\":\"$ADMIN_PASSWORD\"}" 2>/dev/null || true
    if [[ -s "$COOKIE_JAR2" ]]; then
        # 用 cookie 配置 webhook
        curl -sf -X POST http://127.0.0.1:9217/api/settings/webhook \
            -H 'Content-Type: application/json' \
            -b "$COOKIE_JAR2" \
            -d '{"url":"http://newapi-tg-relay:9218/","push_hourly":true,"push_daily":true,"push_error":true}' \
            >/dev/null 2>&1 || true
        log "Telegram webhook 已配置 ✓"
    else
        warn "Telegram webhook 配置失败: 无法登录"
    fi
    rm -f "$COOKIE_JAR2" 2>/dev/null || true
fi
