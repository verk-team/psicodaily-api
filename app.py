"""Ponto de entrada da API: cria o app Flask, liga as rotas e trata os erros.

Cada domínio (consultas, pagamentos, chat...) tem um Blueprint em view/<dominio>.py. Blueprint é um
grupo de rotas do Flask que pode ser registrado no app de uma vez.
"""
import os

from flask import Flask, jsonify, request
from flask.cli import load_dotenv
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from config import ORIGENS_FRONT
from database.db import banco_responde
from extensions.websocket import sock
from services import lembretes_service
from services.auditoria_service import IP_ATUAL
from services.erros import ErroNegocio
from view.admin import admin_bp
from view.artigos import artigos_bp
from view.assinaturas import assinaturas_bp
from view.assistente import assistente_bp
from view.auth import auth_bp
from view.chat import chat_bp
from view.consultas import cons_bp
from view.dashboard import dashboard_bp
from view.encaminhamentos import encam_bp
from view.favoritos import favoritos_bp
from view.lista_espera import lista_espera_bp
from view.notificacoes import notificacoes_bp
from view.pagamentos import pagamentos_bp
from view.profissionais import prof_bp
from view.prontuario import prontuario_bp
from view.registros import registros_bp
from view.signaling import signaling_bp
from view.usuarios import usuarios_bp
from view.whatsapp import whatsapp_bp

load_dotenv()

app = Flask(__name__)
sock.init_app(app)

BLUEPRINTS = (
    signaling_bp,
    auth_bp,
    usuarios_bp,
    registros_bp,
    prontuario_bp,
    prof_bp,
    cons_bp,
    encam_bp,
    pagamentos_bp,
    artigos_bp,
    favoritos_bp,
    admin_bp,
    notificacoes_bp,
    lista_espera_bp,
    dashboard_bp,
    chat_bp,
    assistente_bp,
    whatsapp_bp,
    assinaturas_bp,
)
for blueprint in BLUEPRINTS:
    app.register_blueprint(blueprint)

app.config.from_pyfile("config.py")

# Tarefas automáticas (lembretes 24h e 1h antes da consulta, cobranças da assinatura) rodam numa thread
# deste mesmo processo, a cada minuto. JOBS=0 desliga: os testes chamam essas funções na mão.
if os.getenv('JOBS', '1') == '1':
    lembretes_service.iniciar()


@app.before_request
def guardar_ip():
    """Guarda o IP de quem fez a requisição, para a auditoria registrar quem fez cada ação."""
    # Atrás do proxy do Northflank, o IP real do cliente é o último do cabeçalho X-Forwarded-For
    IP_ATUAL.set(request.access_route[-1] if request.access_route else request.remote_addr)


@app.route('/api/saude')
def saude():
    """GET /api/saude: diz se a API e o banco estão no ar.

    O Northflank chama esta rota (readiness probe) e só manda tráfego para o servidor quando ela responde 200,
    ou seja, depois que o banco migrou e está respondendo.
    """
    if banco_responde():
        return jsonify({'status': 'ok'})
    return jsonify({'error': 'Banco indisponivel'}), 503


@app.errorhandler(ErroNegocio)
def erro_negocio(erro):
    """Os serviços levantam ErroNegocio quando uma regra é violada; aqui ele vira a resposta JSON de erro."""
    return jsonify({'error': erro.mensagem, **erro.extra}), erro.status


@app.errorhandler(Exception)
def erro_interno(erro):
    """Qualquer outra exceção: grava no log e responde um 500 genérico, sem vazar detalhes para o cliente."""
    # Erros HTTP do próprio Flask (404 de rota que não existe, 405...) seguem como estão
    if isinstance(erro, HTTPException):
        return erro
    app.logger.exception('Erro nao tratado')
    return jsonify({'error': 'Internal server error'}), 500


# CORS: o navegador só deixa o front (outro domínio) chamar a API se ela autorizar a origem dele.
# supports_credentials libera o envio do cookie de login.
CORS(
    app,
    supports_credentials=True,
    origins=ORIGENS_FRONT
)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
