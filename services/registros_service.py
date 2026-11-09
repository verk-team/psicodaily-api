"""Diário do paciente (tabela REGISTRO): humor, sono, alimentação, exercício, interação e anotação.

O profissional vê os registros pelo prontuário; o resumo do período (evolucao) é usado nas duas telas.
"""
from datetime import date, timedelta

from database.db import transacao
from services.erros import ErroNegocio

# Humor em número, para calcular médias e montar o gráfico
ESCALA_HUMOR = {'PESSIMO': 1, 'RUIM': 2, 'NEUTRO': 3, 'BOM': 4, 'EXCELENTE': 5}
PERIODOS_DIAS = (7, 30, 90)

# Valores aceitos em cada campo de opção
DOMINIOS = {
    'humor': {'PESSIMO', 'RUIM', 'NEUTRO', 'BOM', 'EXCELENTE'},
    'qualidade_sono': {'RUIM', 'REGULAR', 'BOA', 'EXCELENTE'},
    'alimentacao': {'POUCA', 'DESREGULADA', 'ADEQUADA', 'EXCESSIVA'},
}
COLUNAS = ('registro_id', 'usuario_id', 'humor', 'qualidade_sono',
           'minutos_sono', 'alimentacao', 'exercicio_fisico', 'interacao',
           'anotacao', 'criado_em')


def serializar(linha):
    """Linha do REGISTRO -> dicionário. A anotação é BLOB e pode chegar como arquivo: lê o texto."""
    registro = dict(zip(COLUNAS, linha))
    if hasattr(registro['anotacao'], 'read'):
        registro['anotacao'] = registro['anotacao'].read()
    if registro['criado_em'] is not None:
        registro['criado_em'] = registro['criado_em'].isoformat()
    return registro


def validar(dados):
    """Devolve a mensagem de erro, ou None se o registro está válido."""
    if not isinstance(dados, dict):
        return 'Envie um objeto JSON'
    permitidos = set(COLUNAS) - {'registro_id', 'usuario_id', 'criado_em'}
    if set(dados) - permitidos:
        return 'Campos desconhecidos; usuario e data sao definidos pelo servidor'
    for campo, valores in DOMINIOS.items():
        valor = dados.get(campo)
        if campo == 'alimentacao' and valor is None:
            continue
        if not isinstance(valor, str) or valor not in valores:
            return f'Valor invalido para {campo}'
    for campo, limite in [('minutos_sono', 1440), ('interacao', 32767)]:
        valor = dados.get(campo)
        if valor is not None and (type(valor) is not int or not 0 <= valor <= limite):
            return f'{campo} deve ser inteiro entre 0 e {limite}'
    if type(dados.get('exercicio_fisico', False)) is not bool:
        return 'exercicio_fisico deve ser booleano'
    if not isinstance(dados.get('anotacao', ''), str) or len(dados.get('anotacao', '')) > 10000:
        return 'anotacao deve ter no maximo 10000 caracteres'
    return None


def listar(usuario_id, limite, antes):
    """Registros com id menor que `antes`, dos mais novos para os mais antigos."""
    with transacao() as cur:
        # Busca um a mais que o limite só para saber se existe uma próxima página
        cur.execute(f"""SELECT FIRST ? {', '.join(COLUNAS)} FROM REGISTRO
            WHERE USUARIO_ID = ? AND REGISTRO_ID < ?
            ORDER BY REGISTRO_ID DESC""", (limite + 1, usuario_id, antes))
        linhas = cur.fetchall()
        itens = [serializar(linha) for linha in linhas[:limite]]
    return {'registros': itens, 'proximo_cursor': itens[-1]['registro_id'] if len(linhas) > limite else None}


def criar(usuario_id, dados):
    erro = validar(dados)
    if erro:
        raise ErroNegocio(erro)
    with transacao() as cur:
        cur.execute(f"""INSERT INTO REGISTRO
            (USUARIO_ID, HUMOR, QUALIDADE_SONO, MINUTOS_SONO, ALIMENTACAO,
             EXERCICIO_FISICO, INTERACAO, ANOTACAO)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING {', '.join(COLUNAS)}""",
            (usuario_id, dados['humor'], dados['qualidade_sono'], dados.get('minutos_sono'),
             dados.get('alimentacao'), dados.get('exercicio_fisico', False),
             dados.get('interacao'), dados.get('anotacao', '')))
        return serializar(cur.fetchone())


def inicio_periodo(dias):
    """Primeiro dia de uma janela de `dias` que termina hoje."""
    return date.today() - timedelta(days=dias - 1)


def evolucao(cur, usuario_id, dias):
    """Resumo e media diaria de humor (escala 1-5) dos ultimos `dias`, dentro da transacao do chamador."""
    cur.execute('''SELECT CRIADO_EM, HUMOR, MINUTOS_SONO, EXERCICIO_FISICO
        FROM REGISTRO WHERE USUARIO_ID = ? AND CRIADO_EM BETWEEN ? AND ?
        ORDER BY CRIADO_EM, REGISTRO_ID''', (usuario_id, inicio_periodo(dias), date.today()))
    linhas = cur.fetchall()

    # Notas de humor agrupadas por dia: {'2026-11-09': [4, 5], ...}
    por_dia = {}
    for criado_em, humor, _, _ in linhas:
        if humor in ESCALA_HUMOR:
            por_dia.setdefault(criado_em.isoformat(), []).append(ESCALA_HUMOR[humor])
    valores = [ESCALA_HUMOR[humor] for _, humor, _, _ in linhas if humor in ESCALA_HUMOR]
    sono = [minutos for _, _, minutos, _ in linhas if minutos is not None]

    return {
        'resumo': {
            'totalRegistros': len(linhas),
            'diasRegistrados': len({criado_em for criado_em, _, _, _ in linhas}),
            'humorMedio': sum(valores) / len(valores) if valores else None,
            'minutosSonoMedio': sum(sono) / len(sono) if sono else None,
            # Somar True conta quantos são True (True vale 1)
            'registrosComExercicio': sum(exercicio is True or exercicio == 1 for _, _, _, exercicio in linhas),
        },
        'evolucaoHumor': [{'data': dia, 'valor': sum(notas) / len(notas), 'quantidade': len(notas)}
                          for dia, notas in por_dia.items()],
    }


def evolucao_paciente(usuario_id, dias):
    """O resumo do período para o próprio paciente."""
    if dias not in PERIODOS_DIAS:
        raise ErroNegocio('dias deve ser 7, 30 ou 90')
    with transacao() as cur:
        return {'dias': dias, **evolucao(cur, usuario_id, dias)}
