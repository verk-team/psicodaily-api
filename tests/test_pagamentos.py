# F2 e 17: cobranca ligada a sessao (Pix, boleto ou cartao), so os participantes consultam,
# a pendente do mesmo metodo e reaproveitada e o pagamento confirma a sessao.
from datetime import datetime, timedelta

import pytest

from apoio import PACIENTE, PROFISSIONAL, executar
from services import consultas_service, notificacoes_service, pagamentos_service

ID_PIX = 987654321
ID_BOLETO = 987654322
# Mesmo numero do Pix de proposito: a venda no cartao vem de outra tabela da Arkhé e pode repetir o id
ID_COMPRA = ID_PIX
CARTAO = {'numero_cartao': '4111111111111111', 'validade': '12/2030', 'cvv': '123', 'tipo': 'CREDITO', 'parcelas': 3}


@pytest.fixture
def arkhe(monkeypatch):
    """Arkhé falsa: guarda as chamadas; `status[id]` e o status de cada cobranca e `cartao` a resposta da venda."""
    estado = {'status': {}, 'chamadas': [],
              'cartao': (201, {'aprovado': True, 'id_compra': ID_COMPRA, 'valor': 150.0, 'taxa': 7.5,
                               'final_cartao': '1111'})}

    def responder(method, path, body=None, bruto=False):
        estado['chamadas'].append((method, path, body))
        if path.endswith('/pdf'):
            return 200, b'%PDF-teste'
        if path == '/api/v1/vendas/cartao':
            return estado['cartao']
        id_cobranca = ID_BOLETO if 'boletos' in path else ID_PIX
        return 200, {'id_cobranca': id_cobranca, 'valor': 150.0, 'codigo_pagamento': 'ARKHE:TESTE',
                     'status': estado['status'].get(id_cobranca, 0), 'url_pdf': '/api/v1/interno.pdf'}

    monkeypatch.setattr(pagamentos_service, '_arkhe_request', responder)
    monkeypatch.setattr(notificacoes_service.email_service, 'enviar_email_async', lambda *email: None)
    return estado


@pytest.fixture
def sessao(sessoes):
    inicio = (datetime.now() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    sid = executar('''INSERT INTO SESSAO (PACIENTE_ID, PROFISSIONAL_ID, DATA_HORA_INICIO, DATA_HORA_FIM, STATUS, VALOR_CENTAVOS)
        VALUES (?, ?, ?, ?, 'AGENDADO', 15000) RETURNING SESSAO_ID''', (PACIENTE, PROFISSIONAL, inicio, inicio + timedelta(hours=1)))[0][0]
    sessoes.append(sid)  # o PAGAMENTO sai junto (cascade)
    return sid


def _pagamentos(sessao):
    return executar('SELECT METODO, STATUS FROM PAGAMENTO WHERE SESSAO_ID = ? ORDER BY PAGAMENTO_ID', (sessao,))


def test_fluxo_do_pix(arkhe, sessao, paciente, outro_paciente, profissional, ultima_notificacao):
    assert paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao}).status_code == 201
    assert executar('SELECT VALOR_CENTAVOS, STATUS FROM PAGAMENTO WHERE SESSAO_ID = ?', (sessao,)) == [(15000, 'PENDENTE')]

    # Segunda tentativa reaproveita o Pix pendente em vez de criar outro
    assert paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao}).status_code == 201
    assert [m for m, _, _ in arkhe['chamadas']] == ['POST', 'GET'], arkhe

    # So paciente e profissional da sessao consultam (S8)
    assert outro_paciente.get(f'/api/pagamentos/cobranca/{ID_PIX}').status_code == 404
    assert profissional.get(f'/api/pagamentos/cobranca/{ID_PIX}').status_code == 200

    arkhe['status'][ID_PIX] = 1
    cobranca = paciente.get(f'/api/pagamentos/cobranca/{ID_PIX}').get_json()['cobranca']
    assert cobranca['status'] == 1 and cobranca['metodo'] == 'PIX'
    assert _pagamentos(sessao) == [('PIX', 'PAGO')]
    assert executar('SELECT STATUS FROM SESSAO WHERE SESSAO_ID = ?', (sessao,)) == [('CONFIRMADO',)]
    avisos = executar('SELECT USUARIO_ID, TIPO FROM NOTIFICACAO WHERE NOTIFICACAO_ID > ?', (ultima_notificacao,))
    # Profissional avisado e comprovante para o paciente (13)
    assert avisos == [(PROFISSIONAL, 'PAGAMENTO_CONFIRMADO'), (PACIENTE, 'PAGAMENTO_RECEBIDO')], avisos
    comprovante = executar("SELECT MENSAGEM FROM NOTIFICACAO WHERE NOTIFICACAO_ID > ? AND TIPO = 'PAGAMENTO_RECEBIDO'",
                           (ultima_notificacao,))[0][0]
    assert 'R$ 150,00 por pix' in comprovante and str(ID_PIX) in comprovante, comprovante

    # Consultar de novo nao notifica outra vez; pagar de novo e recusado
    paciente.get(f'/api/pagamentos/cobranca/{ID_PIX}')
    assert len(executar('SELECT 1 FROM NOTIFICACAO WHERE NOTIFICACAO_ID > ?', (ultima_notificacao,))) == 2
    assert paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao}).status_code == 409


