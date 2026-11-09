"""Rotas das notificações do usuário (/api/notificacoes): o sininho do site."""
from flask import Blueprint, jsonify, request

from services import notificacoes_service
from util.rotas import MAIOR_ID, sem_cache
from util.usuarios import usuario_autenticado

notificacoes_bp = Blueprint('notificacoes', __name__, url_prefix='/api/notificacoes')


@notificacoes_bp.route('/', methods=['GET'])
@usuario_autenticado
def listar(dados_usuario):
    """GET /api/notificacoes/?limite=20&antes=<id>: as notificações, das mais novas para as mais antigas."""
    try:
        limite = int(request.args.get('limite', 20))
        antes = int(request.args.get('antes', MAIOR_ID))
        if not 1 <= limite <= 100 or antes < 1:
            raise ValueError()
    except ValueError:
        return jsonify({'error': 'Paginacao invalida'}), 400
    return sem_cache(jsonify(notificacoes_service.listar(dados_usuario['id_usuario'], limite, antes)))


@notificacoes_bp.route('/lidas', methods=['POST'])
@usuario_autenticado
def marcar_lidas(dados_usuario):
    """POST /api/notificacoes/lidas com {"ids": [...]}: marca essas como lidas; sem ids, marca todas."""
    total = notificacoes_service.marcar_lidas(dados_usuario['id_usuario'], request.get_json(silent=True) or {})
    return jsonify({'message': 'Notificacoes marcadas como lidas', 'atualizadas': total})
