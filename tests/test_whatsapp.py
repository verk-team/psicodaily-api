# 30, 13, 12 e 31: avisos por WhatsApp (WAHA), webhook assinado, conversa com o assistente e lembretes.
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest

from apoio import PACIENTE, PROFISSIONAL, executar
from services import assistente_service, lembretes_service, notificacoes_service, whatsapp_service

CHAVE = 'chave-hmac-de-teste'
PACIENTE_CHAT = '5511922222222@c.us'  # (11) 92222-2222 do seed


@pytest.fixture
def waha(monkeypatch):
    """WAHA falso e sincrono: guarda o que seria enviado. E-mail desligado."""
    enviados = []
    monkeypatch.setenv('WAHA_URL', 'http://waha.teste')
    monkeypatch.setenv('WAHA_HMAC_KEY', CHAVE)
    monkeypatch.setattr(whatsapp_service, '_waha', lambda metodo, caminho, corpo=None: enviados.append(corpo) or {})
    monkeypatch.setattr(whatsapp_service, 'enviar_async', whatsapp_service._enviar)
    monkeypatch.setattr(notificacoes_service.email_service, 'enviar_email_async', lambda *email: None)
    assistente_service._conversas.clear()
    return enviados


@pytest.fixture
def sessao_futura(sessoes):
    def criar(horas, status='AGENDADO'):
        inicio = (datetime.now() + timedelta(hours=horas)).replace(second=0, microsecond=0)
        sid = executar('''INSERT INTO SESSAO (PACIENTE_ID, PROFISSIONAL_ID, DATA_HORA_INICIO, DATA_HORA_FIM, STATUS,
            VALOR_CENTAVOS) VALUES (?, ?, ?, ?, ?, 15000) RETURNING SESSAO_ID''',
                       (PACIENTE, PROFISSIONAL, inicio, inicio + timedelta(hours=1), status))[0][0]
        sessoes.append(sid)
        return sid
    return criar


def assinar(corpo):
    return hmac.new(CHAVE.encode(), corpo, hashlib.sha512).hexdigest()


def test_telefone_do_cadastro_e_do_whatsapp():
    assert whatsapp_service.chat_id('(11) 92222-2222') == PACIENTE_CHAT
    assert whatsapp_service.chat_id('123') is None
    assert whatsapp_service.usuario_por_telefone('5511922222222')[0] == PACIENTE
    # Numero antigo que o WhatsApp manda sem o 9 da frente
    assert whatsapp_service.usuario_por_telefone('551122222222')[0] == PACIENTE
    assert whatsapp_service.usuario_por_telefone('5521922222222') is None  # outro DDD


def test_assinatura_do_waha(monkeypatch):
    # Vetor da documentacao do WAHA (docs/how-to/events, secao HMAC)
    monkeypatch.setenv('WAHA_HMAC_KEY', 'my-secret-key')
    corpo = b'{"event":"message","session":"default","engine":"WEBJS"}'
    assert whatsapp_service.assinatura_valida(corpo, '208f8a55dde9e05519e898b10b89bf0d0b3b0fdf11fdbf09b6b90476301b98d8'
                                                     '097c462b2b17a6ce93b6b47a136cf2e78a33a63f6752c2c1631777076153fa89')
    assert not whatsapp_service.assinatura_valida(corpo, 'errada')


def test_webhook_so_aceita_o_waha(anonimo, waha, monkeypatch):
    respondidos = []
    monkeypatch.setattr(assistente_service, 'responder_whatsapp', lambda *args: respondidos.append(args))
    evento = {'event': 'message', 'payload': {'from': PACIENTE_CHAT, 'body': 'oi', 'fromMe': False}}
    corpo = json.dumps(evento).encode()
    assert anonimo.post('/api/whatsapp/webhook', data=corpo, content_type='application/json').status_code == 401
    assert anonimo.post('/api/whatsapp/webhook', data=corpo, content_type='application/json',
                        headers={'X-Webhook-Hmac': 'x' * 128}).status_code == 401
    resposta = anonimo.post('/api/whatsapp/webhook', data=corpo, content_type='application/json',
                            headers={'X-Webhook-Hmac': assinar(corpo)})
    assert resposta.status_code == 200 and resposta.get_json()['respondendo'] is True
    # Mensagem nossa e de grupo nao respondem
    for payload in ({'from': PACIENTE_CHAT, 'body': 'oi', 'fromMe': True}, {'from': '123@g.us', 'body': 'oi'}):
        assert assistente_service.receber_whatsapp({'event': 'message', 'payload': payload}) is False


