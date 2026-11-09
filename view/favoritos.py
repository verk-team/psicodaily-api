"""Rotas dos profissionais favoritos do paciente (/api/favoritos)."""
from flask import Blueprint, jsonify

from services import favoritos_service
from util.rotas import sem_cache
from util.usuarios import usuario_autenticado

favoritos_bp = Blueprint('favoritos', __name__, url_prefix='/api/favoritos')


@favoritos_bp.route('/', methods=['GET'])
@usuario_autenticado
def listar(dados_usuario):
    """GET /api/favoritos/: os favoritos do paciente logado."""
    favoritos = favoritos_service.listar(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'))
    return sem_cache(jsonify({'favoritos': favoritos})), 200


@favoritos_bp.route('/<int:profissional_id>', methods=['POST'])
@usuario_autenticado
def favoritar(dados_usuario, profissional_id):
    """POST /api/favoritos/<profissional_id>: 201 se favoritou agora, 200 se ele já era favorito."""
    criado = favoritos_service.favoritar(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'),
                                         profissional_id)
    return jsonify({'message': 'Profissional favoritado'}), 201 if criado else 200


@favoritos_bp.route('/<int:profissional_id>', methods=['DELETE'])
@usuario_autenticado
def desfavoritar(dados_usuario, profissional_id):
    """DELETE /api/favoritos/<profissional_id>: tira dos favoritos."""
    favoritos_service.desfavoritar(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'), profissional_id)
    return jsonify({'message': 'Profissional removido dos favoritos'}), 200
