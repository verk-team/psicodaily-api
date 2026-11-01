# F1: agenda do profissional 22, agendamento do paciente 23.
from datetime import date, datetime, time, timedelta

import pytest

from apoio import PROFISSIONAL, executar
from services import notificacoes_service
from services.disponibilidade_service import dia_semana

# Daqui a uma semana, sempre no futuro e no mesmo dia da semana
DIA = date.today() + timedelta(days=7)


@pytest.fixture(autouse=True)
def sem_email(monkeypatch):
    monkeypatch.setattr(notificacoes_service.email_service, 'enviar_email_async', lambda *email: None)


def test_dia_semana_comeca_no_domingo():
    assert dia_semana(date(2026, 10, 18)) == 0 and dia_semana(date(2026, 10, 24)) == 6


@pytest.mark.parametrize('corpo', [
    {'janelas': [{'dia_semana': 7, 'inicio': '08:00', 'fim': '12:00'}]},
    {'janelas': [{'dia_semana': 1, 'inicio': '12:00', 'fim': '08:00'}]},
    {'janelas': [{'dia_semana': 1, 'inicio': '8h', 'fim': '12:00'}]},
    {'janelas': [{'dia_semana': 1, 'inicio': '08:00', 'fim': '12:00'},
                 {'dia_semana': 1, 'inicio': '11:00', 'fim': '13:00'}]},
    {'janelas': [{'dia_semana': True, 'inicio': '08:00', 'fim': '12:00'}]},
    {'janelas': [{'dia_semana': 1, 'inicio': '08:00', 'fim': '12:00', 'x': 1}]},
    {'janelas': {}},
    {'outra': []},
    ['lista'],
])
def test_put_invalido(profissional, corpo):
    r = profissional.put('/api/profissionais/me/disponibilidade', json=corpo)
    assert r.status_code == 400 and r.get_json()['error'], (r.status_code, r.get_json())


def test_put_so_profissional(anonimo, paciente):
    assert anonimo.put('/api/profissionais/me/disponibilidade', json={'janelas': []}).status_code == 401
    assert paciente.put('/api/profissionais/me/disponibilidade', json={'janelas': []}).status_code == 403


@pytest.mark.parametrize('query', ['de=ontem', f'de={DIA}&ate={DIA - timedelta(days=1)}',
                                   f'de={DIA}&ate={DIA + timedelta(days=31)}', 'duracao=10', 'duracao=abc'])
def test_horarios_parametro_invalido(anonimo, query):
    r = anonimo.get(f'/api/profissionais/{PROFISSIONAL}/horarios?{query}')
    assert r.status_code == 400, r.get_json()


def test_agenda_rege_agendamento_horarios_e_busca(anonimo, paciente, profissional, agenda_vazia, sessoes):
    def agendar(hora_inicio, hora_fim):
        r = paciente.post('/api/consultas/', json={
            'id_profissional': PROFISSIONAL,
            'timestamp_inicio': datetime.combine(DIA, hora_inicio).isoformat(),
            'timestamp_fim': datetime.combine(DIA, hora_fim).isoformat()})
        if r.status_code == 201:
            sessoes.append(r.get_json()['sessao']['sessao_id'])
        return r

    def na_busca(hora_inicio, hora_fim):
        r = anonimo.get('/api/profissionais/?page_size=100&disponibilidade_inicio='
                        f'{datetime.combine(DIA, hora_inicio):%Y-%m-%dT%H:%M}&disponibilidade_fim='
                        f'{datetime.combine(DIA, hora_fim):%Y-%m-%dT%H:%M}')
        assert r.status_code == 200, r.get_json()
        return PROFISSIONAL in [p['id_usuario'] for p in r.get_json()['profissionais']]

    # Sem janela nenhuma, qualquer horario sem conflito vale (como antes da F1)
    r = agendar(time(22), time(23))
    assert r.status_code == 201, r.get_json()

    janelas = [{'dia_semana': dia_semana(DIA), 'inicio': '14:00', 'fim': '18:00'},
               {'dia_semana': dia_semana(DIA), 'inicio': '08:00', 'fim': '12:00'}]
    r = profissional.put('/api/profissionais/me/disponibilidade', json={'janelas': janelas})
    assert r.status_code == 200, r.get_json()
    r = anonimo.get(f'/api/profissionais/{PROFISSIONAL}/disponibilidade')
    assert r.get_json()['janelas'] == janelas[::-1], r.get_json()
    assert anonimo.get('/api/profissionais/999999/disponibilidade').status_code == 404

    # Agendamento passa a exigir horario dentro de uma janela
    assert agendar(time(12), time(13)).status_code == 409
    assert agendar(time(11, 30), time(12, 30)).status_code == 409
    r = agendar(time(9), time(10))
    assert r.status_code == 201, r.get_json()

    # Horarios livres: janelas em fatias de 60 min, menos a sessao das 9h
    r = anonimo.get(f'/api/profissionais/{PROFISSIONAL}/horarios?de={DIA}&ate={DIA}')
    assert r.status_code == 200, r.get_json()
    inicios = [h['inicio'][11:] for h in r.get_json()['horarios']]
    assert inicios == ['08:00', '10:00', '11:00', '14:00', '15:00', '16:00', '17:00'], inicios
    r = anonimo.get(f'/api/profissionais/{PROFISSIONAL}/horarios?de={DIA}&ate={DIA}&duracao=90')
    assert [h['inicio'][11:] for h in r.get_json()['horarios']] == ['14:00', '15:30'], r.get_json()

    # Busca por disponibilidade usa a agenda
    assert na_busca(time(15), time(16))
    assert not na_busca(time(9), time(10))  # ocupado
    assert not na_busca(time(19), time(20))  # fora da agenda


