# Features 18 e 19: so conversa quem tem sessao nao cancelada; historico paginado, polling por `depois`, leitura e anexos.
import json
import time
from datetime import datetime, timedelta
from io import BytesIO

import boto3
import pytest
import simple_websocket

from apoio import ADMIN, PACIENTE, PORTA, PROFISSIONAL, PSIQUIATRA, executar
from botocore.exceptions import ClientError

from services import chat_service
from services.erros import ErroNegocio
from services.auth_service import gerar_token


@pytest.fixture
def vinculo(sessoes):
    inicio = (datetime.now() + timedelta(days=5)).replace(hour=10, minute=0, second=0, microsecond=0)
    sessoes.append(executar('''INSERT INTO SESSAO (PACIENTE_ID, PROFISSIONAL_ID, DATA_HORA_INICIO, DATA_HORA_FIM, STATUS, VALOR_CENTAVOS)
        VALUES (?, ?, ?, ?, 'AGENDADO', 15000) RETURNING SESSAO_ID''', (PACIENTE, PROFISSIONAL, inicio, inicio + timedelta(hours=1)))[0][0])


@pytest.fixture
def ultima_mensagem():
    ultima = executar('SELECT COALESCE(MAX(MENSAGEM_ID), 0) FROM MENSAGEM')[0][0]
    yield ultima
    executar('DELETE FROM MENSAGEM WHERE MENSAGEM_ID > ?', (ultima,))


def test_permissoes_e_vinculo(anonimo, admin, outro_paciente, paciente, vinculo, ultima_mensagem):
    assert anonimo.get('/api/chat/').status_code == 401
    assert admin.get('/api/chat/').status_code == 403
    # Sem sessao com o profissional, nem le nem envia
    assert outro_paciente.post(f'/api/chat/{PROFISSIONAL}', json={'texto': 'oi'}).status_code == 404
    assert outro_paciente.get(f'/api/chat/{PROFISSIONAL}').status_code == 404
    assert paciente.post(f'/api/chat/{PSIQUIATRA}', json={'texto': 'oi'}).status_code == 404
    for invalido in ({}, {'texto': '   '}, {'texto': 'x' * 2001}, {'texto': 'oi', 'arquivo': 'a.pdf'}):
        assert paciente.post(f'/api/chat/{PROFISSIONAL}', json=invalido).status_code == 400, invalido
    assert paciente.get(f'/api/chat/{PROFISSIONAL}?antes=1&depois=1').status_code == 400


def test_conversa(paciente, profissional, vinculo, ultima_mensagem):
    r = paciente.post(f'/api/chat/{PROFISSIONAL}', json={'texto': ' Olá – tudo “bem”? 😊 '})
    assert r.status_code == 201, r.get_json()
    primeira = r.get_json()['mensagem']
    assert primeira['texto'] == 'Olá – tudo “bem”? 😊' and primeira['remetente_id'] == PACIENTE
    for n in range(3):
        paciente.post(f'/api/chat/{PROFISSIONAL}', json={'texto': f'msg {n}'})

    conversa = next(c for c in profissional.get('/api/chat/').get_json()['conversas'] if c['id_usuario'] == PACIENTE)
    assert conversa['nao_lidas'] == 4 and conversa['ultima_mensagem']['texto'] == 'msg 2', conversa

    # Ultimas 2 em ordem cronologica, depois o historico anterior
    ultimas = profissional.get(f'/api/chat/{PACIENTE}?limite=2').get_json()['mensagens']
    assert [m['texto'] for m in ultimas] == ['msg 1', 'msg 2']
    anteriores = profissional.get(f'/api/chat/{PACIENTE}?antes={ultimas[0]["mensagem_id"]}').get_json()['mensagens']
    assert [m['texto'] for m in anteriores] == [primeira['texto'], 'msg 0']
    conversa = next(c for c in profissional.get('/api/chat/').get_json()['conversas'] if c['id_usuario'] == PACIENTE)
    assert conversa['nao_lidas'] == 0

    # Polling: so o que chegou depois da ultima vista
    resposta = profissional.post(f'/api/chat/{PACIENTE}', json={'texto': 'Tudo sim'}).get_json()['mensagem']
    novas = paciente.get(f'/api/chat/{PROFISSIONAL}?depois={ultimas[-1]["mensagem_id"]}').get_json()['mensagens']
    assert [m['mensagem_id'] for m in novas] == [resposta['mensagem_id']]