def test_boleto_vence_no_dia_da_consulta_e_tem_pdf(arkhe, sessao, paciente, outro_paciente):
    resposta = paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao, 'metodo': 'BOLETO'})
    assert resposta.status_code == 201, resposta.get_json()
    cobranca = resposta.get_json()['cobranca']
    assert cobranca['metodo'] == 'BOLETO' and 'url_pdf' not in cobranca
    cpf, inicio = executar('''SELECT u.CPF, s.DATA_HORA_INICIO FROM SESSAO s JOIN USUARIO u ON u.ID_USUARIO = s.PACIENTE_ID
        WHERE s.SESSAO_ID = ?''', (sessao,))[0]
    assert arkhe['chamadas'][0] == ('POST', '/api/v1/cobrancas/boletos',
                                    {'valor': 150.0, 'documento_pagador': cpf, 'data_vencimento': inicio.date().isoformat()})

    assert paciente.get(f'/api/pagamentos/cobranca/{ID_BOLETO}').get_json()['cobranca']['metodo'] == 'BOLETO'
    assert arkhe['chamadas'][-1][1] == f'/api/v1/cobrancas/boletos/{ID_BOLETO}'
    pdf = paciente.get(f'/api/pagamentos/cobranca/{ID_BOLETO}/pdf')
    assert pdf.status_code == 200 and pdf.mimetype == 'application/pdf' and pdf.data == b'%PDF-teste'
    assert outro_paciente.get(f'/api/pagamentos/cobranca/{ID_BOLETO}/pdf').status_code == 404
    # O Pix nao tem PDF
    paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao})
    assert paciente.get(f'/api/pagamentos/cobranca/{ID_PIX}/pdf').status_code == 404

    # Cada metodo reaproveita a sua pendente
    chamadas = len(arkhe['chamadas'])
    paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao, 'metodo': 'BOLETO'})
    assert all(m == 'GET' for m, _, _ in arkhe['chamadas'][chamadas:])
    assert _pagamentos(sessao) == [('BOLETO', 'PENDENTE'), ('PIX', 'PENDENTE')]


def test_cartao_aprovado_confirma_na_hora(arkhe, sessao, paciente, ultima_notificacao):
    # Um Pix pendente com o mesmo id da venda convive com ela (ids de tabelas diferentes na Arkhé)
    paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao})
    resposta = paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao, 'metodo': 'CARTAO', 'cartao': CARTAO})
    assert resposta.status_code == 201, resposta.get_json()
    assert resposta.get_json()['cobranca']['metodo'] == 'CARTAO'
    assert arkhe['chamadas'][-1] == ('POST', '/api/v1/vendas/cartao', {**CARTAO, 'valor': 150.0})
    assert _pagamentos(sessao) == [('PIX', 'PENDENTE'), ('CARTAO', 'PAGO')]
    # A taxa da Arkhé no credito (5%) fica gravada para o financeiro do profissional (32)
    assert executar("SELECT TAXA_ARKHE_CENTAVOS FROM PAGAMENTO WHERE SESSAO_ID = ? AND METODO = 'CARTAO'", (sessao,)) == [(750,)]
    assert executar('SELECT STATUS FROM SESSAO WHERE SESSAO_ID = ?', (sessao,)) == [('CONFIRMADO',)]
    assert executar('SELECT TIPO FROM NOTIFICACAO WHERE NOTIFICACAO_ID > ?', (ultima_notificacao,)) == [('PAGAMENTO_CONFIRMADO',), ('PAGAMENTO_RECEBIDO',)]
    assert paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao, 'metodo': 'CARTAO', 'cartao': CARTAO}).status_code == 409


