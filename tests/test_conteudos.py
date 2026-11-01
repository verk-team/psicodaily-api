# Feature 8: conteudos do profissional 22; o paciente 23 e o psiquiatra 24 tentam mexer.
import pytest

from apoio import PROFISSIONAL, executar


@pytest.fixture
def criar(profissional):
    """Cria conteudos do profissional 22 e apaga todos no fim."""
    criados = []

    def post(titulo, corpo):
        r = profissional.post('/api/conteudos/', json={'titulo': titulo, 'corpo': corpo})
        assert r.status_code == 201, r.get_json()
        criados.append(r.get_json()['conteudo']['conteudo_id'])
        return r.get_json()['conteudo']

    yield post
    for conteudo_id in criados:
        executar('DELETE FROM CONTEUDO WHERE CONTEUDO_ID = ?', (conteudo_id,))


def ids_publicos(anonimo):
    return [c['conteudo_id'] for c in anonimo.get(f'/api/profissionais/{PROFISSIONAL}/conteudos').get_json()['conteudos']]


def test_criar_e_listar_mais_recente_primeiro(anonimo, criar):
    primeiro = criar('  Ansiedade – 5 dicas  ', '# Respire\n\n“Calma” é **treino** 😊 e 3 < 5')
    assert primeiro['titulo'] == 'Ansiedade – 5 dicas' and primeiro['profissional_id'] == PROFISSIONAL, primeiro
    assert primeiro['publicado_em'] and primeiro['atualizado_em'] is None, primeiro
    segundo = criar('Sono', 'Durma bem.')

    r = anonimo.get(f'/api/profissionais/{PROFISSIONAL}/conteudos')
    assert r.status_code == 200, r.get_json()
    lista = [c for c in r.get_json()['conteudos'] if c['conteudo_id'] in (primeiro['conteudo_id'], segundo['conteudo_id'])]
    assert [c['conteudo_id'] for c in lista] == [segundo['conteudo_id'], primeiro['conteudo_id']], lista
    assert lista[1]['corpo'] == '# Respire\n\n“Calma” é **treino** 😊 e 3 < 5', lista[1]
    assert anonimo.get('/api/profissionais/999999/conteudos').status_code == 404


@pytest.mark.parametrize('corpo', [
    {'titulo': 'ab', 'corpo': 'x'},
    {'titulo': 'Titulo', 'corpo': ' '},
    {'titulo': 'Titulo', 'corpo': 'x' * 20001},
    {'titulo': 'Titulo', 'corpo': 5},
    {'titulo': 'Titulo', 'corpo': 'oi <script>alert(1)</script>'},
    {'titulo': '<b>Titulo</b>', 'corpo': 'x'},
    {'titulo': 'Titulo', 'corpo': 'x', 'extra': 1},
    ['lista'],
])
def test_validacao(profissional, corpo):
    r = profissional.post('/api/conteudos/', json=corpo)
    assert r.status_code == 400 and r.get_json()['error'], (r.status_code, r.get_json())


def test_permissoes(anonimo, paciente, psiquiatra, profissional, criar):
    conteudo = criar('Sono', 'Durma bem.')
    assert profissional.patch(f"/api/conteudos/{conteudo['conteudo_id']}", json={}).status_code == 400
    # anonimo 401, paciente 403, outro profissional 404 (nao ve o que nao e dele)
    assert anonimo.post('/api/conteudos/', json={'titulo': 'Titulo', 'corpo': 'x'}).status_code == 401
    assert paciente.post('/api/conteudos/', json={'titulo': 'Titulo', 'corpo': 'x'}).status_code == 403
    assert psiquiatra.patch(f"/api/conteudos/{conteudo['conteudo_id']}", json={'titulo': 'Invadido'}).status_code == 404
    assert psiquiatra.delete(f"/api/conteudos/{conteudo['conteudo_id']}").status_code == 404


@pytest.fixture
def profissional_inativo():
    """Desativa o 22 quando o teste chamar e reativa no fim."""
    yield lambda: executar('UPDATE USUARIO SET ATIVO = FALSE WHERE ID_USUARIO = ?', (PROFISSIONAL,))
    executar('UPDATE USUARIO SET ATIVO = TRUE WHERE ID_USUARIO = ?', (PROFISSIONAL,))


def destaques(anonimo, query=''):
    r = anonimo.get(f'/api/conteudos/destaques{query}')
    assert r.status_code == 200, r.get_json()
    return r.get_json()['conteudos']


def test_destaques_publicos_com_autor_e_mais_recentes_primeiro(anonimo, criar):
    antigo = criar('Ansiedade', 'Respire.')
    novo = criar('Sono', 'Durma bem.')
    lista = destaques(anonimo)
    assert [c['conteudo_id'] for c in lista[:2]] == [novo['conteudo_id'], antigo['conteudo_id']], lista
    assert lista[0] == {**novo, 'profissional_nome': 'Ana Psicologa Teste', 'profissional_foto_url': lista[0]['profissional_foto_url']}, lista[0]
    assert f'usuarios/{PROFISSIONAL}.jpg' in lista[0]['profissional_foto_url'], lista[0]


@pytest.mark.parametrize('plano, dias', [('ESSENCIAL', 10), ('PROFISSIONAL', 10), ('PREMIUM', -1)])
def test_destaques_so_de_premium_valido(anonimo, criar, assinatura, plano, dias):
    conteudo = criar('Ansiedade', 'Respire.')
    assert conteudo['conteudo_id'] in [c['conteudo_id'] for c in destaques(anonimo)]
    assinatura(plano, dias=dias)
    assert conteudo['conteudo_id'] not in [c['conteudo_id'] for c in destaques(anonimo)]


def test_destaques_sem_profissional_inativo(anonimo, criar, profissional_inativo):
    conteudo = criar('Ansiedade', 'Respire.')
    profissional_inativo()
    assert conteudo['conteudo_id'] not in [c['conteudo_id'] for c in destaques(anonimo)]


def test_destaques_limite(anonimo, criar):
    for numero in range(13):
        criar(f'Artigo {numero}', 'Texto.')
    assert len(destaques(anonimo)) == 12
    assert len(destaques(anonimo, '?limite=2')) == 2


@pytest.mark.parametrize('limite', ['0', '13', '-1', 'x'])
def test_destaques_limite_invalido(anonimo, limite):
    assert anonimo.get(f'/api/conteudos/destaques?limite={limite}').status_code == 400


def test_edicao_parcial_e_remocao_pelo_dono(anonimo, profissional, criar):
    fica = criar('Ansiedade', 'Respire.')
    sai = criar('Sono', 'Durma bem.')
    r = profissional.patch(f"/api/conteudos/{sai['conteudo_id']}", json={'titulo': 'Sono e humor'})
    assert r.status_code == 200, r.get_json()
    editado = r.get_json()['conteudo']
    assert editado['titulo'] == 'Sono e humor' and editado['corpo'] == 'Durma bem.' and editado['atualizado_em'], editado
    assert profissional.delete(f"/api/conteudos/{sai['conteudo_id']}").status_code == 200
    assert profissional.delete(f"/api/conteudos/{sai['conteudo_id']}").status_code == 404
    ids = ids_publicos(anonimo)
    assert sai['conteudo_id'] not in ids and fica['conteudo_id'] in ids, ids
