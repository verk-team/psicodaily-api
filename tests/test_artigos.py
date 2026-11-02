# Artigos (feature 8, rotas em /api/artigos): o profissional 22 (Premium no seed) escreve; todos leem e curtem.
import pytest

from apoio import PROFISSIONAL, executar


@pytest.fixture
def criar(profissional):
    """Cria artigos do profissional 22 e apaga todos no fim (as curtidas saem junto, por cascata)."""
    criados = []

    def post(titulo, corpo='Texto.', subtitulo='Um subtitulo'):
        r = profissional.post('/api/artigos/', json={'titulo': titulo, 'subtitulo': subtitulo, 'corpo': corpo})
        assert r.status_code == 201, r.get_json()
        criados.append(r.get_json()['artigo']['artigo_id'])
        return r.get_json()['artigo']

    yield post
    for artigo_id in criados:
        executar('DELETE FROM CONTEUDO WHERE CONTEUDO_ID = ?', (artigo_id,))


def ids(cliente, query=''):
    r = cliente.get(f'/api/artigos/?page_size=50{query}')
    assert r.status_code == 200, r.get_json()
    return [a['artigo_id'] for a in r.get_json()['artigos']]


def test_criar_e_ler(paciente, profissional, criar):
    artigo = criar('  Ansiedade – 5 dicas  ', '# Respire\n\n“Calma” é **treino** 😊 e 3 < 5', 'O que a ciência diz')
    assert (artigo['titulo'], artigo['subtitulo']) == ('Ansiedade – 5 dicas', 'O que a ciência diz'), artigo
    assert artigo['autor']['id_usuario'] == PROFISSIONAL and artigo['autor']['nome'] and artigo['autor']['foto_url']
    assert artigo['publicado_em'] and artigo['atualizado_em'] is None, artigo
    assert (artigo['curtidas'], artigo['curtido']) == (0, False), artigo

    # Paciente e profissional leem; o detalhe traz o corpo em Markdown, a lista nao
    lido = paciente.get(f"/api/artigos/{artigo['artigo_id']}").get_json()['artigo']
    assert lido['corpo'] == '# Respire\n\n“Calma” é **treino** 😊 e 3 < 5', lido
    assert profissional.get(f"/api/artigos/{artigo['artigo_id']}").status_code == 200
    item = next(a for a in paciente.get('/api/artigos/?page_size=50').get_json()['artigos']
                if a['artigo_id'] == artigo['artigo_id'])
    assert 'corpo' not in item and item['subtitulo'] == 'O que a ciência diz', item
    assert paciente.get('/api/artigos/999999').status_code == 404


def test_paginacao_mais_recentes_primeiro_e_autor(paciente, criar):
    criados = [criar(f'Artigo {numero}')['artigo_id'] for numero in range(3)]
    pagina = paciente.get('/api/artigos/?page=1&page_size=2').get_json()
    assert pagina['page_size'] == 2 and pagina['total'] >= 3 and pagina['total_pages'] >= 2, pagina
    assert [a['artigo_id'] for a in pagina['artigos']] == criados[::-1][:2], pagina
    segunda = paciente.get('/api/artigos/?page=2&page_size=2').get_json()['artigos']
    assert segunda[0]['artigo_id'] == criados[0], segunda
    assert set(criados) <= set(ids(paciente, f'&autor={PROFISSIONAL}'))
    assert ids(paciente, '&autor=999999') == []


@pytest.mark.parametrize('query', ['page=0', 'page=x', 'page_size=51', 'page_size=0', 'autor=x'])
def test_paginacao_invalida(paciente, query):
    assert paciente.get(f'/api/artigos/?{query}').status_code == 400


def test_leitura_exige_login(anonimo, criar):
    artigo = criar('Sono')
    assert anonimo.get('/api/artigos/').status_code == 401
    assert anonimo.get(f"/api/artigos/{artigo['artigo_id']}").status_code == 401
    assert anonimo.post(f"/api/artigos/{artigo['artigo_id']}/curtida").status_code == 401


