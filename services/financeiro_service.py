from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from database.db import transacao
from funcao import formatar_reais
from services import notificacoes_service, pagamentos_service, planos_service
from services.auditoria_service import auditar
from services.erros import ErroNegocio

STATUS_SAIDA = ('PENDENTE', 'PAGO')
PERIODOS_DIAS = ('7', '30', '90', '365')

# Pagamento que ja pode ir para um repasse: pago, consulta realizada, sem reembolso e fora de outro repasse
ELEGIVEL_REPASSE = '''p.STATUS = 'PAGO' AND p.REPASSE_ID IS NULL
    AND NOT EXISTS (SELECT 1 FROM REEMBOLSO r WHERE r.PAGAMENTO_ID = p.PAGAMENTO_ID)
    AND EXISTS (SELECT 1 FROM SESSAO se WHERE se.SESSAO_ID = p.SESSAO_ID AND se.STATUS = 'REALIZADO'
                AND se.PROFISSIONAL_ID = {profissional})'''

# Saidas de dinheiro que o admin paga: o nome da tabela vem daqui, nunca da requisicao.
# `destino` devolve (quem recebe, CPF dele, valor, status).
SAIDAS = {
    'REEMBOLSO': {
        'destino': '''SELECT s.PACIENTE_ID, u.CPF, r.VALOR_CENTAVOS, r.STATUS FROM REEMBOLSO r
            JOIN PAGAMENTO p ON p.PAGAMENTO_ID = r.PAGAMENTO_ID
            JOIN SESSAO s ON s.SESSAO_ID = p.SESSAO_ID
            JOIN USUARIO u ON u.ID_USUARIO = s.PACIENTE_ID
            WHERE r.REEMBOLSO_ID = ?''',
        'titulo': 'Reembolso enviado',
        'mensagem': 'Devolvemos {valor} da consulta cancelada.',
    },
    'REPASSE': {
        'destino': '''SELECT r.PROFISSIONAL_ID, u.CPF, r.VALOR_CENTAVOS, r.STATUS FROM REPASSE r
            JOIN USUARIO u ON u.ID_USUARIO = r.PROFISSIONAL_ID
            WHERE r.REPASSE_ID = ?''',
        'titulo': 'Repasse enviado',
        'mensagem': 'Enviamos {valor} referente às suas consultas realizadas.',
    },
}


def liquido(valor, taxa_arkhe, percentual):
    """(taxa da plataforma, liquido do profissional) de um pagamento, em centavos.

    `percentual` e o gravado no pagamento; a taxa do cartao da Arkhé tambem sai do profissional."""
    if percentual is None:
        percentual = planos_service.TAXA_PAGAMENTO_ANTIGO
    taxa_plataforma = int((Decimal(valor) * percentual / 100).quantize(Decimal('1'), ROUND_HALF_UP))
    return taxa_plataforma, valor - taxa_plataforma - (taxa_arkhe or 0)


def _situacao(status_sessao, reembolsado, status_repasse):
    if reembolsado:
        return 'REEMBOLSADO'
    if status_repasse:
        return 'REPASSADO' if status_repasse == 'PAGO' else 'EM_REPASSE'
    return 'A_RECEBER' if status_sessao == 'REALIZADO' else 'AGUARDANDO_CONSULTA'


