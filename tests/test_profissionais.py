# Busca, campos e avaliacao de profissionais. O 24 do seed (psiquiatra) fala Ingles e custa 30000.
from datetime import datetime

import pytest

from apoio import PACIENTE, PROFISSIONAL, PSIQUIATRA, executar

COLUNAS = 'ABORDAGEM, MODALIDADE, IDIOMAS, AREAS_ATUACAO, ANO_INICIO_ATUACAO'


@pytest.fixture
def perfil_original():
    original = executar(f'SELECT {COLUNAS} FROM PROFISSIONAL WHERE USUARIO_ID = ?', (PROFISSIONAL,))[0]
    yield
    executar(f'''UPDATE PROFISSIONAL SET {", ".join(c + " = ?" for c in COLUNAS.split(", "))}
        WHERE USUARIO_ID = ?''', (*original, PROFISSIONAL))


@pytest.fixture
def busca(anonimo):
    """(status, {id_usuario: profissional}) na ordem da resposta."""
    def buscar(query=''):
        r = anonimo.get('/api/profissionais/?page_size=100' + query)
        return r.status_code, {p['id_usuario']: p for p in (r.get_json() or {}).get('profissionais', [])}
    return buscar


def nova_sessao(sessoes, status, inicio, fim):
    sessao_id = executar('''INSERT INTO SESSAO (PACIENTE_ID, PROFISSIONAL_ID, DATA_HORA_INICIO, DATA_HORA_FIM,
        STATUS, VALOR_CENTAVOS) VALUES (?, ?, ?, ?, ?, 12000) RETURNING SESSAO_ID''',
                         (PACIENTE, PROFISSIONAL, inicio, fim, status))[0][0]
    sessoes.append(sessao_id)
    return sessao_id


def test_campos_na_listagem_e_no_detalhe(anonimo, busca):
    status, lista = busca()
    assert status == 200, status
    for campo in ('especialidade', 'abordagem', 'modalidade', 'avaliacao', 'total_avaliacoes'):
        assert campo in lista[PROFISSIONAL], campo
    detalhe = anonimo.get(f'/api/profissionais/{PROFISSIONAL}').get_json()['profissionais']
    assert detalhe['total_avaliacoes'] == 0 and detalhe['avaliacao'] is None, detalhe


def test_patch_abordagem_e_modalidade_e_filtros(profissional, busca, perfil_original):
    r = profissional.patch('/api/profissionais/me', json={'abordagem': 'tcc', 'modalidade': 'online'})
    assert r.status_code == 200, r.get_json()
    p = busca()[1][PROFISSIONAL]
    assert (p['abordagem'], p['modalidade']) == ('TCC', 'ONLINE'), p
    assert profissional.patch('/api/profissionais/me', json={'abordagem': 'Psicanalise'}).status_code == 200
    assert busca()[1][PROFISSIONAL]['abordagem'] == 'Psicanálise'

    assert PROFISSIONAL in busca('&abordagem=psicanálise')[1]
    assert busca('&abordagem=TCC') == (200, {})
    assert PROFISSIONAL in busca('&modalidade=Online')[1]
    assert PROFISSIONAL not in busca('&modalidade=Presencial')[1]
    assert profissional.patch('/api/profissionais/me', json={'modalidade': 'ambas'}).status_code == 200
    assert PROFISSIONAL in busca('&modalidade=Presencial')[1] and PROFISSIONAL in busca('&modalidade=Online')[1]
    assert PROFISSIONAL not in busca('&avaliacao_min=1')[1]  # sem avaliacao fica de fora
    assert PROFISSIONAL in busca('&preco_max=15000&especialidade=Psicologia&abordagem=Psicanalise')[1]
    assert PROFISSIONAL not in busca('&preco_max=100')[1]

    # Vazio/null limpa
    assert profissional.patch('/api/profissionais/me', json={'abordagem': '', 'modalidade': None}).status_code == 200
    p = busca()[1][PROFISSIONAL]
    assert p['abordagem'] is None and p['modalidade'] is None, p


@pytest.mark.parametrize('corpo', [{'abordagem': 'xyz'}, {'modalidade': 'hibrido'}, {'abordagem': 5}, {'outro': 1}])
def test_patch_invalido(profissional, corpo):
    assert profissional.patch('/api/profissionais/me', json=corpo).status_code == 400


@pytest.mark.parametrize('query', [
    '&abordagem=xyz', '&modalidade=xyz', '&avaliacao_min=6', '&avaliacao_min=0', '&avaliacao_min=abc',
    '&avaliacao_min=nan', '&idioma=Klingon', '&idioma=Português,Inglês', '&area_atuacao=',
    '&experiencia_min=-1', '&experiencia_min=abc', '&ordenar=nome',
])
def test_busca_parametro_invalido(busca, query):
    assert busca(query)[0] == 400


def test_busca_por_disponibilidade_aceita_o_formato(busca):
    assert busca('&disponibilidade_inicio=2026-09-29T10:00&disponibilidade_fim=2026-09-29T11:00')[0] == 200