@pytest.mark.parametrize('corpo', [
    {'titulo': 'ab', 'subtitulo': 'Sub', 'corpo': 'x'},
    {'titulo': 'Titulo', 'subtitulo': 'Sub', 'corpo': ' '},
    {'titulo': 'Titulo', 'subtitulo': 'Sub', 'corpo': 'x' * 20001},
    {'titulo': 'Titulo', 'subtitulo': 'Sub', 'corpo': 5},
    {'titulo': 'Titulo', 'subtitulo': 'Sub', 'corpo': 'oi <script>alert(1)</script>'},
    {'titulo': '<b>Titulo</b>', 'subtitulo': 'Sub', 'corpo': 'x'},
    {'titulo': 'Titulo', 'subtitulo': '<i>Sub</i>', 'corpo': 'x'},
    {'titulo': 'Titulo', 'corpo': 'x'},
    {'titulo': 'Titulo', 'subtitulo': 'x' * 301, 'corpo': 'x'},
    {'titulo': 'Titulo', 'subtitulo': 'Sub', 'corpo': 'x', 'extra': 1},
    ['lista'],
])
def test_validacao(profissional, corpo):
    r = profissional.post('/api/artigos/', json=corpo)
    assert r.status_code == 400 and r.get_json()['error'], (r.status_code, r.get_json())


def test_permissoes_de_escrita(anonimo, paciente, psiquiatra, profissional, criar):
    artigo = criar('Sono')
    rota = f"/api/artigos/{artigo['artigo_id']}"
    novo = {'titulo': 'Titulo', 'subtitulo': 'Sub', 'corpo': 'x'}
    assert profissional.patch(rota, json={}).status_code == 400
    # anonimo 401, paciente 403, outro profissional 404 (nao mexe no que nao e dele)
    assert anonimo.post('/api/artigos/', json=novo).status_code == 401
    assert paciente.post('/api/artigos/', json=novo).status_code == 403
    assert paciente.patch(rota, json={'titulo': 'Invadido'}).status_code == 403
    assert paciente.delete(rota).status_code == 403
    assert psiquiatra.patch(rota, json={'titulo': 'Invadido'}).status_code == 404
    assert psiquiatra.delete(rota).status_code == 404


def test_escrever_exige_premium(paciente, profissional, criar, assinatura):
    artigo = criar('Ansiedade')
    rota = f"/api/artigos/{artigo['artigo_id']}"
    assinatura('PROFISSIONAL')
    criado = profissional.post('/api/artigos/', json={'titulo': 'Titulo', 'subtitulo': 'Sub', 'corpo': 'x'})
    assert criado.status_code == 403 and criado.get_json()['plano_necessario'] == 'PREMIUM', criado.get_json()
    assert profissional.patch(rota, json={'titulo': 'Outro'}).status_code == 403
    # Abaixo do Premium o artigo sai do ar (lista, detalhe e curtida), mas o dono ainda pode apagar
    assert artigo['artigo_id'] not in ids(paciente)
    assert paciente.get(rota).status_code == 404
    assert paciente.post(f'{rota}/curtida').status_code == 404
    assert profissional.delete(rota).status_code == 200


def test_edicao_marca_data_e_remocao(paciente, profissional, criar):
    artigo = criar('Sono', 'Durma bem.')
    rota = f"/api/artigos/{artigo['artigo_id']}"
    r = profissional.patch(rota, json={'titulo': 'Sono e humor'})
    assert r.status_code == 200, r.get_json()
    editado = r.get_json()['artigo']
    assert (editado['titulo'], editado['subtitulo'], editado['corpo']) == ('Sono e humor', 'Um subtitulo', 'Durma bem.')
    assert editado['atualizado_em'] and editado['publicado_em'] == artigo['publicado_em'], editado
    assert profissional.delete(rota).status_code == 200
    assert profissional.delete(rota).status_code == 404
    assert paciente.get(rota).status_code == 404


