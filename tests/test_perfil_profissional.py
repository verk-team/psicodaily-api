# Perfil completo: formacao, experiencia, idiomas, areas de atuacao e video.
import random
from datetime import date, timedelta

import pytest

from apoio import PROFISSIONAL, executar
from funcao import AREAS_ATUACAO, normalizar_ano_inicio, normalizar_lista, normalizar_video_url, validar_cpf
from services import profissionais_service

COLUNAS = 'ABORDAGEM, MODALIDADE, FORMACAO, ANO_INICIO_ATUACAO, IDIOMAS, AREAS_ATUACAO, VIDEO_URL'
YT = 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ'
IDIOMAS = ['Português', 'Inglês', 'Espanhol', 'Francês', 'Italiano', 'Alemão', 'Libras']


def cpf_valido():
    while True:
        cpf = ''.join(random.choice('0123456789') for _ in range(9))
        for i in (9, 10):
            soma = sum(int(cpf[j]) * (i + 1 - j) for j in range(i))
            cpf += str(soma * 10 % 11 % 10)
        if validar_cpf(cpf):
            return cpf


@pytest.fixture
def perfil_original():
    original = executar(f'SELECT {COLUNAS} FROM PROFISSIONAL WHERE USUARIO_ID = ?', (PROFISSIONAL,))[0]
    yield
    executar(f'''UPDATE PROFISSIONAL SET {", ".join(c + " = ?" for c in COLUNAS.split(", "))}
        WHERE USUARIO_ID = ?''', (*original, PROFISSIONAL))


def detalhe(anonimo, id_usuario=PROFISSIONAL):
    return anonimo.get(f'/api/profissionais/{id_usuario}').get_json()['profissionais']


@pytest.mark.parametrize('entrada, esperado', [
    ('https://www.youtube.com/watch?v=dQw4w9WgXcQ', YT),
    ('https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s', YT),
    ('https://m.youtube.com/watch?v=dQw4w9WgXcQ', YT),
    ('https://youtu.be/dQw4w9WgXcQ?si=abc', YT),
    ('https://www.youtube.com/shorts/dQw4w9WgXcQ', YT),
    ('https://www.youtube.com/embed/dQw4w9WgXcQ', YT),
    ('  https://youtu.be/dQw4w9WgXcQ  ', YT),
    ('https://vimeo.com/76979871', 'https://player.vimeo.com/video/76979871'),
    ('https://vimeo.com/76979871/af3c2b1d9e', 'https://player.vimeo.com/video/76979871?h=af3c2b1d9e'),
    ('https://player.vimeo.com/video/76979871?h=af3c2b1d9e&badge=0', 'https://player.vimeo.com/video/76979871?h=af3c2b1d9e'),
    ('https://www.youtube.com/watch?v=curto', None),
    ('https://www.youtube.com/watch?v=dQw4w9WgXcQ"><script>', None),
    ('https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ', None),
    ('https://evil.com/youtu.be/dQw4w9WgXcQ', None),
    ('javascript:alert(1)//youtu.be/dQw4w9WgXcQ', None),
    ('https://www.instagram.com/reel/abc', None),
    ('https://vimeo.com/channels/staffpicks/76979871', None),
    ('https://vimeo.com/76979871/<b>', None),
    ('youtu.be/dQw4w9WgXcQ', None),
    (12345, None),
])
def test_normalizar_video_url(entrada, esperado):
    assert normalizar_video_url(entrada) == esperado


def test_normalizar_lista():
    assert normalizar_lista(['ingles', ' LIBRAS ', 'Inglês'], IDIOMAS, 7) == 'Inglês,Libras'
    assert normalizar_lista('portugues, espanhol', IDIOMAS, 7) == 'Português,Espanhol'
    assert normalizar_lista('["Alemao"]', IDIOMAS, 7) == 'Alemão'
    for invalido in (['Klingon'], [1], {'a': 1}, '[quebrado'):
        assert normalizar_lista(invalido, IDIOMAS, 7) is None, invalido
    assert normalizar_lista(AREAS_ATUACAO[:9], AREAS_ATUACAO, 8) is None


