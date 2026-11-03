# Sobe o app numa porta local e conecta clientes WebSocket como o front.
import io
import json
from datetime import datetime, timedelta
from urllib.error import URLError

import pytest
import simple_websocket

from apoio import OUTRO_PACIENTE, PACIENTE, PORTA, PROFISSIONAL, executar
from services import consultas_service
from services.auth_service import gerar_token

@pytest.fixture
def emails(monkeypatch):
    enviados = []
    monkeypatch.setattr(consultas_service.email_service, 'enviar_email_async',
                        lambda destino, assunto, corpo: enviados.append((destino, assunto, corpo)))
    return enviados


@pytest.fixture
def conectar(servidor):
    clientes = []

    def abrir(id_usuario, papel, sessao_id, origem=None):
        token = gerar_token({'id_usuario': id_usuario, 'usuario_role': papel})
        cabecalhos = {'Cookie': f'access_token={token}', **({'Origin': origem} if origem else {})}
        ws = simple_websocket.Client.connect(f'ws://127.0.0.1:{PORTA}/ws/signaling?sessao_id={sessao_id}',
                                             headers=cabecalhos)
        clientes.append(ws)
        return ws

    yield abrir
    # Cliente aberto segura o processo: fecha todos para uma falha terminar o teste
    for ws in clientes:
        try:
            ws.close()
        except Exception:
            pass


def nova_sessao(sessoes, inicio, fim, status='CONFIRMADO'):
    sessao_id = executar('''INSERT INTO SESSAO (PACIENTE_ID, PROFISSIONAL_ID, DATA_HORA_INICIO, DATA_HORA_FIM,
        STATUS, VALOR_CENTAVOS) VALUES (?, ?, ?, ?, ?, 100) RETURNING SESSAO_ID''',
                         (PACIENTE, PROFISSIONAL, inicio, fim, status))[0][0]
    sessoes.append(sessao_id)
    return sessao_id


def receber(ws):
    texto = ws.receive(timeout=3)
    assert texto is not None, 'nada recebido em 3s'
    return json.loads(texto)


def fechado_pelo_servidor(ws):
    try:
        return ws.receive(timeout=3) is None
    except simple_websocket.ConnectionClosed:
        return True


def test_presenca_aviso_por_email_e_repasse(conectar, emails, sessoes):
    agora = datetime.now().replace(second=0, microsecond=0)
    sessao = nova_sessao(sessoes, agora - timedelta(minutes=5), agora + timedelta(minutes=50))
    futura = nova_sessao(sessoes, agora + timedelta(days=1), agora + timedelta(days=1, hours=1))
    email_profissional = executar('SELECT EMAIL FROM USUARIO WHERE ID_USUARIO = ?', (PROFISSIONAL,))[0][0]

    # Quem nao participa ou chega fora da janela e desconectado
    assert fechado_pelo_servidor(conectar(OUTRO_PACIENTE, 'PACIENTE', sessao))
    assert fechado_pelo_servidor(conectar(PACIENTE, 'PACIENTE', futura))
    assert emails == [], emails

    # 1. Paciente entra sozinho: sala vazia e o profissional recebe um e-mail
    paciente = conectar(PACIENTE, 'PACIENTE', sessao)
    assert receber(paciente) == {'type': 'peers', 'participantes': []}
    assert len(emails) == 1 and emails[0][0] == email_profissional, emails
    assert f'/videochamada/{sessao}' in emails[0][2], emails[0][2]

    # 2. Reconectar nao repete o e-mail
    paciente.close()
    paciente = conectar(PACIENTE, 'PACIENTE', sessao)
    assert receber(paciente)['participantes'] == []
    assert len(emails) == 1, emails

    # 3. Profissional entra: os dois ficam sabendo, sem e-mail (o paciente ja esta la)
    profissional = conectar(PROFISSIONAL, 'PSICOLOGO', sessao)
    peers = receber(profissional)
    assert peers['type'] == 'peers' and [p['id_usuario'] for p in peers['participantes']] == [PACIENTE], peers
    entrou = receber(paciente)
    assert entrou['type'] == 'peer-joined' and entrou['id_usuario'] == PROFISSIONAL and entrou['nome'], entrou
    assert 'email' not in entrou and 'email' not in peers['participantes'][0], (entrou, peers)
    assert len(emails) == 1, emails

    # 4. Repasse de offer/ICE continua; JSON invalido e tipo desconhecido sao ignorados
    paciente.send('nao e json')
    paciente.send(json.dumps({'type': 'outro'}))
    offer = {'type': 'offer', 'data': {'type': 'offer', 'sdp': 'v=0'}}
    paciente.send(json.dumps(offer))
    assert receber(profissional) == offer
    ice = {'type': 'ice-candidate', 'data': {'candidate': 'x'}}
    profissional.send(json.dumps(ice))
    assert receber(paciente) == ice

    # 5. Quem fica e avisado da saida
    profissional.close()
    saiu = receber(paciente)
    assert saiu['type'] == 'peer-left' and saiu['id_usuario'] == PROFISSIONAL, saiu
    paciente.close()


def test_origem_estranha_e_recusada(conectar, emails, sessoes):
    # S7: outro site aberto no navegador do participante leva o cookie, mas nao entra na sala
    agora = datetime.now().replace(second=0, microsecond=0)
    sessao = nova_sessao(sessoes, agora - timedelta(minutes=5), agora + timedelta(minutes=50))
    assert fechado_pelo_servidor(conectar(PACIENTE, 'PACIENTE', sessao, 'https://site-malicioso.example'))
    assert receber(conectar(PACIENTE, 'PACIENTE', sessao, 'https://psicodaily-front.vercel.app'))['type'] == 'peers'


def test_ice_servers(paciente, outro_paciente, sessoes, monkeypatch):
    agora = datetime.now().replace(second=0, microsecond=0)
    sessao = nova_sessao(sessoes, agora - timedelta(minutes=5), agora + timedelta(minutes=50))
    rota = f'/api/consultas/{sessao}/ice-servers'

    # Quem nao participa nao gasta a cota do TURN
    assert outro_paciente.get(rota).status_code == 404
    # Consulta sem pagamento nao abre sala
    sem_pagamento = nova_sessao(sessoes, agora + timedelta(minutes=5), agora + timedelta(minutes=50), 'AGENDADO')
    assert paciente.get(f'/api/consultas/{sem_pagamento}/ice-servers').status_code == 404

    # Sem chave da Cloudflare (dev, CI): so STUN
    monkeypatch.delenv('TURN_KEY_ID', raising=False)
    assert paciente.get(rota).get_json() == {'ice_servers': consultas_service.STUN_PUBLICO}

    # Com chave: devolve o que a Cloudflare gerou
    monkeypatch.setenv('TURN_KEY_ID', 'chave')
    da_cloudflare = [{'urls': ['turn:turn.cloudflare.com:3478'], 'username': 'u', 'credential': 'c'}]
    monkeypatch.setattr(consultas_service, 'urlopen',
                        lambda req, timeout: io.BytesIO(json.dumps({'iceServers': da_cloudflare}).encode()))
    assert paciente.get(rota).get_json() == {'ice_servers': da_cloudflare}

    # Cloudflare fora do ar: a consulta continua so com STUN
    def fora_do_ar(req, timeout):
        raise URLError('sem rede')
    monkeypatch.setattr(consultas_service, 'urlopen', fora_do_ar)
    assert paciente.get(rota).get_json() == {'ice_servers': consultas_service.STUN_PUBLICO}