def ganhos(profissional_id, papel, dias):
    if papel not in ('PSICOLOGO', 'PSIQUIATRA'):
        raise ErroNegocio('Somente profissionais podem ver os ganhos', 403)
    if dias not in PERIODOS_DIAS:
        raise ErroNegocio('dias deve ser 7, 30, 90 ou 365')
    inicio = datetime.combine(date.today() - timedelta(days=int(dias) - 1), datetime.min.time())

    with transacao() as cur:
        # Todos os pagos (nao so os do periodo): o saldo a receber nao depende do filtro
        cur.execute('''SELECT p.SESSAO_ID, s.DATA_HORA_INICIO, s.STATUS, u.NOME, p.METODO, p.VALOR_CENTAVOS,
                p.TAXA_ARKHE_CENTAVOS, p.TAXA_PLATAFORMA_PERCENTUAL, p.PAGO_EM, r.REEMBOLSO_ID, rp.STATUS
            FROM PAGAMENTO p
            JOIN SESSAO s ON s.SESSAO_ID = p.SESSAO_ID
            JOIN USUARIO u ON u.ID_USUARIO = s.PACIENTE_ID
            LEFT JOIN REEMBOLSO r ON r.PAGAMENTO_ID = p.PAGAMENTO_ID
            LEFT JOIN REPASSE rp ON rp.REPASSE_ID = p.REPASSE_ID
            WHERE s.PROFISSIONAL_ID = ? AND p.STATUS = 'PAGO'
            ORDER BY p.PAGO_EM DESC''', (profissional_id,))
        linhas = cur.fetchall()
        cur.execute('''SELECT COUNT(*) FROM SESSAO WHERE PROFISSIONAL_ID = ? AND STATUS = 'REALIZADO'
            AND DATA_HORA_INICIO >= ?''', (profissional_id, inicio))
        realizadas = cur.fetchone()[0]
        cur.execute('''SELECT REPASSE_ID, VALOR_CENTAVOS, STATUS, CRIADO_EM, PAGO_EM FROM REPASSE
            WHERE PROFISSIONAL_ID = ? ORDER BY REPASSE_ID DESC''', (profissional_id,))
        repasses = [{'repasse_id': repasse_id, 'valor_centavos': valor, 'status': status,
                     'criado_em': criado.isoformat(), 'pago_em': pago.isoformat() if pago else None}
                    for repasse_id, valor, status, criado, pago in cur.fetchall()]
        taxa_atual = planos_service.taxa_atual(cur, profissional_id)

    consultas, do_periodo = [], []
    for (sessao_id, data, status_sessao, paciente, metodo, valor, taxa_arkhe, percentual, pago_em, reembolso,
         repasse) in linhas:
        taxa_plataforma, valor_liquido = liquido(valor, taxa_arkhe, percentual)
        consulta = {
            'sessao_id': sessao_id, 'data_sessao': data.isoformat(), 'paciente_nome': paciente, 'metodo': metodo,
            'valor_centavos': valor, 'taxa_plataforma_centavos': taxa_plataforma,
            'taxa_cartao_centavos': taxa_arkhe or 0, 'liquido_centavos': valor_liquido,
            'pago_em': pago_em.isoformat(), 'situacao': _situacao(status_sessao, reembolso, repasse),
        }
        consultas.append(consulta)
        if pago_em >= inicio:
            do_periodo.append(consulta)
    validas = [c for c in do_periodo if c['situacao'] != 'REEMBOLSADO']

    def soma(lista, campo):
        return sum(c[campo] for c in lista)

    def saldo(*situacoes):
        return sum(c['liquido_centavos'] for c in consultas if c['situacao'] in situacoes)

    return {
        'dias': int(dias),
        'data_inicio': inicio.date().isoformat(),
        'taxa_plataforma_percentual': float(taxa_atual),
        'resumo': {
            'consultas_realizadas': realizadas,
            'recebido_centavos': soma(validas, 'valor_centavos'),
            'taxa_plataforma_centavos': soma(validas, 'taxa_plataforma_centavos'),
            'taxa_cartao_centavos': soma(validas, 'taxa_cartao_centavos'),
            'liquido_centavos': soma(validas, 'liquido_centavos'),
        },
        'saldo': {
            'aguardando_consulta_centavos': saldo('AGUARDANDO_CONSULTA'),
            'a_receber_centavos': saldo('A_RECEBER'),
            'em_repasse_centavos': saldo('EM_REPASSE'),
        },
        'consultas': do_periodo,
        'repasses': repasses,
    }


