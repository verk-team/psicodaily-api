"""Painel do administrador: métricas da plataforma e consulta à auditoria."""
from datetime import date, datetime, timedelta

from database.db import transacao
from services.assinaturas_service import PLANOS
from services.auditoria_service import ACOES
from services.erros import ErroNegocio

PERIODOS_DIAS = ('7', '30', '90', '365')
PAPEIS = ('PACIENTE', 'PSICOLOGO', 'PSIQUIATRA', 'ADMIN')
STATUS_SESSAO = ('AGENDADO', 'CONFIRMADO', 'REALIZADO', 'CANCELADO')


def _contagem(chaves, linhas):
    """{chave: total} com todas as chaves; as que não apareceram no GROUP BY ficam com 0."""
    return {**dict.fromkeys(chaves, 0), **{chave: total for chave, total in linhas}}


def metricas(dias):
    """Números dos últimos `dias`: consultas, faturamento, usuários, avaliações e assinaturas."""
    if dias not in PERIODOS_DIAS:
        raise ErroNegocio('dias deve ser 7, 30, 90 ou 365')
    inicio = date.today() - timedelta(days=int(dias) - 1)
    # De meia-noite do primeiro dia até meia-noite de amanhã (o fim não entra)
    de = datetime.combine(inicio, datetime.min.time())
    ate = datetime.combine(date.today() + timedelta(days=1), datetime.min.time())

    with transacao() as cur:
        # Sessões pela data em que acontecem; sessões futuras ficam fora do período
        cur.execute('''SELECT STATUS, COUNT(*) FROM SESSAO
            WHERE DATA_HORA_INICIO >= ? AND DATA_HORA_INICIO < ? GROUP BY STATUS''', (de, ate))
        sessoes = cur.fetchall()
        # Faturamento das consultas: o que foi pago no período e não foi devolvido
        cur.execute('''SELECT SUM(p.VALOR_CENTAVOS) FROM PAGAMENTO p WHERE p.STATUS = 'PAGO' AND p.PAGO_EM >= ? AND p.PAGO_EM < ?
            AND NOT EXISTS (SELECT 1 FROM REEMBOLSO r WHERE r.PAGAMENTO_ID = p.PAGAMENTO_ID)''', (de, ate))
        faturamento = cur.fetchone()[0]
        cur.execute('SELECT USUARIO_ROLE, COUNT(*) FROM USUARIO WHERE ATIVO = TRUE GROUP BY USUARIO_ROLE')
        ativos = cur.fetchall()
        cur.execute('SELECT USUARIO_ROLE, COUNT(*) FROM USUARIO WHERE CRIADO_EM >= ? GROUP BY USUARIO_ROLE', (de,))
        novos = cur.fetchall()
        cur.execute('SELECT AVG(CAST(NOTA AS NUMERIC(5,2))), COUNT(*) FROM AVALIACAO WHERE CRIADO_EM >= ?', (de,))
        media, avaliacoes = cur.fetchone()
        cur.execute('SELECT PLANO, COUNT(*) FROM ASSINATURA WHERE VALIDA_ATE >= CURRENT_DATE GROUP BY PLANO')
        planos = cur.fetchall()
        cur.execute('''SELECT SUM(VALOR_CENTAVOS) FROM COBRANCA_ASSINATURA
            WHERE STATUS = 'PAGO' AND PAGO_EM >= ? AND PAGO_EM < ?''', (de, ate))
        receita_assinaturas = cur.fetchone()[0]

    return {
        'dias': int(dias),
        'data_inicio': inicio.isoformat(),
        'faturamento_centavos': faturamento or 0,
        'consultas_por_status': _contagem(STATUS_SESSAO, sessoes),
        'usuarios_ativos': _contagem(PAPEIS, ativos),
        'novos_cadastros': _contagem(PAPEIS, novos),
        'avaliacao': {'media': round(float(media), 1) if media is not None else None, 'total': avaliacoes},
        # A receita das assinaturas é da plataforma; faturamento_centavos continua sendo só o das consultas
        'assinaturas': {'ativas_por_plano': _contagem(PLANOS, planos), 'receita_centavos': receita_assinaturas or 0},
    }


def auditoria(limite, antes, usuario_id=None, acao=None):
    """Registros de auditoria com id menor que `antes`, dos mais novos para os mais antigos."""
    if acao is not None and acao not in ACOES:
        raise ErroNegocio(f'acao deve ser uma de: {", ".join(sorted(ACOES))}')
    filtros, parametros = ['AUDITORIA_ID < ?'], [antes]
    if usuario_id is not None:
        filtros.append('USUARIO_ID = ?')
        parametros.append(usuario_id)
    if acao is not None:
        filtros.append('ACAO = ?')
        parametros.append(acao)

    with transacao() as cur:
        # Busca uma a mais que o limite só para saber se existe uma próxima página
        cur.execute(f'''SELECT FIRST ? AUDITORIA_ID, USUARIO_ID, ACAO, ENTIDADE, ENTIDADE_ID, IP, CRIADO_EM
            FROM AUDITORIA WHERE {' AND '.join(filtros)} ORDER BY AUDITORIA_ID DESC''', (limite + 1, *parametros))
        linhas = cur.fetchall()
    itens = [{'auditoria_id': auditoria_id, 'usuario_id': usuario, 'acao': acao_feita, 'entidade': entidade,
              'entidade_id': entidade_id, 'ip': ip, 'criado_em': criado.isoformat()}
             for auditoria_id, usuario, acao_feita, entidade, entidade_id, ip, criado in linhas[:limite]]
    # Veio uma linha a mais que o limite: existe próxima página, que começa depois do último item
    return {'auditoria': itens, 'proximo_cursor': itens[-1]['auditoria_id'] if len(linhas) > limite else None}