def dias(a_partir_de, quantos=0):
    """(inicio, fim) em AAAA-MM-DD, contados a partir de hoje."""
    return {'inicio': (date.today() + timedelta(days=a_partir_de)).isoformat(),
            'fim': (date.today() + timedelta(days=a_partir_de + quantos)).isoformat()}


def agendar_em(paciente, sessoes, dia, hora_inicio, hora_fim):
    fim = datetime.combine(dia + timedelta(days=1) if hora_fim == time(0) else dia, hora_fim)
    r = paciente.post('/api/consultas/', json={'id_profissional': PROFISSIONAL,
                                               'timestamp_inicio': datetime.combine(dia, hora_inicio).isoformat(),
                                               'timestamp_fim': fim.isoformat()})
    if r.status_code == 201:
        sessoes.append(r.get_json()['sessao']['sessao_id'])
    return r.status_code


def test_bloqueio_recusa_agendamento_de_quem_nao_tem_janelas(paciente, profissional, agenda_vazia, sessoes, bloqueios):
    assert profissional.post('/api/profissionais/me/bloqueios', json=dias(7)).status_code == 201
    assert agendar_em(paciente, sessoes, DIA, time(10), time(11)) == 409
    # Os dias vizinhos seguem livres, inclusive a sessao que termina a meia-noite, na porta do bloqueio
    assert agendar_em(paciente, sessoes, DIA - timedelta(days=1), time(23), time(0)) == 201
    assert agendar_em(paciente, sessoes, DIA + timedelta(days=1), time(10), time(11)) == 201


def test_bloqueio_recusa_agendamento_some_da_busca_e_dos_horarios(anonimo, paciente, profissional, agenda_vazia, sessoes, bloqueios):
    janelas = [{'dia_semana': dia, 'inicio': '08:00', 'fim': '10:00'} for dia in range(7)]
    assert profissional.put('/api/profissionais/me/disponibilidade', json={'janelas': janelas}).status_code == 200
    r = profissional.post('/api/profissionais/me/bloqueios', json=dias(7, 1))
    assert r.status_code == 201, r.get_json()

    assert agendar_em(paciente, sessoes, DIA, time(8), time(9)) == 409
    assert agendar_em(paciente, sessoes, DIA + timedelta(days=1), time(8), time(9)) == 409, 'o ultimo dia e inclusivo'

    de, ate = DIA - timedelta(days=1), DIA + timedelta(days=2)
    horarios = anonimo.get(f'/api/profissionais/{PROFISSIONAL}/horarios?de={de}&ate={ate}').get_json()['horarios']
    assert {h['inicio'][:10] for h in horarios} == {de.isoformat(), ate.isoformat()}, horarios

    def na_busca(dia):
        inicio, fim = datetime.combine(dia, time(8)), datetime.combine(dia, time(9))
        r = anonimo.get(f'/api/profissionais/?page_size=100&disponibilidade_inicio={inicio:%Y-%m-%dT%H:%M}'
                        f'&disponibilidade_fim={fim:%Y-%m-%dT%H:%M}')
        return PROFISSIONAL in [p['id_usuario'] for p in r.get_json()['profissionais']]

    assert not na_busca(DIA) and na_busca(ate)