@pytest.mark.parametrize('invalido', [1949, date.today().year + 1, '20x5', True, 2015.5, None])
def test_normalizar_ano_inicio(invalido):
    assert normalizar_ano_inicio('2015') == 2015 and normalizar_ano_inicio(1950) == 1950
    assert normalizar_ano_inicio(invalido) is None


def test_patch_preenche_e_limpa(anonimo, profissional, perfil_original):
    r = profissional.patch('/api/profissionais/me', json={
        # Aspas curvas, travessao e emoji (F0.1)
        'formacao': '  Psicologia – USP (2012); especialização em “TCC” 😊  ',
        'ano_inicio_atuacao': 2014,
        'idiomas': ['portugues', 'Inglês', 'libras'],
        'areas_atuacao': 'ansiedade, depressao, TDAH',
        'video_url': 'https://youtu.be/dQw4w9WgXcQ',
    })
    assert r.status_code == 200, r.get_json()
    perfil = detalhe(anonimo)
    assert perfil['formacao'] == 'Psicologia – USP (2012); especialização em “TCC” 😊', perfil
    assert perfil['ano_inicio_atuacao'] == 2014, perfil
    assert perfil['anos_experiencia'] == date.today().year - 2014, perfil
    assert perfil['idiomas'] == ['Português', 'Inglês', 'Libras'], perfil
    assert perfil['areas_atuacao'] == ['Ansiedade', 'Depressão', 'TDAH'], perfil
    assert perfil['video_url'] == YT, perfil

    busca = anonimo.get('/api/profissionais/?page_size=100').get_json()['profissionais']
    na_busca = next(p for p in busca if p['id_usuario'] == PROFISSIONAL)
    assert {k: na_busca[k] for k in perfil} == perfil, (na_busca, perfil)
    # Quem nao preencheu recebe listas vazias e null, nao chaves faltando
    outro = next((p for p in busca if p['id_usuario'] != PROFISSIONAL and not p['idiomas']), None)
    if outro:
        assert outro['areas_atuacao'] == [] and outro['anos_experiencia'] is None, outro

    # null, lista vazia ou texto vazio limpam
    r = profissional.patch('/api/profissionais/me', json={'formacao': None, 'ano_inicio_atuacao': None,
                                                         'idiomas': [], 'areas_atuacao': '', 'video_url': ' '})
    assert r.status_code == 200, r.get_json()
    limpo = detalhe(anonimo)
    assert (limpo['formacao'], limpo['ano_inicio_atuacao'], limpo['anos_experiencia'], limpo['idiomas'],
            limpo['areas_atuacao'], limpo['video_url']) == (None, None, None, [], [], None), limpo


@pytest.mark.parametrize('corpo', [
    {'idiomas': ['Klingon']},
    {'idiomas': 5},
    {'areas_atuacao': AREAS_ATUACAO[:9]},
    {'ano_inicio_atuacao': 1900},
    {'ano_inicio_atuacao': date.today().year + 1},
    {'ano_inicio_atuacao': 'dois mil'},
    {'video_url': 'https://www.instagram.com/reel/abc'},
    {'video_url': ['https://youtu.be/dQw4w9WgXcQ']},
    {'formacao': 'ab'},
    {'formacao': 'x' * 1001},
    {'campo_inventado': 1},
])
def test_patch_invalido_nao_altera_nada(anonimo, profissional, perfil_original, corpo):
    antes = detalhe(anonimo)
    r = profissional.patch('/api/profissionais/me', json=corpo)
    assert r.status_code == 400 and r.get_json()['error'], (r.status_code, r.get_json())
    assert detalhe(anonimo) == antes


@pytest.fixture
def envios(monkeypatch):
    enviados = []
    monkeypatch.setattr(profissionais_service, 'enviar_ativacao', enviados.append)
    return enviados


@pytest.fixture
def email_novo():
    email = f'teste.perfil.{random.randint(0, 10 ** 9)}@exemplo.com'
    yield email
    executar('DELETE FROM USUARIO WHERE EMAIL = ?', (email,))


