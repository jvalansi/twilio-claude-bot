#!/usr/bin/env bash
# setup.sh — install the Twilio-Claude bot as a systemd service
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="twilio-claude-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="${SCRIPT_DIR}/.env"
BOT_USER="${SUDO_USER:-$(whoami)}"
MEMORY_LIMIT="${MEMORY_LIMIT:-1G}"
CONDA_PYTHON="/home/${BOT_USER}/miniconda3/envs/twilio-claude-bot/bin/python"

# ── helpers ───────────────────────────────────────────────────────────────────
info()  { echo "[info]  $*"; }
warn()  { echo "[warn]  $*" >&2; }
die()   { echo "[error] $*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || die "Run this script with sudo: sudo $0"
}

prompt_var() {
    local var="$1" prompt="$2" current
    current="$(grep -E "^${var}=" "${ENV_FILE}" 2>/dev/null | cut -d= -f2- || true)"
    if [[ -n "$current" ]]; then
        info "${var} already set in .env — skipping."
        return
    fi
    read -rsp "${prompt}: " value; echo
    [[ -n "$value" ]] || die "${var} cannot be empty."
    echo "${var}=${value}" >> "${ENV_FILE}"
}

# ── pre-flight ────────────────────────────────────────────────────────────────
require_root
[[ -f "${CONDA_PYTHON}" ]] || die "Conda env not found at ${CONDA_PYTHON}. Run: conda create -n twilio-claude-bot python=3.11 && pip install -r requirements.txt"

# ── .env setup ────────────────────────────────────────────────────────────────
if [[ ! -f "${ENV_FILE}" ]]; then
    info "Creating ${ENV_FILE}"
    touch "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
fi

info "Configuring credentials (leave blank if already in .env)…"
prompt_var TWILIO_ACCOUNT_SID "TWILIO_ACCOUNT_SID"
prompt_var TWILIO_AUTH_TOKEN  "TWILIO_AUTH_TOKEN"

chown "${BOT_USER}:${BOT_USER}" "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

# ── systemd unit ──────────────────────────────────────────────────────────────
CLAUDE_PATH="/home/${BOT_USER}/.local/bin/claude"

info "Writing ${SERVICE_FILE}"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Twilio voice bot powered by Claude
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
WorkingDirectory=${SCRIPT_DIR}
EnvironmentFile=${ENV_FILE}
Environment=CLAUDE_PATH=${CLAUDE_PATH}
ExecStart=${CONDA_PYTHON} ${SCRIPT_DIR}/bot.py
Restart=on-failure
RestartSec=5s

# Memory limit
MemoryMax=${MEMORY_LIMIT}
MemorySwapMax=0

# Basic hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# ── enable & start ────────────────────────────────────────────────────────────
info "Reloading systemd…"
systemctl daemon-reload

info "Enabling ${SERVICE_NAME}…"
systemctl enable "${SERVICE_NAME}"

info "Starting ${SERVICE_NAME}…"
systemctl restart "${SERVICE_NAME}"

sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "Service is running. Check logs with:"
    info "  journalctl -u ${SERVICE_NAME} -f"
else
    warn "Service did not start cleanly. Check logs with:"
    warn "  journalctl -u ${SERVICE_NAME} -xe"
    exit 1
fi
