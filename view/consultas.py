"""Rotas das consultas (/api/consultas): listar, agendar, alterar, cancelar, avaliar e a videochamada.

No banco a consulta se chama SESSAO. Os status possíveis são: AGENDADO (horário reservado, esperando o
pagamento), CONFIRMADO (paga), REALIZADO (aconteceu) e CANCELADO.
"""
from flask import Blueprint, jsonify, request

from services import consultas_service
from util.usuarios import profissional_autenticado, usuario_autenticado

consultas_bp = Blueprint('consultas', __name__, url_prefix='/api/consultas')


@consultas_bp.route('/', methods=['GET'])
@usuario_autenticado
def consultas(dados_usuario):
    """GET /api/consultas/: as consultas do paciente ou do profissional logado (o admin vê todas)."""
    resultados = consultas_service.listar(dados_usuario.get('id_usuario'), dados_usuario.get('usuario_role'))
    return jsonify({'message': 'Consultas obtidas com sucesso', 'consultas': resultados}), 200


@consultas_bp.route('/', methods=['POST'])
@usuario_autenticado
def agendar_consulta(dados_usuario):
    """POST /api/consultas/ com {id_profissional, timestamp_inicio, timestamp_fim}: o paciente reserva um horário."""
    sessao = consultas_service.agendar(dados_usuario.get('id_usuario'), dados_usuario.get('usuario_role'),
                                       request.get_json(silent=True))
    return jsonify({'message': 'Sessao criada com sucesso', 'sessao': sessao}), 201


@consultas_bp.route('/<int:sessao_id>', methods=['PATCH'])
@profissional_autenticado
def atualizar_consulta(dados_usuario, sessao_id):
    """PATCH /api/consultas/<id>: o profissional remarca, troca o link da reunião ou muda o status."""
    sessao = consultas_service.atualizar(dados_usuario.get('id_usuario'), sessao_id, request.get_json(silent=True))
    return jsonify({'message': 'Sessao atualizada com sucesso', 'sessao': sessao})


@consultas_bp.route('/<int:sessao_id>/cancelar', methods=['POST'])
@usuario_autenticado
def cancelar_consulta(dados_usuario, sessao_id):
    """POST /api/consultas/<id>/cancelar: o paciente cancela (até 15 min antes), com reembolso do que pagou."""
    resultado = consultas_service.cancelar(dados_usuario.get('id_usuario'), dados_usuario.get('usuario_role'),
                                           sessao_id)
    return jsonify({'message': 'Consulta cancelada', **resultado})


@consultas_bp.route('/<int:sessao_id>/ice-servers', methods=['GET'])
@usuario_autenticado
def ice_servers(dados_usuario, sessao_id):
    """GET /api/consultas/<id>/ice-servers: os servidores STUN/TURN que a videochamada usa para conectar."""
    servidores = consultas_service.ice_servers(dados_usuario.get('id_usuario'), sessao_id)
    return jsonify({'ice_servers': servidores})


@consultas_bp.route('/<int:sessao_id>/avaliacao', methods=['POST'])
@usuario_autenticado
def avaliar_consulta(dados_usuario, sessao_id):
    """POST /api/consultas/<id>/avaliacao com {nota, comentario, publica}: o paciente avalia a consulta."""
    avaliacao = consultas_service.avaliar(dados_usuario.get('id_usuario'), dados_usuario.get('usuario_role'),
                                          sessao_id, request.get_json(silent=True))
    return jsonify({'message': 'Sessao avaliada com sucesso', 'avaliacao': avaliacao}), 201