def fechar_repasses(admin_id):
    """Cria um repasse PENDENTE por profissional com o liquido de tudo que ja pode ser repassado."""
    with transacao() as cur:
        cur.execute(f'''SELECT DISTINCT s.PROFISSIONAL_ID FROM PAGAMENTO p JOIN SESSAO s ON s.SESSAO_ID = p.SESSAO_ID
            WHERE {ELEGIVEL_REPASSE.format(profissional='s.PROFISSIONAL_ID')}''')
        profissionais = [linha[0] for linha in cur.fetchall()]
        criados = []
        for profissional_id in profissionais:
            cur.execute('INSERT INTO REPASSE (PROFISSIONAL_ID, VALOR_CENTAVOS) VALUES (?, 0) RETURNING REPASSE_ID',
                        (profissional_id,))
            repasse_id = cur.fetchone()[0]
            cur.execute(f'UPDATE PAGAMENTO p SET REPASSE_ID = ? WHERE {ELEGIVEL_REPASSE.format(profissional="?")}',
                        (repasse_id, profissional_id))
            # O valor sai do que foi de fato vinculado: um fechamento ao mesmo tempo nao conta duas vezes
            cur.execute('''SELECT VALOR_CENTAVOS, TAXA_ARKHE_CENTAVOS, TAXA_PLATAFORMA_PERCENTUAL FROM PAGAMENTO
                WHERE REPASSE_ID = ?''', (repasse_id,))
            pagamentos = cur.fetchall()
            if not pagamentos:
                cur.execute('DELETE FROM REPASSE WHERE REPASSE_ID = ?', (repasse_id,))
                continue
            valor = sum(liquido(*pagamento)[1] for pagamento in pagamentos)
            cur.execute('UPDATE REPASSE SET VALOR_CENTAVOS = ? WHERE REPASSE_ID = ?', (valor, repasse_id))
            auditar(cur, admin_id, 'REPASSE_CRIADO', 'REPASSE', repasse_id)
            criados.append({'repasse_id': repasse_id, 'profissional_id': profissional_id, 'valor_centavos': valor,
                            'consultas': len(pagamentos), 'status': 'PENDENTE'})
    if not criados:
        raise ErroNegocio('Nao ha valores a repassar', 409)
    return criados


def listar_repasses(status=None):
    status = _ler_status(status)
    with transacao() as cur:
        cur.execute(f'''SELECT FIRST 200 r.REPASSE_ID, r.STATUS, r.VALOR_CENTAVOS, r.CRIADO_EM, r.PAGO_EM,
                r.ID_MOVIMENTACAO_ARKHE, u.ID_USUARIO, u.NOME, u.CPF,
                (SELECT COUNT(*) FROM PAGAMENTO p WHERE p.REPASSE_ID = r.REPASSE_ID)
            FROM REPASSE r JOIN USUARIO u ON u.ID_USUARIO = r.PROFISSIONAL_ID
            {'WHERE r.STATUS = ?' if status else ''} ORDER BY r.REPASSE_ID DESC''', (status,) if status else ())
        linhas = cur.fetchall()
    return [{'repasse_id': repasse_id, 'status': situacao, 'valor_centavos': valor, 'criado_em': criado.isoformat(),
             'pago_em': pago.isoformat() if pago else None, 'id_movimentacao_arkhe': movimentacao,
             'consultas': consultas, 'profissional': {'id_usuario': profissional_id, 'nome': nome, 'cpf': cpf}}
            for repasse_id, situacao, valor, criado, pago, movimentacao, profissional_id, nome, cpf, consultas in linhas]


def pagar_repasse(admin_id, repasse_id, dados):
    return _pagar_saida(admin_id, 'REPASSE', repasse_id, dados)


def _ler_status(status):
    if status is not None and status not in STATUS_SAIDA:
        raise ErroNegocio('status deve ser PENDENTE ou PAGO')
    return status


