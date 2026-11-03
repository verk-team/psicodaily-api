# Assistente de IA: validacao, ferramentas no banco real e o loop com o modelo simulado (o CI nao tem chave do Gemini).
import io
import json
from datetime import date, timedelta

import pytest
from urllib.error import HTTPError

from apoio import PACIENTE, PROFISSIONAL, checar, executar
from services import assistente_service
from services.erros import ErroNegocio


@pytest.fixture
def janela_amanha(agenda_vazia):
    amanha = date.today() + timedelta(days=1)
    executar("INSERT INTO DISPONIBILIDADE (PROFISSIONAL_ID, DIA_SEMANA, HORA_INICIO, HORA_FIM) VALUES (?, ?, '09:00', '10:00')",
             (PROFISSIONAL, (amanha.weekday() + 1) % 7))
    return amanha.isoformat()


def test_rota_valida_corpo_e_papel(anonimo, paciente, profissional):
    corpo = {'mensagens': [{'role': 'user', 'content': 'oi'}]}
    checar(anonimo.post('/api/assistente/', json=corpo), 401, 'error')
    checar(profissional.post('/api/assistente/', json=corpo), 403, 'error')
    for invalido in ({}, {'mensagens': []}, {'mensagens': corpo['mensagens'], 'usuario_id': 1},
                     {'mensagens': [{'role': 'system', 'content': 'x'}]},
                     {'mensagens': [{'role': 'user', 'content': 'oi'}, {'role': 'assistant', 'content': 'ola'}]},
                     {'mensagens': [{'role': 'user', 'content': 'x' * 2001}]}):
        checar(paciente.post('/api/assistente/', json=invalido), 400, 'error')


def test_ferramentas(janela_amanha):
    encontrados = assistente_service.executar_ferramenta('buscar_profissionais', {'especialidade': 'Psicologia'}, PACIENTE)
    assert encontrados and set(encontrados[0]) == set(assistente_service.CAMPOS_PROFISSIONAL)

    livres = assistente_service.executar_ferramenta('horarios_livres', {'id_profissional': PROFISSIONAL, 'de': janela_amanha,
                                                                        'ate': janela_amanha}, PACIENTE)
    assert livres['horarios'] == [{'inicio': f'{janela_amanha}T09:00', 'fim': f'{janela_amanha}T10:00'}]

    proposta = assistente_service.executar_ferramenta(
        'propor_agendamento', {'id_profissional': PROFISSIONAL, 'inicio': f'{janela_amanha}T09:00', 'fim': f'{janela_amanha}T10:00'}, PACIENTE)
    assert proposta == {'id_profissional': PROFISSIONAL, 'timestamp_inicio': f'{janela_amanha}T09:00',
                        'timestamp_fim': f'{janela_amanha}T10:00'}
    # O erro ja traz os livres do dia, para o modelo corrigir sem outra rodada
    with pytest.raises(ErroNegocio, match=f'{janela_amanha}T09:00'):
        assistente_service.executar_ferramenta(
            'propor_agendamento', {'id_profissional': PROFISSIONAL, 'inicio': f'{janela_amanha}T09:30', 'fim': f'{janela_amanha}T10:30'}, PACIENTE)


def _chamada(id_, nome, argumentos):
    return {'id': id_, 'type': 'function', 'function': {'name': nome, 'arguments': argumentos}}


def test_loop_executa_ferramentas_e_devolve_proposta(monkeypatch, paciente, janela_amanha):
    pedido = json.dumps({'id_profissional': PROFISSIONAL, 'inicio': f'{janela_amanha}T09:00', 'fim': f'{janela_amanha}T10:00'})
    respostas = [
        {'content': None, 'tool_calls': [_chamada('t1', 'horarios_livres', '{')]},
        {'content': None, 'tool_calls': [_chamada('t2', 'propor_agendamento', pedido)]},
    ]
    enviadas = []

    def chamar(mensagens):
        enviadas.append(list(mensagens))
        return respostas.pop(0)

    monkeypatch.setattr(assistente_service, '_chamar', chamar)

    corpo = checar(paciente.post('/api/assistente/', json={'mensagens': [{'role': 'user', 'content': 'quero marcar amanha 9h'}]}),
                   200, 'resposta', 'agendamento')
    # Proposta pronta encerra o loop com texto fixo: duas chamadas ao modelo, nao tres
    assert len(enviadas) == 2
    assert corpo['resposta'] == f'O horário de {janela_amanha[8:10]}/{janela_amanha[5:7]} às 09:00 está livre.'
    assert corpo['agendamento']['timestamp_inicio'] == f'{janela_amanha}T09:00'
    assert enviadas[0][0]['role'] == 'system'
    # JSON quebrado do modelo vira erro para ele, nao 500
    invalida = enviadas[1][-1]
    assert invalida['tool_call_id'] == 't1' and invalida['content'].startswith('Erro')


@pytest.fixture
def gemini(monkeypatch):
    """Simula o Gemini: cada item da lista e um status de erro ou a mensagem devolvida; sem esperar entre tentativas."""
    respostas = []

    def urlopen(req, timeout):
        resposta = respostas.pop(0)
        if isinstance(resposta, int):
            raise HTTPError(req.full_url, resposta, 'erro', {}, io.BytesIO(b'{"error": {}}'))
        return io.BytesIO(json.dumps({'choices': [{'message': resposta}]}).encode())

    monkeypatch.setattr(assistente_service, 'urlopen', urlopen)
    monkeypatch.setattr(assistente_service, 'sleep', lambda segundos: None)
    return respostas


def test_chamar_tenta_de_novo_so_em_erro_passageiro(gemini):
    gemini.extend([503, 429, {'content': 'oi'}])
    assert assistente_service._chamar([]) == {'content': 'oi'} and not gemini

    gemini.extend([503, 503, 429])
    with pytest.raises(ErroNegocio, match='muitas solicitações'):
        assistente_service._chamar([])
    assert not gemini

    gemini.extend([400, {'content': 'nao chega aqui'}])
    with pytest.raises(ErroNegocio, match='no momento'):
        assistente_service._chamar([])
    assert len(gemini) == 1
