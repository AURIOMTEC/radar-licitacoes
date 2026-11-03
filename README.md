# Radar de Licitações — Auriomtec

Você digita o que vende e vê **quem está querendo comprar agora** em todo o Brasil.
Fonte: PNCP (Portal Nacional de Contratações Públicas), API pública e gratuita.

- Coleta todos os editais com propostas abertas (Brasil inteiro), **de madrugada**
- Baixa os **itens** de cada edital: a busca olha o objeto **e** a descrição dos itens
- Busca sem acento/plural: `inversor frequencia`, `"soft starter"`, `A OU B`, `-exclui`
- Filtros por UF, modalidade e faixa de valor; exporta CSV (abre direto no Excel)
- **Buscas salvas com alerta no WhatsApp** (Evolution API ou Z-API), só dos editais novos
- Python (FastAPI) + SQLite próprio. Não usa o Postgres do ERP. Roda no pm2 como os outros apps.
- Versão no rodapé (`v1.NNNN` = número de commits)

## Publicação (mesmo caminho do ERP e do site)

`git push` no `main` → webhook da VPS → `scripts/deploy-vps.sh` → `pm2 restart radar-licitacoes`.

### Instalação na VPS (uma vez)

1. Conferir o ambiente:
   ```
   python3 --version; dpkg -l python3-venv | tail -1; pm2 -v
   ss -tlnp | grep LISTEN
   ls /etc/nginx/sites-enabled
   ```
2. Se faltar o venv: `apt install -y python3-venv`
3. Clonar e configurar:
   ```
   git clone git@github.com:AURIOMTEC/radar-licitacoes.git /var/www/radar
   cd /var/www/radar && cp .env.example .env && nano .env
   ```
   No `.env`, defina `PORT` (livre, padrão 5002), `APP_USER`, `APP_PASSWORD` e `APP_PUBLIC_URL`.
4. Primeiro deploy (cria o venv, grava a versão, cria o banco e sobe no pm2):
   ```
   cd /var/www/radar && bash scripts/deploy-vps.sh
   ```
5. nginx + HTTPS: `deploy/nginx-radar.conf` → `/etc/nginx/sites-available/radar`, link em
   `sites-enabled`, `nginx -t && systemctl reload nginx`, DNS do subdomínio apontando para a VPS,
   `certbot --nginx -d licitacoes.auriomtec.com.br`.
6. Deploy automático: ver `deploy/webhook-target.md`.

### Deploy manual (quando o webhook falhar)

    cd /var/www/radar && nohup bash scripts/deploy-vps.sh > /tmp/radar-deploy.log 2>&1 &
    tail -3 /tmp/radar-deploy.log; pm2 list

## Carga na VPS (2 núcleos)

| O quê | Quando | Peso |
|---|---|---|
| Lista nacional de editais + purga | 1x por noite, dentro de `JANELA_COLETA` (01:30–05:30) | ~400 páginas de 50 só de pregão eletrônico (medido em 03/11/2026: 17,7 mil abertos); todas as modalidades devem passar de 1.000 requisições, ~10–15 min a 2 req/s |
| Itens dos editais | mesma janela, lotes de 300 com pausa de 20 s, 2 em paralelo, 2 req/s, até a janela fechar | 1 requisição por edital novo |
| Alertas WhatsApp | 1x por dia em `ALERTA_HORARIO` (07:00) | consultas no SQLite |
| Busca na tela | quando alguém busca | FTS5 (índice), milissegundos |

- **Nada roda ao subir o app.** O agendador pergunta ao banco (tabela `coletas`) se a tarefa já
  rodou nesta janela, então reiniciar não repete trabalho.
- Na **primeira noite** a fila de itens é grande (todos os editais abertos do país). A 2 req/s
  ela pode levar mais de uma noite. Enquanto isso, a busca já funciona pelo objeto do edital.
- Os botões da tela **Coleta** existem para uso pontual: "Atualizar editais" e "Baixar 1 lote de itens".

## Rodar no PC (Windows)

```powershell
py -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # deixe APP_USER vazio e COLETA_AUTOMATICA=0 se quiser só testar
uvicorn app.main:app --port 5002 --env-file .env
```

## Testes

    python tests/test_pipeline.py

Simula a API do PNCP no formato dos manuais: paginação, itens, erro 500, encerrados, retificação,
busca, alertas sem repetição e agendador com relógio simulado.

## WhatsApp

| Opção | Custo | Observação |
|---|---|---|
| Evolution API | grátis (self-hosted) | usa um número conectado por QR Code |
| Z-API | mensalidade por instância | hospedado |

Teste em **Alertas → Testar envio**. Ao criar uma busca salva, o que já está aberto é marcado como
visto. Marque "Enviar agora também os já abertos" se quiser receber esses editais também.

## Endpoints do PNCP usados

| Etapa | Endpoint |
|---|---|
| Editais abertos | `GET /api/consulta/v1/contratacoes/proposta?dataFinal=AAAAMMDD&codigoModalidadeContratacao=N&pagina=P&tamanhoPagina=50` |
| Itens | `GET /api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/itens` |
| Link público | `https://pncp.gov.br/app/editais/{cnpj}/{ano}/{sequencial}` |

### Comportamento real da API (medido na VPS em 03/11/2026, difere do manual)
- `tamanhoPagina` máximo **50** (100 e 500 → 400 "Tamanho de página inválido").
- `dataFinal` é o **limite de encerramento** das propostas: com a data de hoje só vêm as que
  encerram hoje. O app usa hoje + `DATA_FINAL_DIAS` (730).

### Limites
- Só cobre o que foi publicado no PNCP. Estatais com portal próprio e municípios que publicam só no
  portal local ficam de fora.
- A descrição varia entre órgãos: salve buscas com sinônimos (`inversor OU "conversor de frequencia"`).
- Itens com orçamento sigiloso aparecem sem valor.
