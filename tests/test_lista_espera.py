# Feature 6: paciente 23 espera vaga com o profissional 22; o cancelamento de uma sessao do 25 libera o horario.
from datetime import date, datetime, time, timedelta

import pytest

from apoio import PROFISSIONAL, executar
from services import notificacoes_service

DIA = date.today() + timedelta(days=3)


def em(hora, dia=DIA):
    return datetime.combine(dia, time(hora)).isoformat(timespec='minutes')


@pytest.fixture
def ultima_entrada():
    ultima = executar('SELECT COALESCE(MAX(LISTA_ESPERA_ID), 0) FROM LISTA_ESPERA')[0][0]
    yield ultima
    executar('DELETE FROM LISTA_ESPERA WHERE LISTA_ESPERA_ID > ?', (ultima,))


@pytest.fixture
def emails(monkeypatch):
    enviados = []
    monkeypatch.setattr(notificacoes_service.email_service, 'enviar_email_async', lambda *email: enviados.append(email))
    return enviados


def entrar(cliente, inicio, fim, profissional_id=PROFISSIONAL):
    return cliente.post('/api/lista-espera/', json={'id_profissional': profissional_id, 'inicio': inicio, 'fim': fim})


def test_permissoes(anonimo, profissional):
    assert anonimo.get('/api/lista-espera/').status_code == 401
    assert profissional.get('/api/lista-espera/').status_code == 403
    assert entrar(profissional, em(8), em(9)).status_code == 403
    assert profissional.delete('/api/lista-espera/1').status_code == 403


@pytest.mark.parametrize('corpo', [
    {},
    {'id_profissional': PROFISSIONAL, 'inicio': em(8)},
    {'id_profissional': PROFISSIONAL, 'inicio': em(8), 'fim': em(9), 'extra': 1},
    {'id_profissional': str(PROFISSIONAL), 'inicio': em(8), 'fim': em(9)},
    {'id_profissional': PROFISSIONAL, 'inicio': em(9), 'fim': em(8)},
    {'id_profissional': PROFISSIONAL, 'inicio': em(8, date.today() - timedelta(days=1)), 'fim': em(9)},
    {'id_profissional': PROFISSIONAL, 'inicio': em(8), 'fim': em(9, DIA + timedelta(days=32))},
    {'id_profissional': PROFISSIONAL, 'inicio': '8h', 'fim': em(9)},
    ['lista'],
])
def test_entrada_invalida(paciente, corpo):
    r = paciente.post('/api/lista-espera/', json=corpo)
    assert r.status_code == 400 and r.get_json()['error'], (r.status_code, r.get_json())


def test_entrar_listar_e_sair(paciente, outro_paciente, ultima_entrada):
    assert entrar(paciente, em(8), em(9), profissional_id=999999).status_code == 404
    r = entrar(paciente, em(8), em(12))
    assert r.status_code == 201, r.get_json()
    entrada = r.get_json()['lista_espera']
    assert (entrada['profissional_id'], entrada['inicio'], entrada['fim'], entrada['avisado_em']) == (
        PROFISSIONAL, em(8) + ':00', em(12) + ':00', None), entrada
    assert entrada['profissional_nome'], entrada

    r = paciente.get('/api/lista-espera/')
    assert r.status_code == 200 and r.headers['Cache-Control'] == 'private, no-store'
    assert entrada in r.get_json()['lista_espera'], r.get_json()
    assert entrada not in outro_paciente.get('/api/lista-espera/').get_json()['lista_espera']

    assert outro_paciente.delete(f"/api/lista-espera/{entrada['lista_espera_id']}").status_code == 404
    assert paciente.delete(f"/api/lista-espera/{entrada['lista_espera_id']}").status_code == 200
    assert paciente.delete(f"/api/lista-espera/{entrada['lista_espera_id']}").status_code == 404


