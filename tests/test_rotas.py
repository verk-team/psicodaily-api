# Caracterizacao das rotas (status + chaves) para a refatoracao view -> services.
from datetime import datetime, timedelta

import pytest

from apoio import OUTRO_PACIENTE, PACIENTE, PROFISSIONAL, app, checar, executar, gerar_token


@pytest.fixture
def registros():
    criados = []
    yield criados
    for registro_id in criados:
        executar('DELETE FROM REGISTRO WHERE REGISTRO_ID = ?', (registro_id,))


@pytest.fixture
def nome_e_email_do_paciente():
    original = executar('SELECT NOME, EMAIL FROM USUARIO WHERE ID_USUARIO = ?', (PACIENTE,))[0]
    yield original
    executar('UPDATE USUARIO SET NOME = ?, EMAIL = ? WHERE ID_USUARIO = ?', (*original, PACIENTE))


def criar_registro(paciente, registros, humor, sono):
    registro = checar(paciente.post('/api/registros/', json={'humor': humor, 'qualidade_sono': sono}), 201, 'registro')['registro']
    registros.append(registro['registro_id'])
    return registro


def test_saude(anonimo):
    assert checar(anonimo.get('/api/saude'), 200, 'status')['status'] == 'ok'


def test_auth(anonimo, paciente):
    checar(anonimo.post('/api/auth/login', json={}), 400, 'error')
    checar(anonimo.post('/api/auth/login', json={'email': 'naoexiste@x.com', 'senha': 'a'}), 404, 'error')
    checar(anonimo.post('/api/auth/verificar_codigo', json={}), 400, 'error')
    checar(anonimo.post('/api/auth/alterar_senha', json={}), 400, 'error')
    checar(anonimo.post('/api/auth/esqueci_senha', json={}), 400, 'error')
    checar(paciente.post('/api/auth/alterar_senha_logado', json={}), 400, 'error')
    checar(anonimo.post('/api/auth/alterar_senha_logado', json={}), 401, 'error')
    checar(anonimo.post('/api/auth/logout'), 200, 'message')


def test_auth_bearer(anonimo):
    with app.app_context():
        token = gerar_token({'id_usuario': PACIENTE, 'usuario_role': 'PACIENTE'})
    checar(anonimo.get('/api/usuarios/me', headers={'Authorization': f'Bearer {token}'}), 200)
    checar(anonimo.get('/api/usuarios/me', headers={'Authorization': 'Bearer lixo'}), 401, 'error')


def test_usuarios(anonimo, paciente, nome_e_email_do_paciente):
    checar(anonimo.get('/api/usuarios/me'), 401, 'error')
    me = checar(paciente.get('/api/usuarios/me'), 200, 'usuario')['usuario']
    for chave in ('id_usuario', 'nome', 'email', 'tipo_usuario', 'telefone', 'cpf', 'foto_url'):
        assert chave in me, chave
    checar(paciente.patch('/api/usuarios/me', json={'role': 'ADMIN'}), 400, 'error')
    checar(paciente.patch('/api/usuarios/me', json={'nome': '1'}), 400, 'error')
    # Texto fora do Latin-1 (F0.1): letras nao latinas no nome e no e-mail gravam e voltam iguais
    unicode = {'nome': 'Zoë Ñúñez 李', 'email': 'zoë.李@exemplo.com'}
    checar(paciente.patch('/api/usuarios/me', json=unicode), 200, 'message')
    lido = checar(paciente.get('/api/usuarios/me'), 200, 'usuario')['usuario']
    assert (lido['nome'], lido['email']) == (unicode['nome'], unicode['email']), lido
    checar(paciente.get('/api/usuarios/'), 403, 'error')
    checar(anonimo.post('/api/usuarios/', data={}), 400, 'error')
    checar(anonimo.post('/api/usuarios/logout'), 200, 'message')


def test_profissionais(anonimo, paciente, profissional):
    lista = checar(anonimo.get('/api/profissionais/'), 200, 'page', 'page_size', 'total', 'total_pages', 'profissionais')
    assert lista['profissionais'], lista
    checar(anonimo.get('/api/profissionais/?page=0'), 400, 'error')
    checar(anonimo.get('/api/profissionais/?especialidade=x'), 400, 'error')
    checar(anonimo.get(f'/api/profissionais/{PROFISSIONAL}'), 200, 'profissionais')
    checar(anonimo.get('/api/profissionais/999999'), 404, 'error')
    checar(anonimo.post('/api/profissionais/', data={}), 400, 'error')
    checar(paciente.patch('/api/profissionais/me', json={'descricao': 'x' * 20}), 403, 'error')
    checar(profissional.patch('/api/profissionais/me', json={'foo': 1}), 400, 'error')


