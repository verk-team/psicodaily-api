"""Rota do prontuário (/api/prontuario): o diário de humor e sono de um paciente, visto pelo profissional.

Só abre para o profissional que tem uma consulta (não cancelada) com o paciente, e todo acesso fica
na auditoria.
"""
from flask import Blueprint, jsonify, request

from services import prontuario_service
from util.rotas import MAIOR_ID, sem_cache
from util.usuarios import profissional_autenticado

prontuario_bp = Blueprint('prontuario', __name__, url_prefix='/api/prontuario')


@prontuario_bp.route('/<int:paciente_id>', methods=['GET'])
@profissional_autenticado
def prontuario(dados_usuario, paciente_id):
    """GET /api/prontuario/<paciente_id>?dias=30&antes=<id>: resumo do período (7, 30 ou 90 dias) e os registros."""
    profissional_id = dados_usuario.get('id_usuario')
    if not profissional_id:
        return jsonify({'error': 'Usuario nao autenticado'}), 401
    try:
        dias = int(request.args.get('dias', 30))
        antes = int(request.args.get('antes', MAIOR_ID))
        if dias not in (7, 30, 90) or antes < 1:
            raise ValueError()
    except ValueError:
        return jsonify({'error': 'Periodo ou paginacao invalida'}), 400

    return sem_cache(jsonify(prontuario_service.prontuario(profissional_id, paciente_id, dias, antes)))