def test_avaliacao(anonimo, paciente, outro_paciente, profissional, busca, sessoes):
    realizada = nova_sessao(sessoes, 'REALIZADO', datetime(2026, 9, 1, 10), datetime(2026, 9, 1, 11))
    agendada = nova_sessao(sessoes, 'AGENDADO', datetime(2030, 1, 1, 10), datetime(2030, 1, 1, 11))
    # Paga, mas ainda nao comecou; e sem pagamento, mesmo ja passada
    confirmada_futura = nova_sessao(sessoes, 'CONFIRMADO', datetime(2030, 1, 2, 10), datetime(2030, 1, 2, 11))
    nao_paga = nova_sessao(sessoes, 'AGENDADO', datetime(2026, 9, 3, 10), datetime(2026, 9, 3, 11))
    url = f'/api/consultas/{realizada}/avaliacao'

    assert anonimo.post(url, json={'nota': 5}).status_code == 401
    assert profissional.post(url, json={'nota': 5}).status_code == 403
    assert outro_paciente.post(url, json={'nota': 5}).status_code == 404
    for sessao in (agendada, confirmada_futura, nao_paga):
        assert paciente.post(f'/api/consultas/{sessao}/avaliacao', json={'nota': 5}).status_code == 409, sessao
    for corpo in ({'nota': 6}, {'nota': 0}, {'nota': True}, {'nota': 4.5}, {'nota': '5'}, {},
                  {'nota': 5, 'extra': 1}, {'nota': 5, 'comentario': 'x' * 501}, {'nota': 5, 'comentario': 3},
                  {'nota': 5, 'publica': 'sim'}, {'nota': 5, 'publica': 1}):
        assert paciente.post(url, json=corpo).status_code == 400, corpo

    # Aspas curvas, travessao e emoji (F0.1) no comentario
    r = paciente.post(url, json={'nota': 5, 'comentario': '  “ótima” – recomendo 😊  ', 'publica': True})
    assert r.status_code == 201, r.get_json()
    comentario = executar('SELECT COMENTARIO FROM AVALIACAO WHERE SESSAO_ID = ?', (realizada,))[0][0]
    assert comentario == '“ótima” – recomendo 😊', comentario
    assert r.get_json()['avaliacao'] == {'sessao_id': realizada, 'nota': 5, 'publica': True}
    assert paciente.post(url, json={'nota': 3}).status_code == 409
    # Logo depois da videochamada a sessao paga ainda e CONFIRMADO: ja pode avaliar. Privada por padrao
    realizada2 = nova_sessao(sessoes, 'CONFIRMADO', datetime(2026, 9, 2, 10), datetime(2026, 9, 2, 11))
    r = paciente.post(f'/api/consultas/{realizada2}/avaliacao', json={'nota': 4, 'comentario': 'so para voce'})
    assert r.status_code == 201 and r.get_json()['avaliacao']['publica'] is False, r.get_json()

    # No perfil so o comentario publico, sem quem escreveu
    publicas = anonimo.get(f'/api/profissionais/{PROFISSIONAL}/avaliacoes').get_json()['avaliacoes']
    assert [(a['nota'], a['comentario']) for a in publicas] == [(5, '“ótima” – recomendo 😊')], publicas
    assert set(publicas[0]) == {'nota', 'comentario', 'criado_em'}, publicas
    # O profissional le os dois na lista de consultas
    dele = {c['sessao_id']: c for c in profissional.get('/api/consultas/').get_json()['consultas']}
    assert (dele[realizada2]['comentario_avaliacao'], dele[realizada2]['avaliacao_publica']) == ('so para voce', False)

    p = busca()[1][PROFISSIONAL]
    assert p['avaliacao'] == 4.5 and isinstance(p['avaliacao'], float) and p['total_avaliacoes'] == 2, p
    assert PROFISSIONAL in busca('&avaliacao_min=4.5')[1]
    assert PROFISSIONAL not in busca('&avaliacao_min=5')[1]
    assert anonimo.get(f'/api/profissionais/{PROFISSIONAL}').get_json()['profissionais']['avaliacao'] == 4.5
    total = anonimo.get('/api/profissionais/?avaliacao_min=4').get_json()['total']
    assert total == 1, total
    # O seed nao tem outro profissional avaliado: o 22 vem primeiro
    assert list(busca('&ordenar=avaliacao')[1])[0] == PROFISSIONAL

    consultas = {c['sessao_id']: c for c in paciente.get('/api/consultas/').get_json()['consultas']}
    assert consultas[realizada]['nota_avaliacao'] == 5 and consultas[agendada]['nota_avaliacao'] is None


def test_filtros_avancados(busca, perfil_original):
    # Feature 15: idioma, area, experiencia e ordenacao
    executar('''UPDATE PROFISSIONAL SET IDIOMAS = 'Português,Libras', AREAS_ATUACAO = 'Ansiedade,Luto',
        ANO_INICIO_ATUACAO = ? WHERE USUARIO_ID = ?''', (datetime.now().year - 10, PROFISSIONAL))
    assert PROFISSIONAL in busca('&idioma=libras')[1] and PSIQUIATRA not in busca('&idioma=libras')[1]
    assert PSIQUIATRA in busca('&idioma=Inglês')[1] and PROFISSIONAL not in busca('&idioma=Inglês')[1]
    assert PROFISSIONAL in busca('&area_atuacao=luto&idioma=Português')[1]
    assert PROFISSIONAL not in busca('&area_atuacao=Trauma')[1]
    assert PROFISSIONAL in busca('&experiencia_min=10')[1] and PROFISSIONAL not in busca('&experiencia_min=11')[1]
    ordem = list(busca('&ordenar=preco')[1])
    assert ordem.index(PROFISSIONAL) < ordem.index(PSIQUIATRA), ordem
    assert list(busca('&ordenar=experiencia')[1])[0] == PROFISSIONAL