def test_bloqueios_e_destaques(anonimo, paciente, profissional, bloqueios):
    rota = '/api/profissionais/me/bloqueios'
    checar(anonimo.get(rota), 401, 'error')
    checar(paciente.get(rota), 403, 'error')
    checar(profissional.get(rota), 200, 'bloqueios')
    checar(profissional.post(rota, json={}), 400, 'error')
    dia = (datetime.now() + timedelta(days=400)).date().isoformat()
    criado = checar(profissional.post(rota, json={'inicio': dia, 'fim': dia}), 201, 'bloqueio', 'sessoes_no_periodo')
    assert set(criado['bloqueio']) == {'bloqueio_id', 'inicio', 'fim'}, criado
    checar(profissional.post(rota, json={'inicio': dia, 'fim': dia}), 409, 'error')
    checar(paciente.delete(f"{rota}/{criado['bloqueio']['bloqueio_id']}"), 403, 'error')
    checar(profissional.delete(f"{rota}/{criado['bloqueio']['bloqueio_id']}"), 200, 'message')
    checar(profissional.delete(f"{rota}/{criado['bloqueio']['bloqueio_id']}"), 404, 'error')

    checar(anonimo.get('/api/conteudos/destaques'), 200, 'conteudos')
    checar(anonimo.get('/api/conteudos/destaques?limite=13'), 400, 'error')


def test_registros_e_evolucao(paciente, profissional, registros):
    checar(profissional.get('/api/registros/'), 403, 'error')
    checar(paciente.get('/api/registros/?limite=0'), 400, 'error')
    checar(paciente.get('/api/registros/'), 200, 'registros', 'proximo_cursor')
    checar(paciente.post('/api/registros/', json={'humor': 'X'}), 400, 'error')
    checar(profissional.get('/api/registros/evolucao'), 403, 'error')
    checar(paciente.get('/api/registros/evolucao?dias=5'), 400, 'error')
    checar(paciente.get('/api/registros/evolucao?dias=x'), 400, 'error')
    hoje = datetime.now().date().isoformat()

    def humor_de_hoje():
        corpo = checar(paciente.get('/api/registros/evolucao?dias=7'), 200, 'dias', 'resumo', 'evolucaoHumor')
        dia = next((d for d in corpo['evolucaoHumor'] if d['data'] == hoje), {'valor': 0, 'quantidade': 0})
        return corpo['resumo']['totalRegistros'], dia['valor'] * dia['quantidade'], dia['quantidade']

    total_antes, soma_antes, qtd_antes = humor_de_hoje()
    registro = criar_registro(paciente, registros, 'BOM', 'BOA')
    assert registro['usuario_id'] == PACIENTE and registro['humor'] == 'BOM', registro
    criar_registro(paciente, registros, 'RUIM', 'RUIM')
    # BOM vale 4 e RUIM vale 2: a media do dia passa a incluir os dois
    total, soma, qtd = humor_de_hoje()
    assert (total, qtd) == (total_antes + 2, qtd_antes + 2), (total, qtd, total_antes, qtd_antes)
    assert abs(soma - (soma_antes + 6)) < 1e-9, (soma, soma_antes)