def test_cartao_recusado_mostra_o_motivo(arkhe, sessao, paciente):
    arkhe['cartao'] = (400, {'mensagem': 'Limite insuficiente'})
    resposta = paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao, 'metodo': 'CARTAO', 'cartao': CARTAO})
    assert resposta.status_code == 402 and resposta.get_json()['error'] == 'Limite insuficiente'
    assert _pagamentos(sessao) == []


def test_nao_cobra_cartao_se_o_pix_ja_foi_pago(arkhe, sessao, paciente):
    paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao})
    arkhe['status'][ID_PIX] = 1  # pago na Arkhé, mas ninguem consultou ainda
    resposta = paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao, 'metodo': 'CARTAO', 'cartao': CARTAO})
    assert resposta.status_code == 409
    assert not any(path == '/api/v1/vendas/cartao' for _, path, _ in arkhe['chamadas'])
    assert _pagamentos(sessao) == [('PIX', 'PAGO')]


@pytest.mark.parametrize('corpo', [
    {'metodo': 'DINHEIRO'},
    {'metodo': 'CARTAO'},
    {'cartao': CARTAO},
    {'extra': 1},
    {'metodo': 'CARTAO', 'cartao': {**CARTAO, 'numero_cartao': '4111'}},
    {'metodo': 'CARTAO', 'cartao': {**CARTAO, 'validade': '13/2030'}},
    {'metodo': 'CARTAO', 'cartao': {**CARTAO, 'tipo': 'DEBITO'}},
    {'metodo': 'CARTAO', 'cartao': {**CARTAO, 'parcelas': 16}},
    {'metodo': 'CARTAO', 'cartao': {**CARTAO, 'nome': 'X'}},
])
def test_pedido_invalido_nao_chama_a_arkhe(arkhe, sessao, paciente, corpo):
    assert paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao, **corpo}).status_code == 400
    assert arkhe['chamadas'] == []


def test_reserva_sem_pagamento_expira(arkhe, sessao, sessoes, paciente, profissional, ultima_notificacao):
    inicio = executar('SELECT DATA_HORA_INICIO FROM SESSAO WHERE SESSAO_ID = ?', (sessao,))[0][0]
    paga = executar('''INSERT INTO SESSAO (PACIENTE_ID, PROFISSIONAL_ID, DATA_HORA_INICIO, DATA_HORA_FIM, STATUS,
        VALOR_CENTAVOS) VALUES (?, ?, ?, ?, 'AGENDADO', 15000) RETURNING SESSAO_ID''',
                    (PACIENTE, PROFISSIONAL, inicio + timedelta(hours=2), inicio + timedelta(hours=3)))[0][0]
    sessoes.append(paga)
    assert paciente.post('/api/pagamentos/cobranca', json={'id_sessao': paga}).status_code == 201
    arkhe['status'][ID_PIX] = 1  # pago na Arkhé sem ninguem abrir a tela de pagamento
    # CRIADO_EM em 2000 separa estas duas das outras sessoes do banco
    criada = datetime(2000, 1, 1)
    executar('UPDATE SESSAO SET CRIADO_EM = ? WHERE SESSAO_ID IN (?, ?)', (criada, sessao, paga))

    assert consultas_service.expirar_reservas(criada + timedelta(minutes=29)) == 0
    assert consultas_service.expirar_reservas(criada + timedelta(minutes=31)) == 1
    status = dict(executar('SELECT SESSAO_ID, STATUS FROM SESSAO WHERE SESSAO_ID IN (?, ?)', (sessao, paga)))
    assert status == {sessao: 'CANCELADO', paga: 'CONFIRMADO'}, status
    avisos = executar('SELECT USUARIO_ID, TITULO FROM NOTIFICACAO WHERE NOTIFICACAO_ID > ?', (ultima_notificacao,))
    assert (PACIENTE, 'Reserva cancelada') in avisos and (PROFISSIONAL, 'Nova consulta confirmada') in avisos, avisos
    # A sessao expirada nao aceita mais pagamento
    r = paciente.post('/api/pagamentos/cobranca', json={'id_sessao': sessao})
    assert r.status_code == 409 and r.get_json()['cancelada'] is True, r.get_json()