def test_bloqueio_nao_cancela_e_conta_as_sessoes_do_periodo(paciente, profissional, agenda_vazia, sessoes, bloqueios):
    assert agendar_em(paciente, sessoes, DIA, time(10), time(11)) == 201
    assert agendar_em(paciente, sessoes, DIA, time(14), time(15)) == 201
    assert agendar_em(paciente, sessoes, DIA + timedelta(days=5), time(10), time(11)) == 201  # fora do periodo
    executar("UPDATE SESSAO SET STATUS = 'CANCELADO' WHERE SESSAO_ID = ?", (sessoes[1],))

    corpo = profissional.post('/api/profissionais/me/bloqueios', json=dias(7)).get_json()
    assert corpo['sessoes_no_periodo'] == 1, corpo
    assert executar('SELECT STATUS FROM SESSAO WHERE SESSAO_ID = ?', (sessoes[0],)) == [('AGENDADO',)]


def test_listar_e_apagar_bloqueios(anonimo, paciente, profissional, psiquiatra, assinatura, bloqueios):
    assert anonimo.get('/api/profissionais/me/bloqueios').status_code == 401
    assert paciente.post('/api/profissionais/me/bloqueios', json=dias(7)).status_code == 403

    # Vale em todos os planos, e sem plano tambem: e so a agenda do proprio profissional
    assinatura('ESSENCIAL', dias=-1)
    criado = profissional.post('/api/profissionais/me/bloqueios', json=dias(20, 2)).get_json()['bloqueio']
    assert criado == {'bloqueio_id': criado['bloqueio_id'], **dias(20, 2)}, criado
    proximo = profissional.post('/api/profissionais/me/bloqueios', json=dias(7)).get_json()['bloqueio']
    executar('INSERT INTO BLOQUEIO_AGENDA (PROFISSIONAL_ID, DATA_INICIO, DATA_FIM) VALUES (?, ?, ?)',
             (PROFISSIONAL, date.today() - timedelta(days=5), date.today() - timedelta(days=1)))
    hoje = profissional.post('/api/profissionais/me/bloqueios', json=dias(0)).get_json()['bloqueio']

    r = profissional.get('/api/profissionais/me/bloqueios')
    assert r.headers['Cache-Control'] == 'private, no-store'
    assert r.get_json() == {'bloqueios': [hoje, proximo, criado]}, 'o que ja terminou nao aparece'
    assert psiquiatra.get('/api/profissionais/me/bloqueios').get_json() == {'bloqueios': []}

    rota = f"/api/profissionais/me/bloqueios/{criado['bloqueio_id']}"
    assert psiquiatra.delete(rota).status_code == 404, 'so o dono apaga'
    assert profissional.delete(rota).status_code == 200
    assert profissional.delete(rota).status_code == 404


def test_bloqueio_em_andamento_e_sobreposicao(profissional, bloqueios):
    # fim hoje, inicio no passado: as ferias ja comecaram
    r = profissional.post('/api/profissionais/me/bloqueios', json=dias(-2, 2))
    assert r.status_code == 201, r.get_json()
    assert profissional.post('/api/profissionais/me/bloqueios', json=dias(10, 4)).status_code == 201
    for sobreposto in (dias(10), dias(14), dias(8, 2), dias(14, 3), dias(8, 10), dias(11, 1)):
        r = profissional.post('/api/profissionais/me/bloqueios', json=sobreposto)
        assert r.status_code == 409 and r.get_json()['error'], (sobreposto, r.get_json())
    # Encostar nao e sobrepor
    assert profissional.post('/api/profissionais/me/bloqueios', json=dias(9)).status_code == 201
    assert profissional.post('/api/profissionais/me/bloqueios', json=dias(15)).status_code == 201


def test_bloqueio_de_365_dias_passa(profissional, bloqueios):
    r = profissional.post('/api/profissionais/me/bloqueios', json=dias(1, 364))
    assert r.status_code == 201, r.get_json()


@pytest.mark.parametrize('corpo', [
    {}, ['lista'], {'inicio': dias(1)['inicio']}, {**dias(1), 'extra': 1}, {**dias(1), 'profissional_id': 24},
    {**dias(1), 'fim': None}, {**dias(1), 'fim': 20261231}, {**dias(1), 'fim': '31/12/2099'},
    {'inicio': '2099-1-5', 'fim': '2099-01-06'}, {'inicio': '20990105', 'fim': '2099-01-06'},
    {'inicio': '2099-W01-1', 'fim': '2099-W01-2'}, {'inicio': '2099-02-30', 'fim': '2099-03-01'},
    {'inicio': dias(5)['inicio'], 'fim': dias(4)['fim']},  # fim antes do inicio
    dias(-3, 1),  # terminou ontem ou antes
    dias(1, 365),  # 366 dias
])
def test_bloqueio_invalido(profissional, bloqueios, corpo):
    r = profissional.post('/api/profissionais/me/bloqueios', json=corpo)
    assert r.status_code == 400 and r.get_json()['error'], (corpo, r.status_code, r.get_json())
