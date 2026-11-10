#!/bin/sh
set -e

# O banco vive num volume (padrao /data; na VPS, /var/lib/firebird/data), fora da imagem: deploy nao apaga dados.
# Ordem: Firebird -> senha -> (banco novo so com CRIAR_BANCO=1) -> backup -> migrations -> gunicorn.
# Qualquer passo que falhe derruba o container antes de ele receber trafego.
: "${DB_DATABASE:=/data/BANCO.FDB}"
: "${DB_HOST:=127.0.0.1}"
: "${DB_USER:=SYSDBA}"
if [ -z "$DB_PASSWORD" ]; then
    echo "AVISO: DB_PASSWORD vazio, usando a senha padrao 'sysdba'. Defina no .env."
    DB_PASSWORD=sysdba
fi
export DB_DATABASE DB_HOST DB_USER DB_PASSWORD

DB_DIR=$(dirname "$DB_DATABASE")
BACKUPS="$DB_DIR/backups"
mkdir -p "$BACKUPS"
chown -R firebird:firebird "$DB_DIR"

BANCO_NOVO=0
if [ ! -f "$DB_DATABASE" ]; then
    if [ "$CRIAR_BANCO" != "1" ]; then
        echo "ERRO: $DB_DATABASE nao existe. Confira o volume; para um ambiente novo, defina CRIAR_BANCO=1."
        exit 1
    fi
    # Modo embarcado (sem DB_HOST), antes de o servidor subir: o CREATE DATABASE remoto
    # recusa a senha inicial da imagem, a conexao local nao precisa dela.
    echo "Criando banco vazio em $DB_DATABASE..."
    DB_HOST= python database/migrar.py --criar --status
    chown firebird:firebird "$DB_DATABASE"
    BANCO_NOVO=1
fi

echo "Starting Firebird..."
/usr/lib/firebird/4.0/bin/firebird -i &

python - <<'EOF'
import socket, time
for _ in range(60):
    try:
        socket.create_connection(("127.0.0.1", 3050), 1).close()
        break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit("Firebird nao abriu a porta 3050 em 30s")
EOF

# A senha inicial do SYSDBA vem da imagem (o security4.fdb nao fica no volume), entao e redefinida a cada boot.
SENHA_IMAGEM=$(grep "^ISC_PASSWORD=" /etc/firebird/4.0/SYSDBA.password | cut -d= -f2)

printf "ALTER USER SYSDBA SET PASSWORD '%s';\nCOMMIT;\n" "$DB_PASSWORD" | \
    /usr/bin/isql-fb -q -user SYSDBA -password "$SENHA_IMAGEM" "127.0.0.1:$DB_DATABASE"

if [ "$BANCO_NOVO" = "0" ]; then
    ARQUIVO="$BACKUPS/antes-$(date +%Y%m%d-%H%M%S).fbk"
    echo "Backup antes das migrations: $ARQUIVO"
    /usr/bin/gbak -b -user SYSDBA -password "$DB_PASSWORD" "127.0.0.1:$DB_DATABASE" "$ARQUIVO"
    # Guarda os 5 mais recentes; o snapshot diario do volume cobre o resto
    ls -1t "$BACKUPS"/*.fbk | tail -n +6 | xargs -r rm --
fi

python database/migrar.py

echo "Starting Gunicorn..."

# gthread e nao gevent: o fdb chama o libfbclient via ctypes, que o gevent nao torna
# cooperativo, entao cada query travava o worker inteiro. 1 worker porque as salas do
# signaling ficam em memoria; cada chamada de video ocupa uma thread enquanto dura.
exec gunicorn \
    --worker-class gthread \
    --threads 32 \
    --bind 0.0.0.0:8000 \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    app:app
