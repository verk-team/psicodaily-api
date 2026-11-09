"""Webhook do WhatsApp (/api/whatsapp/webhook).

Webhook é uma rota que outro sistema chama quando algo acontece. Aqui quem chama é o WAHA (o serviço que
conecta nosso número ao WhatsApp) toda vez que chega uma mensagem; o assistente responde o paciente por lá.
"""
from flask import Blueprint, jsonify, request

from services import assistente_service, whatsapp_service

whatsapp_bp = Blueprint('whatsapp', __name__, url_prefix='/api/whatsapp')


@whatsapp_bp.route('/webhook', methods=['POST'])
def webhook():
    """POST /api/whatsapp/webhook: recebe a mensagem e responde em segundo plano."""
    # Não tem login: a prova de que quem chamou é o WAHA é a assinatura HMAC do corpo, feita com uma chave
    # que só nós e o WAHA conhecemos. Sem ela, qualquer um poderia mandar mensagens falsas para cá.
    if not whatsapp_service.assinatura_valida(request.get_data(), request.headers.get('X-Webhook-Hmac')):
        return jsonify({'error': 'Assinatura invalida'}), 401
    respondendo = assistente_service.receber_whatsapp(request.get_json(silent=True) or {})
    return jsonify({'ok': True, 'respondendo': respondendo})