@pytest.fixture
def conectar(servidor):
    clientes = []

    def abrir(id_usuario, papel, origem=None, token=True):
        cabecalhos = {'Origin': origem} if origem else {}
        if token:
            # Como o app mobile: Bearer e sem Origin
            cabecalhos['Authorization'] = 'Bearer ' + gerar_token({'id_usuario': id_usuario, 'usuario_role': papel})
        ws = simple_websocket.Client.connect(f'ws://127.0.0.1:{PORTA}/ws/chat', headers=cabecalhos)
        clientes.append(ws)
        return ws

    yield abrir
    for ws in clientes:
        try:
            ws.close()
        except Exception:
            pass


def fechado_pelo_servidor(ws):
    try:
        return ws.receive(timeout=3) is None
    except simple_websocket.ConnectionClosed:
        return True


def conectados(usuario_id):
    with chat_service._lock:
        return len(chat_service._conexoes.get(usuario_id, ()))


def test_tempo_real(conectar, paciente, vinculo, ultima_mensagem):
    assert fechado_pelo_servidor(conectar(PACIENTE, 'PACIENTE', token=False))
    assert fechado_pelo_servidor(conectar(PROFISSIONAL, 'PSICOLOGO', origem='https://site-malicioso.example'))
    assert fechado_pelo_servidor(conectar(ADMIN, 'ADMIN'))

    profissional_ws = conectar(PROFISSIONAL, 'PSICOLOGO', origem='http://localhost:5173')
    outra_aba = conectar(PACIENTE, 'PACIENTE')
    # O registro acontece logo depois do handshake; espera para o POST nao chegar antes
    for _ in range(50):
        if conectados(PROFISSIONAL) and conectados(PACIENTE):
            break
        time.sleep(0.05)
    enviada = paciente.post(f'/api/chat/{PROFISSIONAL}', json={'texto': 'chegou na hora?'}).get_json()['mensagem']

    for ws in (profissional_ws, outra_aba):
        evento = json.loads(ws.receive(timeout=3))
        assert evento == {'type': 'mensagem', 'paciente_id': PACIENTE, 'profissional_id': PROFISSIONAL,
                          'mensagem': enviada}, evento


PDF = b'%PDF-1.4\n%teste\n'
PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 16


@pytest.fixture
def bucket(monkeypatch):
    """Uploads em memoria; a URL assinada e gerada de verdade, com chaves falsas (o boto3 nao chama o R2 para isso)."""
    enviados = {}
    servico = chat_service.upload_service
    monkeypatch.setattr(servico, 'enviar_privado', lambda conteudo, chave, tipo: enviados.update({chave: (conteudo, tipo)}))
    monkeypatch.setattr(servico, 's3', boto3.client('s3', endpoint_url='https://conta.r2.cloudflarestorage.com',
                                                    aws_access_key_id='teste', aws_secret_access_key='teste',
                                                    region_name='auto'))
    return enviados


def anexar(cliente, outro_id, conteudo, nome='laudo.pdf', **form):
    return cliente.post(f'/api/chat/{outro_id}/arquivo', data={'arquivo': (BytesIO(conteudo), nome), **form},
                        content_type='multipart/form-data')


