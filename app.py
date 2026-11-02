import os

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from services.erros import ErroNegocio
from services.auditoria_service import IP_ATUAL
from services import lembretes_service
from database.db import banco_responde
from flask.cli import load_dotenv
from view.auth import auth_bp
from view.usuarios import usuarios_bp
# from view.agendamento import agendamento_bp
from view.registros import registros_bp
from view.prontuario import prontuario_bp
from view.profissionais import prof_bp
from view.consultas import cons_bp
from view.encaminhamentos import encam_bp
from view.pagamentos import pagamentos_bp
from view.artigos import artigos_bp
from view.favoritos import favoritos_bp
from view.admin import admin_bp
from view.notificacoes import notificacoes_bp
from view.lista_espera import lista_espera_bp
from view.dashboard import dashboard_bp
from view.chat import chat_bp
from view.assistente import assistente_bp
from view.whatsapp import whatsapp_bp
from view.assinaturas import assinaturas_bp
from flask_cors import CORS
from config import ORIGENS_FRONT
# from pathlib import Path

from extensions.websocket import sock
from view.signaling import signaling_bp

load_dotenv()

app = Flask(__name__)
sock.init_app(app)

app.register_blueprint(signaling_bp)

app.register_blueprint(auth_bp)
app.register_blueprint(usuarios_bp)
app.register_blueprint(registros_bp)
app.register_blueprint(prontuario_bp)
app.register_blueprint(prof_bp)
app.register_blueprint(cons_bp)
app.register_blueprint(encam_bp)
app.register_blueprint(pagamentos_bp)
app.register_blueprint(artigos_bp)
app.register_blueprint(favoritos_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(notificacoes_bp)
app.register_blueprint(lista_espera_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(assistente_bp)
app.register_blueprint(whatsapp_bp)
app.register_blueprint(assinaturas_bp)

app.config.from_pyfile("config.py")

# Lembretes 24h e 1h antes (30) e cobrancas da assinatura (9): thread do processo unico; JOBS=0 desliga (testes)
if os.getenv('JOBS', '1') == '1':
    lembretes_service.iniciar()


@app.before_request
def guardar_ip():
    # O proxy do Northflank acrescenta o IP do cliente no fim do X-Forwarded-For
    IP_ATUAL.set(request.access_route[-1] if request.access_route else request.remote_addr)


@app.route('/api/saude')
def saude():
    # Readiness probe do Northflank: so recebe trafego depois que o banco migrou e responde.
    if banco_responde():
        return jsonify({'status': 'ok'})
    return jsonify({'error': 'Banco indisponivel'}), 503


@app.errorhandler(ErroNegocio)
def erro_negocio(erro):
    return jsonify({'error': erro.mensagem, **erro.extra}), erro.status


@app.errorhandler(Exception)
def erro_interno(erro):
    if isinstance(erro, HTTPException):
        return erro
    app.logger.exception('Erro nao tratado')
    return jsonify({'error': 'Internal server error'}), 500

CORS(
    app,
    supports_credentials=True,
    origins=ORIGENS_FRONT
)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
