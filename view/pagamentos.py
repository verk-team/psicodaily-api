"""Rotas de pagamento das consultas (/api/pagamentos) e do dinheiro do profissional.

Quem processa o pagamento é a Arkhé, o banco parceiro (API externa). Esta API só repassa os pedidos e
guarda o resultado na tabela PAGAMENTO.
"""
from flask import Blueprint, Response, jsonify, request

from services import financeiro_service, pagamentos_service
from util.usuarios import usuario_autenticado

pagamentos_bp = Blueprint('pagamentos', __name__, url_prefix='/api/pagamentos')


@pagamentos_bp.route('/cobranca', methods=['POST'])
@usuario_autenticado
def criar_cobranca(dados_usuario):
    """POST /api/pagamentos/cobranca com {id_sessao, metodo, cartao}: o paciente paga a consulta.

    PIX e boleto voltam como cobrança pendente (o paciente paga depois); cartão é aprovado ou recusado na hora.
    """
    dados = request.get_json(silent=True)
    cobranca = pagamentos_service.criar_cobranca(dados_usuario.get('id_usuario'), dados_usuario.get('usuario_role'),
                                                 dados)
    return jsonify({
        'message': 'Pagamento aprovado' if cobranca['metodo'] == 'CARTAO' else 'Cobranca criada com sucesso',
        'cobranca': cobranca,
        'id_sessao': dados['id_sessao'],
    }), 201


@pagamentos_bp.route('/cobranca/<int:id_cobranca>', methods=['GET'])
@usuario_autenticado
def consultar_cobranca(dados_usuario, id_cobranca):
    """GET /api/pagamentos/cobranca/<id>: situação da cobrança na Arkhé; se já foi paga, confirma a consulta."""
    cobranca = pagamentos_service.consultar_cobranca(dados_usuario.get('id_usuario'), id_cobranca)
    return jsonify({'cobranca': cobranca}), 200


@pagamentos_bp.route('/cobranca/<int:id_cobranca>/pdf', methods=['GET'])
@usuario_autenticado
def boleto_pdf(dados_usuario, id_cobranca):
    """GET /api/pagamentos/cobranca/<id>/pdf: o PDF do boleto."""
    pdf = pagamentos_service.boleto_pdf(dados_usuario.get('id_usuario'), id_cobranca)
    # inline: o navegador abre o PDF numa aba em vez de baixar
    return Response(pdf, mimetype='application/pdf',
                    headers={'Content-Disposition': f'inline; filename=boleto-{id_cobranca}.pdf'})


@pagamentos_bp.route('/ganhos', methods=['GET'])
@usuario_autenticado
def ganhos(dados_usuario):
    """GET /api/pagamentos/ganhos?dias=30: o que o profissional recebeu, as taxas, o saldo a receber e os repasses."""
    return jsonify(financeiro_service.ganhos(dados_usuario.get('id_usuario'), dados_usuario.get('usuario_role'),
                                             request.args.get('dias', '30'))), 200


@pagamentos_bp.route('/financeiro', methods=['GET'])
@usuario_autenticado
def financeiro(dados_usuario):
    """GET /api/pagamentos/financeiro?dias=30: saldo e extrato da conta da plataforma na Arkhé."""
    return jsonify(pagamentos_service.financeiro(dados_usuario.get('usuario_role'),
                                                 request.args.get('dias', '30'))), 200