def test_curtidas_de_todos_e_idempotentes(paciente, outro_paciente, profissional, criar):
    artigo = criar('Ansiedade')
    rota = f"/api/artigos/{artigo['artigo_id']}"
    assert paciente.post(f'{rota}/curtida').get_json() == {'curtidas': 1, 'curtido': True}
    assert paciente.post(f'{rota}/curtida').get_json() == {'curtidas': 1, 'curtido': True}
    assert profissional.post(f'{rota}/curtida').get_json() == {'curtidas': 2, 'curtido': True}
    assert outro_paciente.get(rota).get_json()['artigo']['curtido'] is False
    lido = paciente.get(rota).get_json()['artigo']
    assert (lido['curtidas'], lido['curtido']) == (2, True), lido
    assert paciente.delete(f'{rota}/curtida').get_json() == {'curtidas': 1, 'curtido': False}
    assert paciente.delete(f'{rota}/curtida').get_json() == {'curtidas': 1, 'curtido': False}
    assert executar('SELECT USUARIO_ID FROM CURTIDA_ARTIGO WHERE CONTEUDO_ID = ?', (artigo['artigo_id'],)) == [(PROFISSIONAL,)]
    assert paciente.post('/api/artigos/999999/curtida').status_code == 404


@pytest.fixture
def profissional_inativo():
    """Desativa o 22 quando o teste chamar e reativa no fim."""
    yield lambda: executar('UPDATE USUARIO SET ATIVO = FALSE WHERE ID_USUARIO = ?', (PROFISSIONAL,))
    executar('UPDATE USUARIO SET ATIVO = TRUE WHERE ID_USUARIO = ?', (PROFISSIONAL,))


def destaques(anonimo, query=''):
    r = anonimo.get(f'/api/artigos/destaques{query}')
    assert r.status_code == 200, r.get_json()
    return r.get_json()['artigos']


def test_destaques_publicos_com_corpo_e_mais_recentes_primeiro(anonimo, paciente, criar):
    antigo = criar('Ansiedade', 'Respire.')
    novo = criar('Sono', 'Durma bem.')
    paciente.post(f"/api/artigos/{novo['artigo_id']}/curtida")
    lista = destaques(anonimo)
    assert [a['artigo_id'] for a in lista[:2]] == [novo['artigo_id'], antigo['artigo_id']], lista
    assert lista[0] == {**novo, 'curtidas': 1}, lista[0]
    assert f'usuarios/{PROFISSIONAL}.jpg' in lista[0]['autor']['foto_url'], lista[0]


@pytest.mark.parametrize('plano, dias', [('ESSENCIAL', 10), ('PROFISSIONAL', 10), ('PREMIUM', -1)])
def test_destaques_so_de_premium_valido(anonimo, criar, assinatura, plano, dias):
    artigo = criar('Ansiedade')
    assert artigo['artigo_id'] in [a['artigo_id'] for a in destaques(anonimo)]
    assinatura(plano, dias=dias)
    assert artigo['artigo_id'] not in [a['artigo_id'] for a in destaques(anonimo)]


def test_destaques_sem_profissional_inativo(anonimo, paciente, criar, profissional_inativo):
    artigo = criar('Ansiedade')
    profissional_inativo()
    assert artigo['artigo_id'] not in [a['artigo_id'] for a in destaques(anonimo)]
    assert artigo['artigo_id'] not in ids(paciente)


def test_destaques_limite(anonimo, criar):
    for numero in range(13):
        criar(f'Artigo {numero}')
    assert len(destaques(anonimo)) == 12
    assert len(destaques(anonimo, '?limite=2')) == 2


@pytest.mark.parametrize('limite', ['0', '13', '-1', 'x'])
def test_destaques_limite_invalido(anonimo, limite):
    assert anonimo.get(f'/api/artigos/destaques?limite={limite}').status_code == 400

