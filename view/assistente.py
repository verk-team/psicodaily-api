"""Rota do assistente virtual (/api/assistente), o "Psicosinho", que roda no Gemini.

O assistente nunca grava nada: para agendar ou cancelar ele devolve uma proposta, e o front pede a
confirmação do paciente antes de chamar a rota de verdade.
"""
from flask import Blueprint, jsonify, request

from services import assistente_service
from util.usuarios import usuario_autenticado

assistente_bp = Blueprint('assistente', __name__, url_prefix='/api/assistente')


@assistente_bp.route('/', methods=['POST'])
@usuario_autenticado
def conversar(dados_usuario):
    """POST /api/assistente/ com {"mensagens": [...]}: a resposta do assistente e as propostas, se houver."""
    resultado = assistente_service.conversar(dados_usuario.get('id_usuario'), dados_usuario.get('usuario_role'),
                                             request.get_json(silent=True))
    return jsonify(resultado)
