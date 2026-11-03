# Entrada do Radar no webhook do ERP

O `webhook.cjs` fica no repositório **AURIOMTEC/ERP** e só faz deploy dos repositórios
listados no mapa `TARGETS`. Para o Radar entrar no deploy automático, acrescente uma
entrada no mesmo molde da do site (`deploySite`), apontando para:

- chave: `auriomtec/radar-licitacoes`
- diretório: `/var/www/radar`
- comando: `bash scripts/deploy-vps.sh`

Essa alteração é feita pelo chat do **ERP+Site**, que conhece o `webhook.cjs`.

No GitHub do `AURIOMTEC/radar-licitacoes`: Settings → Webhooks → Add webhook
- Payload URL: `http://erp.auriomtec.com.br:9000/webhook`
- Content type: `application/json`
- Secret: o mesmo `WEBHOOK_SECRET` do `/var/www/erp/.env` (copiar na VPS; não colar no chat)
- Eventos: só **push**

Enquanto o webhook não estiver ligado, o deploy é manual:

    cd /var/www/radar && nohup bash scripts/deploy-vps.sh > /tmp/radar-deploy.log 2>&1 &
    tail -3 /tmp/radar-deploy.log; pm2 list
