#!/usr/bin/env bash
# Deploy do Radar de Licitações na VPS — chamado pelo webhook (push no main)
# ou à mão:  cd /var/www/radar && nohup bash scripts/deploy-vps.sh > /tmp/radar-deploy.log 2>&1 &
#
# Não toca em banco de outros apps: o Radar usa só o SQLite em ./data (caminho no .env).
set -e
cd "$(dirname "$0")/.."
APP=radar-licitacoes

echo "== $(date '+%F %T') deploy $APP"
git pull origin main

# Python: venv próprio do app (python3 do Ubuntu 22.04 = 3.10)
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

# Versão exibida no rodapé (v1.NNNN = nº de commits)
printf 'v1.%04d\n' "$(git rev-list --count HEAD)" > VERSION
echo "versão: $(cat VERSION)"

# "Migração": o schema é só CREATE ... IF NOT EXISTS (nunca apaga dados).
# Roda aqui para falhar o deploy cedo se algo estiver errado.
.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv('.env'); from app import db; db.init(); print('banco ok')"

if pm2 describe "$APP" > /dev/null 2>&1; then
  pm2 restart "$APP" --update-env
else
  pm2 start ecosystem.config.cjs
  pm2 save
fi
echo "== deploy concluído"
