"""Rotas da assinatura do profissional (/api/assinaturas): planos, assinar, cancelar e boleto.

Sem um plano válido o profissional some da busca e não recebe agendamentos (regra em assinaturas_service).
"""
from flask import Blueprint, Response, jsonify, request

from services import assinaturas_service
from util.usuarios import profissional_autenticado

assinaturas_bp = Blueprint('assinaturas', __name__, url_prefix='/api/assinaturas')


@assinaturas_bp.route('/planos', methods=['GET'])
def planos():
    """GET /api/assinaturas/planos: planos disponíveis e preços, sem login."""
    return jsonify({'planos': assinaturas_service.planos()}), 200


@assinaturas_bp.route('/me', methods=['GET'])
@profissional_autenticado
def obter(dados_usuario):
    """GET /api/assinaturas/me: a assinatura do profissional logado."""
    return jsonify({'assinatura': assinaturas_service.obter(dados_usuario['id_usuario'])}), 200


@assinaturas_bp.route('/me', methods=['POST'])
@profissional_autenticado
def assinar(dados_usuario):
    """POST /api/assinaturas/me: assina ou troca de plano.

    201 quando gera uma cobrança para pagar; 200 quando não precisa pagar agora.
    """
    assinatura, cobranca = assinaturas_service.assinar(dados_usuario['id_usuario'], request.get_json(silent=True))
    status = 201 if cobranca else 200
    return jsonify({'assinatura': assinatura, 'cobranca': cobranca}), status


@assinaturas_bp.route('/me/cancelar', methods=['POST'])
@profissional_autenticado
def cancelar(dados_usuario):
    """POST /api/assinaturas/me/cancelar: para de renovar; o plano vale até o fim do ciclo já pago."""
    return jsonify({'assinatura': assinaturas_service.cancelar(dados_usuario['id_usuario'])}), 200


@assinaturas_bp.route('/me/cobranca/<int:cobranca_id>/pdf', methods=['GET'])
@profissional_autenticado
def boleto_pdf(dados_usuario, cobranca_id):
    """GET /api/assinaturas/me/cobranca/<id>/pdf: o PDF do boleto da assinatura."""
    pdf = assinaturas_service.boleto_pdf(dados_usuario['id_usuario'], cobranca_id)
    # inline: o navegador abre o PDF numa aba em vez de baixar
    return Response(pdf, mimetype='application/pdf',
                    headers={'Content-Disposition': f'inline; filename=boleto-assinatura-{cobranca_id}.pdf'})
