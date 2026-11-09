"""Rota da tela inicial do app (/api/dashboard)."""
from flask import Blueprint, jsonify

from services import dashboard_service
from util.rotas import sem_cache
from util.usuarios import usuario_autenticado

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/api/dashboard')


@dashboard_bp.route('/', methods=['GET'])
@usuario_autenticado
def montar(dados_usuario):
    """GET /api/dashboard/: tudo que a tela inicial mostra numa chamada só, conforme o papel do usuário."""
    return sem_cache(jsonify(dashboard_service.montar(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'))))
