"""Tela inicial do app (GET /api/dashboard/): junta numa chamada o que o paciente ou o profissional vê primeiro."""
from datetime import date, datetime

from database.db import transacao
from services import consultas_service, favoritos_service
from services.erros import ErroNegocio

PROXIMAS = 5


def montar(usuario_id, papel):
    """Tela inicial do app numa chamada so: o que ja existe nas outras rotas, recortado por papel."""
    if papel not in ('PACIENTE', 'PSICOLOGO', 'PSIQUIATRA'):
        raise ErroNegocio('Painel exclusivo de pacientes e profissionais', 403)

    # ponytail: filtra a lista completa de sessões em Python; SELECT próprio se o histórico crescer muito
    # Datas em texto ISO ('2026-11-09T14:00:00') dão para comparar como texto: a ordem é a mesma do tempo
    agora = datetime.now().isoformat()
    futuras = [s for s in consultas_service.listar(usuario_id, papel)
               if s['status'] in ('AGENDADO', 'CONFIRMADO') and s['data_hora_inicio'] >= agora]
    # listar() vem da mais nova para a mais antiga: [::-1] inverte, e as 5 primeiras são as mais próximas
    painel = {'papel': papel, 'proximas_sessoes': futuras[::-1][:PROXIMAS]}

    with transacao() as cur:
        cur.execute('SELECT COUNT(*) FROM NOTIFICACAO WHERE USUARIO_ID = ? AND LIDA = FALSE', (usuario_id,))
        painel['nao_lidas'] = cur.fetchone()[0]

        if papel == 'PACIENTE':
            cur.execute('''SELECT FIRST 5 p.ID_COBRANCA_ARKHE, p.METODO, p.SESSAO_ID, p.VALOR_CENTAVOS, p.STATUS,
                    p.CRIADO_EM, p.PAGO_EM
                FROM PAGAMENTO p JOIN SESSAO s ON s.SESSAO_ID = p.SESSAO_ID
                WHERE s.PACIENTE_ID = ? ORDER BY p.PAGAMENTO_ID DESC''', (usuario_id,))
            painel['pagamentos'] = [
                {'id_cobranca': cobranca, 'metodo': metodo, 'sessao_id': sessao_id, 'valor_centavos': valor,
                 'status': status, 'criado_em': criado.isoformat(), 'pago_em': pago.isoformat() if pago else None}
                for cobranca, metodo, sessao_id, valor, status, criado, pago in cur.fetchall()]
        else:
            # Profissional: ganhos e atendimentos do mês corrente e total de pacientes
            inicio_mes = datetime.combine(date.today().replace(day=1), datetime.min.time())
            cur.execute('''SELECT SUM(p.VALOR_CENTAVOS) FROM PAGAMENTO p JOIN SESSAO s ON s.SESSAO_ID = p.SESSAO_ID
                WHERE s.PROFISSIONAL_ID = ? AND p.STATUS = 'PAGO' AND p.PAGO_EM >= ?
                  AND NOT EXISTS (SELECT 1 FROM REEMBOLSO r WHERE r.PAGAMENTO_ID = p.PAGAMENTO_ID)''',
                        (usuario_id, inicio_mes))
            painel['ganhos_mes_centavos'] = cur.fetchone()[0] or 0
            cur.execute('''SELECT COUNT(*) FROM SESSAO WHERE PROFISSIONAL_ID = ? AND STATUS = 'REALIZADO'
                AND DATA_HORA_INICIO >= ?''', (usuario_id, inicio_mes))
            painel['atendimentos_mes'] = cur.fetchone()[0]
            cur.execute('''SELECT COUNT(DISTINCT PACIENTE_ID) FROM SESSAO
                WHERE PROFISSIONAL_ID = ? AND STATUS <> 'CANCELADO' ''', (usuario_id,))
            painel['pacientes'] = cur.fetchone()[0]

    if papel == 'PACIENTE':
        painel['favoritos'] = favoritos_service.listar(usuario_id, papel)
    return painel
