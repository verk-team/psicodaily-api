import os
import threading
from datetime import date, timedelta

import pytest
from werkzeug.serving import make_server

os.environ['JOBS'] = '0'  # sem a thread de lembretes: os testes chamam enviar_lembretes direto

from apoio import ADMIN, OUTRO_PACIENTE, PACIENTE, PORTA, PROFISSIONAL, PSIQUIATRA, cliente, executar
from app import app
from services.auth_service import criar_hash_senha


@pytest.fixture(scope='session')
def servidor():
    """App de verdade numa porta local, para os testes de WebSocket."""
    servidor = make_server('127.0.0.1', PORTA, app, threaded=True)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    yield
    servidor.shutdown()


@pytest.fixture
def anonimo():
    return app.test_client()


@pytest.fixture
def paciente():
    return cliente(PACIENTE, 'PACIENTE')


@pytest.fixture
def outro_paciente():
    return cliente(OUTRO_PACIENTE, 'PACIENTE')


@pytest.fixture
def profissional():
    return cliente(PROFISSIONAL, 'PSICOLOGO')


@pytest.fixture
def psiquiatra():
    return cliente(PSIQUIATRA, 'PSIQUIATRA')


@pytest.fixture
def admin():
    return cliente(ADMIN, 'ADMIN')


@pytest.fixture(autouse=True)
def ultima_auditoria():
    """Login, agendamento etc. gravam em AUDITORIA: todo teste apaga as linhas que criou."""
    ultima = executar('SELECT COALESCE(MAX(AUDITORIA_ID), 0) FROM AUDITORIA')[0][0]
    yield ultima
    executar('DELETE FROM AUDITORIA WHERE AUDITORIA_ID > ?', (ultima,))


@pytest.fixture(autouse=True)
def visitas_ao_perfil():
    """Abrir o detalhe de um profissional conta uma visita (9): o contador de hoje volta a ser o de antes."""
    antes = executar('SELECT PROFISSIONAL_ID, TOTAL FROM VISUALIZACAO_PERFIL WHERE DIA = CURRENT_DATE')
    yield
    executar('DELETE FROM VISUALIZACAO_PERFIL WHERE DIA = CURRENT_DATE')
    for profissional_id, total in antes:
        executar('INSERT INTO VISUALIZACAO_PERFIL (PROFISSIONAL_ID, DIA, TOTAL) VALUES (?, CURRENT_DATE, ?)',
                 (profissional_id, total))


@pytest.fixture
def ultima_notificacao():
    """Apaga as notificacoes criadas pelo teste."""
    ultima = executar('SELECT COALESCE(MAX(NOTIFICACAO_ID), 0) FROM NOTIFICACAO')[0][0]
    yield ultima
    executar('DELETE FROM NOTIFICACAO WHERE NOTIFICACAO_ID > ?', (ultima,))


@pytest.fixture
def sessoes():
    """Lista onde o teste guarda os SESSAO_ID que criou; a AVALIACAO sai junto (cascade)."""
    criadas = []
    yield criadas
    for sessao_id in criadas:
        executar('DELETE FROM SESSAO WHERE SESSAO_ID = ?', (sessao_id,))


@pytest.fixture
def encaminhamentos():
    criados = []
    yield criados
    for encaminhamento_id in criados:
        executar('DELETE FROM ENCAMINHAMENTO WHERE ENCAMINHAMENTO_ID = ?', (encaminhamento_id,))


@pytest.fixture
def agenda_vazia():
    """Agenda do profissional 22 vazia durante o teste e restaurada no fim."""
    original = executar('SELECT DIA_SEMANA, HORA_INICIO, HORA_FIM FROM DISPONIBILIDADE WHERE PROFISSIONAL_ID = ?',
                        (PROFISSIONAL,))
    executar('DELETE FROM DISPONIBILIDADE WHERE PROFISSIONAL_ID = ?', (PROFISSIONAL,))
    yield
    executar('DELETE FROM DISPONIBILIDADE WHERE PROFISSIONAL_ID = ?', (PROFISSIONAL,))
    for linha in original:
        executar('INSERT INTO DISPONIBILIDADE (PROFISSIONAL_ID, DIA_SEMANA, HORA_INICIO, HORA_FIM) VALUES (?, ?, ?, ?)',
                 (PROFISSIONAL, *linha))


@pytest.fixture
def bloqueios():
    """Apaga os bloqueios de agenda criados pelo teste."""
    ultimo = executar('SELECT COALESCE(MAX(BLOQUEIO_ID), 0) FROM BLOQUEIO_AGENDA')[0][0]
    yield
    executar('DELETE FROM BLOQUEIO_AGENDA WHERE BLOQUEIO_ID > ?', (ultimo,))


@pytest.fixture
def assinatura():
    """Troca a assinatura do 22 no teste (`dias` ate o vencimento) e restaura no fim; as cobrancas criadas saem."""
    colunas = 'PLANO, VALIDA_ATE, RENOVAR, PLANO_PROXIMO'
    original = executar(f'SELECT {colunas} FROM ASSINATURA WHERE PROFISSIONAL_ID = ?', (PROFISSIONAL,))[0]
    ultima = executar('SELECT COALESCE(MAX(COBRANCA_ID), 0) FROM COBRANCA_ASSINATURA')[0][0]

    def definir(plano, dias=10, renovar=True, proximo=None):
        executar(f"UPDATE ASSINATURA SET {colunas.replace(',', ' = ?,')} = ? WHERE PROFISSIONAL_ID = ?",
                 (plano, date.today() + timedelta(days=dias), renovar, proximo, PROFISSIONAL))

    yield definir
    executar('DELETE FROM COBRANCA_ASSINATURA WHERE COBRANCA_ID > ?', (ultima,))
    executar(f"UPDATE ASSINATURA SET {colunas.replace(',', ' = ?,')} = ? WHERE PROFISSIONAL_ID = ?", (*original, PROFISSIONAL))


@pytest.fixture
def conta_do_paciente():
    """Senha do paciente conhecida (Senha@123) no teste; senha, e-mail e codigo originais voltam no fim."""
    colunas = 'SENHA, EMAIL, CODIGO, CODIGO_EXPIRA_EM, CODIGO_TENTATIVAS, SENHA_ALTERADA_EM'
    original = executar(f'SELECT {colunas} FROM USUARIO WHERE ID_USUARIO = ?', (PACIENTE,))[0]
    executar('UPDATE USUARIO SET SENHA = ? WHERE ID_USUARIO = ?', (criar_hash_senha('Senha@123'), PACIENTE))
    yield original[1]
    executar(f"UPDATE USUARIO SET {colunas.replace(',', ' = ?,')} = ? WHERE ID_USUARIO = ?", (*original, PACIENTE))