def test_avisos_tambem_saem_no_whatsapp(waha, ultima_notificacao):
    from database.db import transacao
    with transacao() as cur:
        aviso = notificacoes_service.notificar(cur, PACIENTE, 'LEMBRETE_CONSULTA', 'Título', 'Corpo')
    notificacoes_service.enviar_emails([aviso])
    assert waha == [{'session': 'default', 'chatId': PACIENTE_CHAT, 'text': '*Título*\n\nOlá, Bruno Paciente Teste.\n\nCorpo'}]


def test_numero_desconhecido_e_profissional(waha):
    assistente_service.responder_whatsapp('5599911112222@c.us', 'oi')
    assert 'Não encontrei este número' in waha[-1]['text']
    assistente_service.responder_whatsapp('5511911111111@c.us', 'oi')  # psicologa 22
    assert 'atendo pacientes' in waha[-1]['text']


def test_cancela_pelo_whatsapp_com_sim(waha, sessao_futura, monkeypatch, ultima_notificacao):
    sid = sessao_futura(48)
    respostas = [
        {'content': None, 'tool_calls': [{'id': 't1', 'type': 'function', 'function': {
            'name': 'propor_cancelamento', 'arguments': json.dumps({'sessao_id': sid})}}]},
        {'content': 'Posso cancelar sua consulta de depois de amanhã.'},
    ]
    monkeypatch.setattr(assistente_service, '_chamar', lambda mensagens: respostas.pop(0))

    assistente_service.responder_whatsapp(PACIENTE_CHAT, 'quero cancelar minha consulta')
    assert waha[-1]['chatId'] == PACIENTE_CHAT and 'Responda *SIM*' in waha[-1]['text']
    assert executar('SELECT STATUS FROM SESSAO WHERE SESSAO_ID = ?', (sid,)) == [('AGENDADO',)]

    assistente_service.responder_whatsapp(PACIENTE_CHAT, 'Sim!')
    assert executar('SELECT STATUS FROM SESSAO WHERE SESSAO_ID = ?', (sid,)) == [('CANCELADO',)]
    assert 'Consulta cancelada' in [e['text'] for e in waha if e['chatId'] == PACIENTE_CHAT][-1]


def test_lembretes_24h_e_1h_uma_vez_so(waha, sessao_futura, ultima_notificacao):
    amanha, daqui_meia_hora = sessao_futura(20, 'CONFIRMADO'), sessao_futura(0.5, 'CONFIRMADO')
    sem_pagamento = sessao_futura(3)  # so consulta paga recebe lembrete
    lembretes_service.enviar_lembretes()
    marcados = {sid: (h24 is not None, h1 is not None) for sid, h24, h1 in executar(
        'SELECT SESSAO_ID, LEMBRETE_24H_EM, LEMBRETE_1H_EM FROM SESSAO WHERE SESSAO_ID IN (?, ?, ?)',
        (amanha, daqui_meia_hora, sem_pagamento))}
    assert marcados == {amanha: (True, False), daqui_meia_hora: (False, True), sem_pagamento: (False, False)}
    avisos = executar('''SELECT USUARIO_ID FROM NOTIFICACAO WHERE NOTIFICACAO_ID > ? AND TIPO = 'LEMBRETE_CONSULTA'
        ''', (ultima_notificacao,))
    assert sorted(avisos) == sorted([(PACIENTE,), (PROFISSIONAL,)] * 2)
    assert any(e['chatId'] == PACIENTE_CHAT and 'Lembrete' in e['text'] for e in waha)
    # A proxima volta do job nao repete
    assert lembretes_service.enviar_lembretes() == 0
