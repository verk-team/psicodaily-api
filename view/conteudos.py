from flask import Blueprint, jsonify, request
from services import conteudos_service
from util.usuarios import profissional_autenticado

conteudos_bp = Blueprint('conteudos', __name__, url_prefix='/api/conteudos')
MAXIMO_DESTAQUES = 12


@conteudos_bp.route('/destaques', methods=['GET'])
def destaques():
    try:
        limite = int(request.args.get('limite', MAXIMO_DESTAQUES))
        if not 1 <= limite <= MAXIMO_DESTAQUES:
            raise ValueError()
    except ValueError:
        return jsonify({'error': f'limite deve ser de 1 a {MAXIMO_DESTAQUES}'}), 400
    return jsonify({'conteudos': conteudos_service.destaques(limite)}), 200


@conteudos_bp.route('/', methods=['POST'])
@profissional_autenticado
def criar(dados_usuario):
    conteudo = conteudos_service.criar(dados_usuario['id_usuario'], request.get_json(silent=True))
    return jsonify({'conteudo': conteudo}), 201


@conteudos_bp.route('/<int:conteudo_id>', methods=['PATCH'])
@profissional_autenticado
def atualizar(dados_usuario, conteudo_id):
    conteudo = conteudos_service.atualizar(dados_usuario['id_usuario'], conteudo_id, request.get_json(silent=True))
    return jsonify({'conteudo': conteudo}), 200


@conteudos_bp.route('/<int:conteudo_id>', methods=['DELETE'])
@profissional_autenticado
def apagar(dados_usuario, conteudo_id):
    conteudos_service.apagar(dados_usuario['id_usuario'], conteudo_id)
    return jsonify({'message': 'Conteúdo removido'}), 200
