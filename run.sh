#!/bin/sh
# Dev local (WSL). Um só processo escutando nas duas portas: as salas do signaling
# ficam em memória, então dois processos separados quebrariam a videochamada.
cd "$(dirname "$0")"
# Como o entrypoint do Docker: sem isso, código com coluna nova roda num banco antigo e dá 500
.venv/bin/python database/migrar.py || exit 1
exec .venv/bin/gunicorn \
    --worker-class gthread \
    --threads 32 \
    --bind 127.0.0.1:8000 \
    --bind 127.0.0.1:8001 \
    app:app
