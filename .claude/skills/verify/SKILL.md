---
name: verify
description: Receita para rodar API + front localmente no Windows e verificar features pela tela (Edge headless via Playwright), sem tocar no banco de dev nem em serviços pagos.
---

# Verificar features rodando (Windows)

1. Banco novo com dados fictícios (não use o `.FDB` de dev: ele tem e-mails reais):
   `DB_DATABASE=<scratch>\VERIFY.FDB .venv/Scripts/python database/migrar.py --criar --seed database/seed.sql`
   Senha de todos os usuários do seed: `Senha@123` (22 psicóloga, 23 e 25 pacientes, 24 psiquiatra, 26 admin).
2. API com saídas externas desligadas: `SMTP_USER= SMTP_PASSWORD= JOBS=0 ARKHE_BASE_URL=http://127.0.0.1:9`
   (sem cobrança real na Arkhé). `ENDPOINT_URL` do `.env` aponta para o R2 real: não teste envio de arquivo.
3. As portas 5173 e 8080 (origens de `config.ORIGENS_FRONT`) costumam estar ocupadas. Sem origem permitida,
   os WebSockets falham com "Invalid frame header". Suba a API por um lançador fora do repo:
   ```python
   import os, sys; os.chdir(API); sys.path.insert(0, API)
   import config; config.ORIGENS_FRONT.append("http://localhost:5199")
   from app import app; app.run(host="127.0.0.1", port=5000, threaded=True)
   ```
   Front: `VITE_API_PROXY_TARGET=http://127.0.0.1:5000 npx vite --port 5199 --strictPort` (encerrar o `node.exe` no fim: matar o bash não mata o vite).
4. Navegador: `pip install playwright` num venv do scratchpad e `p.chromium.launch(channel="msedge")` (usa o Edge instalado, sem download).
   Vídeo: `args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]`, dois contextos, clicar "Iniciar chamada".
   O `full_page` não pega a página inteira (o AppLayout rola por dentro): leia o texto com `inner_text`.
5. WhatsApp: WAHA falso (servidor HTTP que grava o corpo de `POST /api/sendText`) + `WAHA_URL`, `WAHA_HMAC_KEY` na API;
   webhook assinado com `hmac.new(chave, corpo, sha512).hexdigest()` no header `X-Webhook-Hmac`, `from` = `55` + telefone do seed + `@c.us`.

Gotchas: a sala de vídeo abre 15 min antes do início (crie uma janela de agenda começando daqui a ~10 min);
o paciente não cancela a menos de 15 min; a busca não tem `<a>` nos cards (conte por "Valor da sessão:").