def test_consultas_prontuario_e_encaminhamentos(anonimo, paciente, outro_paciente, profissional,
                                                sessoes, registros, encaminhamentos):
    registro = criar_registro(paciente, registros, 'BOM', 'BOA')
    daqui_400_dias = (datetime.now() + timedelta(days=400)).replace(hour=10, minute=0, second=0, microsecond=0)

    # consultas
    checar(anonimo.get('/api/consultas/'), 401, 'error')
    checar(paciente.get('/api/consultas/'), 200, 'consultas')
    checar(paciente.post('/api/consultas/', json={}), 400, 'error')
    checar(paciente.post('/api/consultas/', json={'id_profissional': PROFISSIONAL,
                                                 'timestamp_inicio': 'x', 'timestamp_fim': 'y'}), 400, 'error')
    corpo = {'id_profissional': PROFISSIONAL, 'timestamp_inicio': daqui_400_dias.isoformat(),
             'timestamp_fim': (daqui_400_dias + timedelta(hours=1)).isoformat()}
    checar(profissional.post('/api/consultas/', json=corpo), 403, 'error')
    checar(paciente.post('/api/consultas/', json={**corpo, 'id_profissional': 999999}), 404, 'error')
    sessao = checar(paciente.post('/api/consultas/', json=corpo), 201, 'sessao')['sessao']
    sessoes.append(sessao['sessao_id'])
    for chave in ('sessao_id', 'paciente_id', 'profissional_id', 'status', 'paciente_foto_url', 'profissional_foto_url'):
        assert chave in sessao, chave
    checar(outro_paciente.post('/api/consultas/', json=corpo), 409, 'error')
    sid = sessao['sessao_id']
    checar(paciente.patch(f'/api/consultas/{sid}', json={'status': 'CANCELADO'}), 403, 'error')
    checar(profissional.patch(f'/api/consultas/{sid}', json={'foo': 1}), 400, 'error')
    checar(profissional.patch(f'/api/consultas/{sid}', json={'status': 'REALIZADO'}), 409, 'error')
    atualizada = checar(profissional.patch(f'/api/consultas/{sid}', json={'link_reuniao': 'https://meet.x/a'}), 200, 'sessao')
    assert atualizada['sessao']['link_reuniao'] == 'https://meet.x/a', atualizada
    checar(profissional.patch('/api/consultas/999999', json={'link_reuniao': None}), 404, 'error')
    checar(profissional.post(f'/api/consultas/{sid}/avaliacao', json={'nota': 5}), 403, 'error')
    checar(paciente.post(f'/api/consultas/{sid}/avaliacao', json={'nota': 9}), 400, 'error')
    checar(paciente.post(f'/api/consultas/{sid}/avaliacao', json={'nota': 5}), 409, 'error')
    checar(outro_paciente.post(f'/api/consultas/{sid}/avaliacao', json={'nota': 5}), 404, 'error')
    checar(anonimo.post(f'/api/consultas/{sid}/cancelar'), 401, 'error')
    checar(profissional.post(f'/api/consultas/{sid}/cancelar'), 403, 'error')
    checar(outro_paciente.post(f'/api/consultas/{sid}/cancelar'), 404, 'error')
    checar(anonimo.get(f'/api/consultas/{sid}/ice-servers'), 401, 'error')
    # A sala so abre 15 min antes: sessao daqui a 400 dias nao tem servidor de chamada
    checar(paciente.get(f'/api/consultas/{sid}/ice-servers'), 404, 'error')

    # prontuario (vinculo vem da sessao criada acima)
    checar(paciente.get(f'/api/prontuario/{PACIENTE}'), 403, 'error')
    checar(profissional.get(f'/api/prontuario/{PACIENTE}?dias=5'), 400, 'error')
    checar(profissional.get(f'/api/prontuario/{OUTRO_PACIENTE}'), 403, 'error')
    pront = checar(profissional.get(f'/api/prontuario/{PACIENTE}'), 200, 'paciente', 'ultimaConsulta',
                   'resumo', 'evolucaoHumor', 'registros', 'proximo_cursor')
    assert any(r['registro_id'] == registro['registro_id'] for r in pront['registros']), pront
    # Paciente e profissional enxergam o mesmo calculo
    proprio = paciente.get('/api/registros/evolucao?dias=30').get_json()
    assert (proprio['resumo'], proprio['evolucaoHumor']) == (pront['resumo'], pront['evolucaoHumor']), (proprio, pront)

    # encaminhamentos
    checar(paciente.get('/api/encaminhamentos/'), 403, 'error')
    checar(profissional.get('/api/encaminhamentos/'), 200, 'encaminhamentos')
    checar(profissional.post('/api/encaminhamentos/', json={}), 400, 'error')
    checar(profissional.post('/api/encaminhamentos/', json={'id_usuario': 999999, 'motivo': 'x'}), 404, 'error')
    encam = checar(profissional.post('/api/encaminhamentos/', json={'id_usuario': PACIENTE, 'motivo': 'teste'}),
                   201, 'encaminhamento')['encaminhamento']
    encaminhamentos.append(encam['encaminhamento_id'])


def test_pagamentos_sem_chamar_a_arkhe(anonimo, paciente, profissional):
    checar(anonimo.post('/api/pagamentos/cobranca', json={}), 401, 'error')
    checar(profissional.post('/api/pagamentos/cobranca', json={}), 403, 'error')
    checar(paciente.post('/api/pagamentos/cobranca', json={}), 400, 'error')
    checar(paciente.post('/api/pagamentos/cobranca', json={'id_sessao': 999999}), 404, 'error')
    checar(paciente.get('/api/pagamentos/financeiro'), 403, 'error')
    checar(profissional.get('/api/pagamentos/financeiro?dias=2'), 400, 'error')
    checar(anonimo.get('/api/pagamentos/cobranca/1'), 401, 'error')
    checar(paciente.get('/api/pagamentos/ganhos'), 403, 'error')
    checar(profissional.get('/api/pagamentos/ganhos'), 200, 'resumo', 'saldo', 'consultas', 'repasses')
    for rota in ('/api/admin/reembolsos', '/api/admin/repasses'):
        checar(profissional.get(rota), 403, 'error')
    checar(profissional.post('/api/admin/repasses'), 403, 'error')
    # Webhook do WAHA sem assinatura valida
    checar(anonimo.post('/api/whatsapp/webhook', json={'event': 'message'}), 401, 'error')


def test_assinaturas_sem_chamar_a_arkhe(anonimo, paciente, profissional):
    checar(anonimo.get('/api/assinaturas/planos'), 200, 'planos')
    checar(anonimo.get('/api/assinaturas/me'), 401, 'error')
    checar(paciente.get('/api/assinaturas/me'), 403, 'error')
    checar(profissional.get('/api/assinaturas/me'), 200, 'assinatura')
    checar(profissional.post('/api/assinaturas/me', json={}), 400, 'error')
    checar(paciente.post('/api/assinaturas/me/cancelar'), 403, 'error')
    checar(profissional.get('/api/assinaturas/me/cobranca/999999/pdf'), 404, 'error')
    checar(paciente.get('/api/profissionais/me/desempenho'), 403, 'error')
    checar(profissional.get('/api/profissionais/me/desempenho'), 200, 'visualizacoes', 'consultas')
