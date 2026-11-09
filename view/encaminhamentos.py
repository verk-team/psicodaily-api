"""Rotas dos encaminhamentos (/api/encaminhamentos): o profissional encaminha um paciente que ele atende."""
from flask import Blueprint, jsonify, request

from services import encaminhamentos_service
from util.usuarios import profissional_autenticado

encaminhamentos_bp = Blueprint('encaminhamentos', __name__, url_prefix='/api/encaminhamentos')


@encaminhamentos_bp.route('/', methods=['POST'])
@profissional_autenticado
def criar_encaminhamento(dados_usuario):
    """POST /api/encaminhamentos/: cria um encaminhamento para um paciente do profissional logado."""
    encaminhamento = encaminhamentos_service.criar(dados_usuario.get('id_usuario'), request.get_json(silent=True))
    return jsonify({
        'message': 'Encaminhamento criado com sucesso',
        'encaminhamento': encaminhamento
    }), 201


@encaminhamentos_bp.route('/', methods=['GET'])
@profissional_autenticado
def encaminhamentos(dados_usuario):
    """GET /api/encaminhamentos/: os encaminhamentos feitos pelo profissional logado."""
    return jsonify({
        'message': 'Encaminhamentos obtidos com sucesso',
        'encaminhamentos': encaminhamentos_service.listar(dados_usuario['id_usuario'])
    }), 200
