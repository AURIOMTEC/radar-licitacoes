// pm2: pm2 start ecosystem.config.cjs && pm2 save
// A porta e o resto da configuração vêm do .env (lido pelo uvicorn --env-file).
const fs = require("fs");
const path = require("path");

function porta() {
  try {
    const env = fs.readFileSync(path.join(__dirname, ".env"), "utf8");
    const m = env.match(/^PORT=(\d+)/m);
    if (m) return m[1];
  } catch (_) {}
  return "5002";
}

module.exports = {
  apps: [
    {
      name: "radar-licitacoes",
      cwd: __dirname,
      script: ".venv/bin/uvicorn",
      args: `app.main:app --host 127.0.0.1 --port ${porta()} --env-file .env --proxy-headers --workers 1`,
      interpreter: "none",
      autorestart: true,
      max_memory_restart: "400M",
      time: true,
    },
  ],
};
