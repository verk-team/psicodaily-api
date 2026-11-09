"""Questionário inicial do paciente (motivos, abordagem, modalidade, idioma e preço máximo).

As respostas ficam num JSON em PREFERENCIA_PACIENTE e alimentam as recomendações de profissionais.
"""
import json

from database.db import transacao
from funcao import (ABORDAGENS, AREAS_ATUACAO, IDIOMAS, MODALIDADES, normalizar_abordagem, normalizar_lista,
                    normalizar_modalidade)
from services.erros import ErroNegocio

MAXIMO_MOTIVOS = 3
# Opções do questionário, devolvidas no GET para o front montar a tela
OPCOES = {
    'motivos': AREAS_ATUACAO,
    'abordagem': ABORDAGENS,
    'modalidade': sorted(MODALIDADES),
    'idioma': IDIOMAS,
}


def _exigir_paciente(papel):
    if papel != 'PACIENTE':
        raise ErroNegocio('O questionario e exclusivo do paciente', 403)


def _validar(dados):
    """Confere as respostas e devolve só as que vieram preenchidas, já no formato oficial."""
    if not isinstance(dados, dict) or not dados or set(dados) - {*OPCOES, 'preco_max'}:
        raise ErroNegocio('Envie apenas: ' + ', '.join([*OPCOES, 'preco_max']))
    respostas = {}
    if dados.get('motivos') is not None:
        motivos = normalizar_lista(dados['motivos'], AREAS_ATUACAO, MAXIMO_MOTIVOS)
        if motivos is None:
            raise ErroNegocio(f'Escolha de 1 a {MAXIMO_MOTIVOS} motivos entre: ' + ', '.join(AREAS_ATUACAO))
        respostas['motivos'] = motivos.split(',')
    # O idioma é um só: passa por normalizar_lista como uma lista de um item
    for campo, normalizar in (('abordagem', normalizar_abordagem), ('modalidade', normalizar_modalidade),
                              ('idioma', lambda valor: normalizar_lista([valor], IDIOMAS, 1)
                               if isinstance(valor, str) else None)):
        if dados.get(campo) is not None:
            valor = normalizar(dados[campo])
            if valor is None:
                raise ErroNegocio(f'{campo} deve ser um de: ' + ', '.join(OPCOES[campo]))
            respostas[campo] = valor
    if dados.get('preco_max') is not None:
        # Centavos, como o preco_max da busca
        if type(dados['preco_max']) is not int or not 1 <= dados['preco_max'] <= 10_000_000:
            raise ErroNegocio('preco_max deve ser um inteiro em centavos entre 1 e 10000000')
        respostas['preco_max'] = dados['preco_max']
    return respostas


def obter(paciente_id, papel):
    """As respostas salvas (None se o paciente nunca respondeu) e as opções de cada pergunta."""
    _exigir_paciente(papel)
    with transacao() as cur:
        cur.execute('SELECT RESPOSTAS, ATUALIZADO_EM FROM PREFERENCIA_PACIENTE WHERE PACIENTE_ID = ?', (paciente_id,))
        linha = cur.fetchone()
    respostas, atualizado_em = linha if linha else (None, None)
    return {
        'preferencias': json.loads(respostas) if linha else None,
        'atualizado_em': atualizado_em.isoformat() if linha else None,
        'opcoes': OPCOES,
    }


def salvar(paciente_id, papel, dados):
    """Substitui todas as respostas; pergunta sem resposta (ausente ou null) = sem preferencia."""
    _exigir_paciente(papel)
    respostas = json.dumps(_validar(dados), ensure_ascii=False)
    with transacao() as cur:
        # UPDATE OR INSERT ... MATCHING: atualiza a linha do paciente se ela existe; se não, cria
        cur.execute('''UPDATE OR INSERT INTO PREFERENCIA_PACIENTE (PACIENTE_ID, RESPOSTAS, ATUALIZADO_EM)
            VALUES (?, ?, CURRENT_TIMESTAMP) MATCHING (PACIENTE_ID)''', (paciente_id, respostas))
    return obter(paciente_id, papel)