def test_arquivo(anonimo, paciente, outro_paciente, profissional, vinculo, ultima_mensagem, bucket):
    assert paciente.post(f'/api/chat/{PROFISSIONAL}/arquivo', data={}, content_type='multipart/form-data').status_code == 400
    # O tipo vem do conteudo: extensao .pdf nao basta
    assert anexar(paciente, PROFISSIONAL, b'MZ\x90\x00 executavel', 'virus.pdf').status_code == 400
    assert anexar(paciente, PROFISSIONAL, PDF + b'0' * chat_service.MAXIMO_ARQUIVO).status_code == 413
    assert anexar(paciente, PROFISSIONAL, PDF, texto='x' * 2001).status_code == 400
    assert anexar(outro_paciente, PROFISSIONAL, PDF).status_code == 404
    assert bucket == {}

    r = anexar(paciente, PROFISSIONAL, PDF, r'C:\Users\eu\Exame de sangue.pdf', texto=' segue o exame ')
    assert r.status_code == 201, r.get_json()
    enviada = r.get_json()['mensagem']
    assert enviada['texto'] == 'segue o exame', enviada
    assert enviada['arquivo'] == {'nome': 'Exame de sangue.pdf', 'tipo': 'application/pdf', 'tamanho': len(PDF)}
    (chave, (conteudo, tipo)), = bucket.items()
    assert chave.startswith(f'chat/{PACIENTE}-{PROFISSIONAL}/') and chave.endswith('.pdf'), chave
    assert (conteudo, tipo) == (PDF, 'application/pdf')
    foto = anexar(profissional, PACIENTE, PNG, 'foto.pdf').get_json()['mensagem']
    assert foto['texto'] == '' and foto['arquivo']['tipo'] == 'image/png', foto
    assert [m['arquivo'] for m in profissional.get(f'/api/chat/{PACIENTE}').get_json()['mensagens']] == [
        enviada['arquivo'], foto['arquivo']]

    # Download: so participantes, por URL assinada de curta duracao
    assert anonimo.get(f'/api/chat/arquivo/{enviada["mensagem_id"]}').status_code == 401
    assert outro_paciente.get(f'/api/chat/arquivo/{enviada["mensagem_id"]}').status_code == 404
    texto = paciente.post(f'/api/chat/{PROFISSIONAL}', json={'texto': 'sem anexo'}).get_json()['mensagem']
    assert paciente.get(f'/api/chat/arquivo/{texto["mensagem_id"]}').status_code == 404
    r = profissional.get(f'/api/chat/arquivo/{enviada["mensagem_id"]}')
    assert r.status_code == 200 and r.headers['Cache-Control'] == 'private, no-store', r.get_json()
    corpo = r.get_json()
    assert corpo['nome'] == 'Exame de sangue.pdf' and corpo['expira_em_segundos'] == 300, corpo
    for trecho in (chat_service.upload_service.bucket_privado, chave, 'X-Amz-Expires=300', 'response-content-disposition='):
        assert trecho in corpo['url'], (trecho, corpo['url'])


@pytest.mark.parametrize('inicio, tipo', [
    (b'OggS\x00\x02', 'audio/ogg'), (b'\x1aE\xdf\xa3\x01', 'audio/webm'), (b'ID3\x04\x00', 'audio/mpeg'),
    (b'\xff\xfb\x90\x64', 'audio/mpeg'), (b'\x00\x00\x00\x20ftypM4A ', 'audio/mp4'), (b'%PDF-1.4', 'application/pdf'),
    (b'GIF89a', None), (b'MZ\x90\x00', None),
])
def test_tipo_pelos_primeiros_bytes(inicio, tipo):
    assert chat_service._tipo_do_arquivo(inicio)[0] == tipo


def test_falha_no_r2_vira_503(monkeypatch):
    def negar(*args, **kwargs):
        raise ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'Access Denied'}}, 'PutObject')
    monkeypatch.setattr(chat_service.upload_service.s3, 'upload_fileobj', negar)
    with pytest.raises(ErroNegocio) as erro:
        chat_service.upload_service.enviar_privado(b'%PDF-1.4', 'chat/teste.pdf', 'application/pdf')
    assert erro.value.status == 503
