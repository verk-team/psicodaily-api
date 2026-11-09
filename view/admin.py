"""Rotas do painel do administrador (/api/admin): métricas, auditoria, reembolsos e repasses.

Todas exigem login de ADMIN (`@admin_autenticado`).
"""
from flask import Blueprint, jsonify, request

from services import admin_service, financeiro_service
from util.rotas import MAIOR_ID, sem_cache
from util.usuarios import admin_autenticado

admin_bp = Blueprint('admin', __name__, url_prefix='/api/admin')


@admin_bp.route('/metricas', methods=['GET'])
@admin_autenticado
def metricas(dados_usuario):
    """GET /api/admin/metricas?dias=30: números da plataforma no período."""
    return sem_cache(jsonify(admin_service.metricas(request.args.get('dias', '30'))))


@admin_bp.route('/auditoria', methods=['GET'])
@admin_autenticado
def auditoria(dados_usuario):
    """GET /api/admin/auditoria: registro de quem fez o quê, do mais novo para o mais antigo.

    Filtros opcionais: limite (1 a 200), antes (id para paginar), usuario_id e acao.
    """
    try:
        limite = int(request.args.get('limite', 50))
        antes = int(request.args.get('antes', MAIOR_ID))
        usuario_id = int(request.args['usuario_id']) if request.args.get('usuario_id') else None
        if not 1 <= limite <= 200 or antes < 1:
            raise ValueError()
    except ValueError:
        return jsonify({'error': 'Paginacao ou usuario_id invalido'}), 400
    acao = request.args.get('acao') or None
    return sem_cache(jsonify(admin_service.auditoria(limite, antes, usuario_id, acao)))


@admin_bp.route('/reembolsos', methods=['GET'])
@admin_autenticado
def reembolsos(dados_usuario):
    """GET /api/admin/reembolsos?status=PENDENTE: reembolsos a pagar (ou pagos) aos pacientes."""
    status = request.args.get('status') or None
    return sem_cache(jsonify({'reembolsos': financeiro_service.listar_reembolsos(status)}))


@admin_bp.route('/reembolsos/<int:reembolso_id>/pagar', methods=['POST'])
@admin_autenticado
def pagar_reembolso(dados_usuario, reembolso_id):
    """POST /api/admin/reembolsos/<id>/pagar: paga pelo Pix da Arkhé, ou só marca como pago com {"manual": true}."""
    reembolso = financeiro_service.pagar_reembolso(dados_usuario.get('id_usuario'), reembolso_id,
                                                   request.get_json(silent=True))
    return jsonify({'message': 'Reembolso pago', 'reembolso': reembolso})


@admin_bp.route('/repasses', methods=['GET'])
@admin_autenticado
def repasses(dados_usuario):
    """GET /api/admin/repasses?status=PENDENTE: repasses de dinheiro aos profissionais."""
    status = request.args.get('status') or None
    return sem_cache(jsonify({'repasses': financeiro_service.listar_repasses(status)}))


@admin_bp.route('/repasses', methods=['POST'])
@admin_autenticado
def fechar_repasses(dados_usuario):
    """POST /api/admin/repasses: cria um repasse por profissional com tudo que ele já tem a receber."""
    criados = financeiro_service.fechar_repasses(dados_usuario.get('id_usuario'))
    return jsonify({'message': 'Repasses criados', 'repasses': criados}), 201


@admin_bp.route('/repasses/<int:repasse_id>/pagar', methods=['POST'])
@admin_autenticado
def pagar_repasse(dados_usuario, repasse_id):
    """POST /api/admin/repasses/<id>/pagar: paga pelo Pix da Arkhé, ou só marca como pago com {"manual": true}."""
    repasse = financeiro_service.pagar_repasse(dados_usuario.get('id_usuario'), repasse_id,
                                               request.get_json(silent=True))
    return jsonify({'message': 'Repasse pago', 'repasse': repasse})
