"""Rotas da lista de espera (/api/lista-espera).

Sem horário livre, o paciente entra na lista de um profissional. Quando um horário é liberado (consulta
cancelada ou reserva não paga), o primeiro da fila para quem o horário serve é avisado. A vaga não fica
reservada: quem agendar primeiro leva.
"""
from flask import Blueprint, jsonify, request

from services import lista_espera_service
from util.rotas import sem_cache
from util.usuarios import usuario_autenticado

lista_espera_bp = Blueprint('lista_espera', __name__, url_prefix='/api/lista-espera')


@lista_espera_bp.route('/', methods=['GET'])
@usuario_autenticado
def listar(dados_usuario):
    """GET /api/lista-espera/: as entradas do usuário logado que ainda valem."""
    entradas = lista_espera_service.listar(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'))
    return sem_cache(jsonify({'lista_espera': entradas}))


@lista_espera_bp.route('/', methods=['POST'])
@usuario_autenticado
def entrar(dados_usuario):
    """POST /api/lista-espera/: o paciente entra na lista de um profissional."""
    entrada = lista_espera_service.entrar(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'),
                                         request.get_json(silent=True))
    return jsonify({'message': 'Voce entrou na lista de espera', 'lista_espera': entrada}), 201


@lista_espera_bp.route('/<int:lista_espera_id>', methods=['DELETE'])
@usuario_autenticado
def sair(dados_usuario, lista_espera_id):
    """DELETE /api/lista-espera/<id>: o paciente sai da lista."""
    lista_espera_service.sair(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'), lista_espera_id)
    return jsonify({'message': 'Voce saiu da lista de espera'})