def test_cadastro_multipart(anonimo, envios, email_novo):
    # Chave repetida para lista, CSV e campos opcionais ausentes
    formulario = {
        'nome': 'Teste Perfil', 'email': email_novo, 'telefone': '(11) 98888-7777', 'cpf': cpf_valido(),
        'senha': 'Senha@123', 'confirmar_senha': 'Senha@123', 'especialidade': 'Psicologia',
        'crp': '06/123456', 'descricao': 'Atendimento de adultos e adolescentes.', 'valor': '150',
        'dias': 'Segunda', 'idiomas': ['Português', 'Inglês'], 'areas_atuacao': 'Luto, Trauma',
        'ano_inicio_atuacao': '2010', 'video_url': 'https://vimeo.com/76979871',
    }
    invalido = anonimo.post('/api/profissionais/', data={**formulario, 'video_url': 'https://tiktok.com/x'})
    assert invalido.status_code == 400, invalido.get_json()
    assert executar('SELECT 1 FROM USUARIO WHERE EMAIL = ?', (email_novo,)) == [], 'cadastro invalido gravou usuario'

    r = anonimo.post('/api/profissionais/', data=formulario)
    assert r.status_code == 201, r.get_json()
    criado = r.get_json()
    novo_usuario = criado['usuario']['id_usuario']
    assert len(envios) == 1 and envios[0]['email'] == email_novo, envios
    perfil = criado['profissional']
    assert perfil['idiomas'] == ['Português', 'Inglês'] and perfil['areas_atuacao'] == ['Luto', 'Trauma'], perfil
    assert perfil['anos_experiencia'] == date.today().year - 2010 and perfil['formacao'] is None, perfil
    assert perfil['video_url'] == 'https://player.vimeo.com/video/76979871', perfil
    gravado = executar(f'SELECT {COLUNAS} FROM PROFISSIONAL WHERE USUARIO_ID = ?', (novo_usuario,))[0]
    assert gravado == (None, None, None, 2010, 'Português,Inglês', 'Luto,Trauma',
                       'https://player.vimeo.com/video/76979871'), gravado
    # Dias do cadastro viram janelas das 8h as 18h (F1); 1 = segunda
    janelas = anonimo.get(f'/api/profissionais/{novo_usuario}/disponibilidade')
    assert janelas.status_code == 404 or janelas.get_json()['janelas'] == [
        {'dia_semana': 1, 'inicio': '08:00', 'fim': '18:00'}], janelas.get_json()
    assert executar('SELECT COUNT(*) FROM DISPONIBILIDADE WHERE PROFISSIONAL_ID = ?', (novo_usuario,)) == [(1,)]
    # 9: todo cadastro comeca com 30 dias de Essencial gratis
    assert executar('SELECT PLANO, VALIDA_ATE FROM ASSINATURA WHERE PROFISSIONAL_ID = ?', (novo_usuario,)) == [
        ('ESSENCIAL', date.today() + timedelta(days=30))]


@pytest.fixture
def meta_original():
    original = executar('SELECT META_MENSAL_CENTAVOS FROM PROFISSIONAL WHERE USUARIO_ID = ?', (PROFISSIONAL,))[0][0]
    yield
    executar('UPDATE PROFISSIONAL SET META_MENSAL_CENTAVOS = ? WHERE USUARIO_ID = ?', (original, PROFISSIONAL))


def test_meta_mensal_editavel_e_privada(anonimo, paciente, profissional, meta_original):
    assert profissional.patch('/api/profissionais/me', json={'meta_mensal': '5.000,50'}).status_code == 200
    assert profissional.get('/api/usuarios/me').get_json()['usuario']['meta_mensal_centavos'] == 500050
    # Nao aparece no perfil publico
    assert 'meta_mensal_centavos' not in anonimo.get(f'/api/profissionais/{PROFISSIONAL}').get_json()['profissionais']
    for invalida in ('-1', 'abc', '100000,01', True):
        assert profissional.patch('/api/profissionais/me', json={'meta_mensal': invalida}).status_code == 400, invalida
    assert profissional.patch('/api/profissionais/me', json={'meta_mensal': ''}).status_code == 200
    assert profissional.get('/api/usuarios/me').get_json()['usuario']['meta_mensal_centavos'] is None
    assert paciente.get('/api/usuarios/me').get_json()['usuario']['meta_mensal_centavos'] is None
    assert paciente.patch('/api/profissionais/me', json={'meta_mensal': '10'}).status_code == 403