def test_cancelamento_dentro_de_bloqueio_nao_avisa(paciente, outro_paciente, profissional, agenda_vazia, sessoes, bloqueios,
                                                   ultima_entrada, ultima_notificacao, emails):
    # Sessao marcada antes das ferias: o bloqueio nao a cancela, mas o horario dela nao vira vaga
    r = outro_paciente.post('/api/consultas/', json={'id_profissional': PROFISSIONAL,
                                                     'timestamp_inicio': em(15), 'timestamp_fim': em(16)})
    assert r.status_code == 201, r.get_json()
    sessao_id = r.get_json()['sessao']['sessao_id']
    sessoes.append(sessao_id)
    entrada = entrar(paciente, em(8), em(20)).get_json()['lista_espera']
    assert profissional.post('/api/profissionais/me/bloqueios', json={'inicio': DIA.isoformat(), 'fim': DIA.isoformat()}).status_code == 201

    emails.clear()
    assert profissional.patch(f'/api/consultas/{sessao_id}', json={'status': 'CANCELADO'}).status_code == 200
    assert not [email for email in emails if email[1].startswith('Abriu um horário')], emails
    # A entrada segue na fila, para a proxima vaga de verdade
    avisados = {e['lista_espera_id']: e['avisado_em'] for e in paciente.get('/api/lista-espera/').get_json()['lista_espera']}
    assert avisados[entrada['lista_espera_id']] is None, avisados


def test_cancelamento_avisa_so_o_primeiro_compativel(paciente, outro_paciente, profissional, agenda_vazia, sessoes,
                                                     ultima_entrada, ultima_notificacao, emails):
    r = outro_paciente.post('/api/consultas/', json={'id_profissional': PROFISSIONAL,
                                                     'timestamp_inicio': em(15), 'timestamp_fim': em(16)})
    assert r.status_code == 201, r.get_json()
    sessao_id = r.get_json()['sessao']['sessao_id']
    sessoes.append(sessao_id)

    curta = entrar(paciente, em(8), em(9)).get_json()['lista_espera']  # nao cobre 15h-16h
    primeira = entrar(paciente, em(8), em(20)).get_json()['lista_espera']
    segunda = entrar(paciente, em(14), em(17)).get_json()['lista_espera']
    do_dono = entrar(outro_paciente, em(8), em(20)).get_json()['lista_espera']  # o paciente da sessao nao e avisado

    # Trocar o link nao libera horario: so o paciente da sessao recebe o link (30)
    emails.clear()  # avisos do agendamento
    assert profissional.patch(f'/api/consultas/{sessao_id}', json={'link_reuniao': 'https://meet.x/a'}).status_code == 200
    assert [(email[0], email[1]) for email in emails] == [('outro.paciente@exemplo.com', 'Link da consulta')], emails
    emails.clear()

    assert profissional.patch(f'/api/consultas/{sessao_id}', json={'status': 'CANCELADO'}).status_code == 200
    vaga = [email for email in emails if email[1].startswith('Abriu um horário')]
    assert len(vaga) == 1 and vaga[0][0] == 'paciente.teste@exemplo.com', emails
    assert DIA.strftime('%d/%m/%Y') + ' às 15:00' in vaga[0][2], emails
    # O paciente da sessao tambem e avisado do cancelamento (24)
    assert [email[1] for email in emails if email not in vaga] == ['Consulta cancelada'], emails

    avisados = {e['lista_espera_id']: e['avisado_em'] for e in paciente.get('/api/lista-espera/').get_json()['lista_espera']}
    assert avisados[primeira['lista_espera_id']] and not avisados[segunda['lista_espera_id']], avisados
    assert not avisados[curta['lista_espera_id']], avisados
    assert outro_paciente.get('/api/lista-espera/').get_json()['lista_espera'][-1]['avisado_em'] is None, do_dono

    notificacao = paciente.get('/api/notificacoes/').get_json()['notificacoes'][0]
    assert notificacao['tipo'] == 'VAGA_LISTA_ESPERA' and notificacao['dados'] == {
        'lista_espera_id': primeira['lista_espera_id'], 'profissional_id': PROFISSIONAL,
        'inicio': em(15) + ':00', 'fim': em(16) + ':00'}, notificacao

    # A vaga nao fica reservada: qualquer paciente agenda
    r = paciente.post('/api/consultas/', json={'id_profissional': PROFISSIONAL,
                                              'timestamp_inicio': em(15), 'timestamp_fim': em(16)})
    assert r.status_code == 201, r.get_json()
    sessoes.append(r.get_json()['sessao']['sessao_id'])