def _ler_manual(dados):
    dados = {} if dados is None else dados
    if not isinstance(dados, dict) or set(dados) - {'manual'} or not isinstance(dados.get('manual', False), bool):
        raise ErroNegocio('Envie {} para pagar pela Arkhé ou {"manual": true} se ja pagou por fora')
    return dados.get('manual', False)


def _pagar_saida(admin_id, tabela, registro_id, dados):
    """Paga um reembolso ou repasse: Pix pela Arkhé para a chave CPF de quem recebe ou, com manual, so marca."""
    manual = _ler_manual(dados)
    saida = SAIDAS[tabela]
    with transacao() as cur:
        cur.execute(saida['destino'], (registro_id,))
        linha = cur.fetchone()
    if linha is None:
        raise ErroNegocio(f'{tabela.capitalize()} nao encontrado', 404)
    destinatario, cpf, valor, status = linha
    if status == 'PAGO':
        raise ErroNegocio(f'{tabela.capitalize()} ja foi pago', 409)

    movimentacao = None if manual else pagamentos_service.pix_saida(cpf, valor, f'psicodaily-{tabela.lower()}-{registro_id}')
    with transacao() as cur:
        cur.execute(f'''UPDATE {tabela} SET STATUS = 'PAGO', PAGO_EM = CURRENT_TIMESTAMP, ID_MOVIMENTACAO_ARKHE = ?
            WHERE {tabela}_ID = ? AND STATUS = 'PENDENTE' RETURNING {tabela}_ID''', (movimentacao, registro_id))
        if cur.fetchone()[0] is None:
            raise ErroNegocio(f'{tabela.capitalize()} ja foi pago', 409)
        auditar(cur, admin_id, f'{tabela}_PAGO', tabela, registro_id)
        email = notificacoes_service.notificar(
            cur, destinatario, f'{tabela}_PAGO', saida['titulo'], saida['mensagem'].format(valor=formatar_reais(valor)),
            {f'{tabela.lower()}_id': registro_id})
    notificacoes_service.enviar_emails([email])
    return {f'{tabela.lower()}_id': registro_id, 'status': 'PAGO', 'valor_centavos': valor,
            'manual': manual, 'id_movimentacao_arkhe': movimentacao}


def listar_reembolsos(status=None):
    status = _ler_status(status)
    with transacao() as cur:
        cur.execute(f'''SELECT FIRST 200 r.REEMBOLSO_ID, r.STATUS, r.VALOR_CENTAVOS, r.CRIADO_EM, r.PAGO_EM,
                r.ID_MOVIMENTACAO_ARKHE, p.METODO, s.SESSAO_ID, s.DATA_HORA_INICIO, u.ID_USUARIO, u.NOME, u.CPF
            FROM REEMBOLSO r
            JOIN PAGAMENTO p ON p.PAGAMENTO_ID = r.PAGAMENTO_ID
            JOIN SESSAO s ON s.SESSAO_ID = p.SESSAO_ID
            JOIN USUARIO u ON u.ID_USUARIO = s.PACIENTE_ID
            {'WHERE r.STATUS = ?' if status else ''} ORDER BY r.REEMBOLSO_ID DESC''', (status,) if status else ())
        linhas = cur.fetchall()
    # O CPF vai para o admin pagar por fora quando o Pix da Arkhé falha
    return [{'reembolso_id': reembolso_id, 'status': situacao, 'valor_centavos': valor, 'criado_em': criado.isoformat(),
             'pago_em': pago.isoformat() if pago else None, 'id_movimentacao_arkhe': movimentacao, 'metodo': metodo,
             'sessao_id': sessao_id, 'data_sessao': inicio.isoformat(),
             'paciente': {'id_usuario': paciente_id, 'nome': nome, 'cpf': cpf}}
            for reembolso_id, situacao, valor, criado, pago, movimentacao, metodo, sessao_id, inicio, paciente_id, nome, cpf
            in linhas]


def pagar_reembolso(admin_id, reembolso_id, dados):
    return _pagar_saida(admin_id, 'REEMBOLSO', reembolso_id, dados)
