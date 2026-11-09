"""Rotas dos artigos (/api/artigos): profissionais escrevem, usuários logados leem e curtem."""
from flask import Blueprint, jsonify, request

from services import artigos_service
from util.usuarios import profissional_autenticado, usuario_autenticado

artigos_bp = Blueprint('artigos', __name__, url_prefix='/api/artigos')
MAXIMO_DESTAQUES = 12


@artigos_bp.route('/', methods=['GET'])
@usuario_autenticado
def listar(dados_usuario):
    """GET /api/artigos/?page=1&page_size=...&autor=<id>: artigos no ar, mais recentes primeiro, sem o texto."""
    return jsonify(artigos_service.listar(dados_usuario['id_usuario'], request.args)), 200


@artigos_bp.route('/destaques', methods=['GET'])
def destaques():
    """GET /api/artigos/destaques?limite=12: artigos para a página inicial, sem login, já com o texto completo."""
    try:
        limite = int(request.args.get('limite', MAXIMO_DESTAQUES))
        if not 1 <= limite <= MAXIMO_DESTAQUES:
            raise ValueError()
    except ValueError:
        return jsonify({'error': f'limite deve ser de 1 a {MAXIMO_DESTAQUES}'}), 400
    return jsonify({'artigos': artigos_service.destaques(limite)}), 200


@artigos_bp.route('/<int:artigo_id>', methods=['GET'])
@usuario_autenticado
def detalhe(dados_usuario, artigo_id):
    """GET /api/artigos/<id>: o artigo completo."""
    return jsonify({'artigo': artigos_service.detalhe(dados_usuario['id_usuario'], artigo_id)}), 200


@artigos_bp.route('/', methods=['POST'])
@profissional_autenticado
def criar(dados_usuario):
    """POST /api/artigos/: o profissional publica um artigo (exige o plano Premium)."""
    artigo = artigos_service.criar(dados_usuario['id_usuario'], request.get_json(silent=True))
    return jsonify({'artigo': artigo}), 201


@artigos_bp.route('/<int:artigo_id>', methods=['PATCH'])
@profissional_autenticado
def atualizar(dados_usuario, artigo_id):
    """PATCH /api/artigos/<id>: o autor edita o artigo."""
    artigo = artigos_service.atualizar(dados_usuario['id_usuario'], artigo_id, request.get_json(silent=True))
    return jsonify({'artigo': artigo}), 200


@artigos_bp.route('/<int:artigo_id>', methods=['DELETE'])
@profissional_autenticado
def apagar(dados_usuario, artigo_id):
    """DELETE /api/artigos/<id>: o autor apaga o artigo."""
    artigos_service.apagar(dados_usuario['id_usuario'], artigo_id)
    return jsonify({'message': 'Artigo removido'}), 200


@artigos_bp.route('/<int:artigo_id>/curtida', methods=['POST', 'DELETE'])
@usuario_autenticado
def curtir(dados_usuario, artigo_id):
    """POST curte e DELETE descurte /api/artigos/<id>/curtida."""
    curtiu = request.method == 'POST'
    return jsonify(artigos_service.curtir(dados_usuario['id_usuario'], artigo_id, curtiu)), 200
