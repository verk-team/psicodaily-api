"""Assinatura do profissional: um plano pago a cada ciclo de 30 dias, cobrado pela Arkhé.

A Arkhé não tem cobrança recorrente, então cada ciclo é uma cobrança nova (tabela COBRANCA_ASSINATURA).
Perto do vencimento, a thread de tarefas automáticas gera o Pix da renovação e manda por e-mail e WhatsApp.
O plano em vigor fica na tabela ASSINATURA (uma linha por profissional).
"""
import logging
from datetime import date, datetime, timedelta

from database.db import transacao
from funcao import formatar_reais
from services import notificacoes_service, pagamentos_service
from services.auditoria_service import auditar
from services.erros import ErroNegocio
from services.planos_service import PLANOS, plano_ativo

logger = logging.getLogger(__name__)

# ponytail: um mês = 30 dias; mês de calendário se o dia do vencimento andar incomodar
CICLO = timedelta(days=30)
GRATIS = timedelta(days=30)
# O job gera o Pix da renovação a partir de 3 dias antes do vencimento
RENOVAR_ANTES = timedelta(days=3)
# Cobrança pendente mais velha que isso deixa de ser conferida na Arkhé
CONFERIR_POR = timedelta(days=35)


def libera(plano, minimo):
    """O plano (None = sem plano valido) da os beneficios de `minimo`?"""
    # Cada plano tem um nível (1, 2, 3): um plano libera tudo dos planos de nível igual ou menor
    return plano is not None and PLANOS[plano].nivel >= PLANOS[minimo].nivel


def exigir(cur, profissional_id, minimo):
    """403 se o plano ativo do profissional não chega no plano `minimo`."""
    if not libera(plano_ativo(cur, profissional_id), minimo):
        raise ErroNegocio(f'Recurso do plano {PLANOS[minimo].nome} ou superior', 403, plano_necessario=minimo)


def planos():
    return [{'plano': plano, 'nome': nome, 'nivel': nivel, 'preco_centavos': preco,
             'taxa_plataforma_percentual': float(taxa)}
            for plano, (nome, nivel, preco, taxa) in PLANOS.items()]


def iniciar_gratis(cur, profissional_id):
    """No cadastro: 30 dias de Essencial; 3 dias antes de acabar, o job manda o Pix para continuar."""
    cur.execute("INSERT INTO ASSINATURA (PROFISSIONAL_ID, PLANO, VALIDA_ATE) VALUES (?, 'ESSENCIAL', ?)",
                (profissional_id, date.today() + GRATIS))


def _estado(cur, profissional_id):
    """A assinatura como o front mostra: plano, validade, renovação e se ainda está no período grátis."""
    cur.execute('SELECT PLANO, VALIDA_ATE, RENOVAR, PLANO_PROXIMO FROM ASSINATURA WHERE PROFISSIONAL_ID = ?',
                (profissional_id,))
    linha = cur.fetchone()
    if linha is None:
        return {'plano': None, 'ativa': False, 'valida_ate': None, 'renovar': False, 'plano_proximo': None,
                'periodo_gratis': False}
    plano, valida_ate, renovar, proximo = linha
    # Período grátis = assinatura ativa sem nenhuma cobrança paga até hoje
    cur.execute("SELECT FIRST 1 1 FROM COBRANCA_ASSINATURA WHERE PROFISSIONAL_ID = ? AND STATUS = 'PAGO'",
                (profissional_id,))
    ativa = valida_ate >= date.today()
    nunca_pagou = cur.fetchone() is None
    return {'plano': plano, 'ativa': ativa, 'valida_ate': valida_ate.isoformat(), 'renovar': renovar,
            'plano_proximo': proximo, 'periodo_gratis': ativa and nunca_pagou}


def _ativar(cur, profissional_id, plano, centavos):
    """Pagamento confirmado: aplica o plano e devolve o aviso. Renovacao e downgrade continuam do fim do ciclo;
    upgrade e plano vencido comecam hoje, sem proporcional."""
    hoje = date.today()
    cur.execute('SELECT PLANO, VALIDA_ATE FROM ASSINATURA WHERE PROFISSIONAL_ID = ?', (profissional_id,))
    atual = cur.fetchone()
    plano_atual, valida_ate_atual = atual if atual is not None else (None, None)
    # Continua do fim do ciclo quando o plano atual ainda vale e o novo não é de nível maior
    continua = (atual is not None and valida_ate_atual >= hoje
                and PLANOS[plano].nivel <= PLANOS[plano_atual].nivel)
    valida_ate = (valida_ate_atual if continua else hoje) + CICLO
    # UPDATE OR INSERT ... MATCHING: atualiza a linha do profissional se ela existe; se não, cria
    cur.execute('''UPDATE OR INSERT INTO ASSINATURA (PROFISSIONAL_ID, PLANO, VALIDA_ATE, RENOVAR, PLANO_PROXIMO)
        VALUES (?, ?, ?, TRUE, NULL) MATCHING (PROFISSIONAL_ID)''', (profissional_id, plano, valida_ate))
    auditar(cur, profissional_id, 'ASSINATURA_PAGA', 'ASSINATURA', profissional_id)
    return [notificacoes_service.notificar(
        cur, profissional_id, 'ASSINATURA_ATIVA', 'Assinatura confirmada',
        f'Recebemos {formatar_reais(centavos)} pelo plano {PLANOS[plano].nome}, que vale até {valida_ate:%d/%m/%Y}.',
        {'plano': plano})]


def _confirmar(cobranca_id):
    """Marca a cobrança como paga e ativa o plano. Se outra requisição já confirmou, não faz nada."""
    with transacao() as cur:
        cur.execute('''UPDATE COBRANCA_ASSINATURA SET STATUS = 'PAGO', PAGO_EM = CURRENT_TIMESTAMP
            WHERE COBRANCA_ID = ? AND STATUS = 'PENDENTE' RETURNING PROFISSIONAL_ID, PLANO, VALOR_CENTAVOS''',
                    (cobranca_id,))
        # No Firebird, UPDATE ... RETURNING que não acha a linha devolve uma linha de NULLs
        linha = cur.fetchone()
        if linha is None or linha[0] is None:
            return  # outra requisição (ou o job) confirmou antes
        profissional_id, plano, centavos = linha
        emails = _ativar(cur, profissional_id, plano, centavos)
    notificacoes_service.enviar_emails(emails)


def _desde(dias):
    """Meia-noite de hoje menos `dias` (um timedelta)."""
    return datetime.combine(date.today(), datetime.min.time()) - dias


def _conferir_pendentes(profissional_id):
    """Confere na Arkhé as cobrancas pendentes do profissional: confirma as pagas e devolve as que seguem abertas."""
    with transacao() as cur:
        cur.execute('''SELECT COBRANCA_ID, PLANO, METODO, ID_COBRANCA_ARKHE FROM COBRANCA_ASSINATURA
            WHERE PROFISSIONAL_ID = ? AND STATUS = 'PENDENTE' AND CRIADO_EM >= ? ORDER BY COBRANCA_ID DESC''',
                    (profissional_id, _desde(CONFERIR_POR)))
        pendentes = cur.fetchall()
    abertas = []
    for cobranca_id, plano, metodo, id_arkhe in pendentes:
        cobranca = pagamentos_service.consultar_arkhe(metodo, id_arkhe)
        # Status da Arkhé: 0 = pendente, 1 = pago
        if cobranca['status'] == 1:
            _confirmar(cobranca_id)
        else:
            abertas.append({**cobranca, 'cobranca_id': cobranca_id, 'plano': plano})
    return abertas


def obter(profissional_id):
    """A assinatura do profissional e as cobranças que ainda esperam pagamento."""
    abertas = _conferir_pendentes(profissional_id)
    with transacao() as cur:
        return {**_estado(cur, profissional_id), 'cobrancas_pendentes': abertas}


def assinar(profissional_id, dados):
    """Assina, renova ou troca de plano: (estado, cobranca). No downgrade a cobranca e None: ele so vale na
    proxima renovacao."""
    if not isinstance(dados, dict) or not set(dados) <= {'plano', 'metodo', 'cartao'} or dados.get('plano') not in PLANOS:
        raise ErroNegocio('Envie plano (ESSENCIAL, PROFISSIONAL ou PREMIUM), metodo (PIX, BOLETO ou CARTAO) '
                          'e, se for CARTAO, cartao')
    plano = dados['plano']
    metodo, cartao = pagamentos_service.ler_metodo(dados)
    # Uma pendente paga sem ninguém conferir muda o plano atual: confere antes de decidir
    abertas = _conferir_pendentes(profissional_id)

    with transacao() as cur:
        atual = plano_ativo(cur, profissional_id)
        # Downgrade (plano novo de nível menor): só agenda a troca para a próxima renovação, sem cobrar agora
        if atual and not libera(plano, atual):
            cur.execute('UPDATE ASSINATURA SET PLANO_PROXIMO = ?, RENOVAR = TRUE WHERE PROFISSIONAL_ID = ?',
                        (plano, profissional_id))
            auditar(cur, profissional_id, 'ASSINATURA_ALTERADA', 'ASSINATURA', profissional_id)
            return _estado(cur, profissional_id), None
        # Reaproveita a pendente do mesmo plano e método: duas iguais abertas deixariam pagar duas vezes
        mesma = next((c for c in abertas if (c['plano'], c['metodo']) == (plano, metodo)), None)
        if mesma:
            return _estado(cur, profissional_id), mesma
        cur.execute('SELECT CPF FROM USUARIO WHERE ID_USUARIO = ?', (profissional_id,))
        cpf = cur.fetchone()[0]

    # O boleto exige conta da Arkhé no CPF do profissional, como o do paciente
    centavos = PLANOS[plano].preco_centavos
    id_arkhe, cobranca, _ = pagamentos_service.cobrar(metodo, centavos, cartao, cpf, date.today() + RENOVAR_ANTES)
    # O cartão é aprovado na hora; Pix e boleto ficam pendentes até o profissional pagar
    pago = metodo == 'CARTAO'
    with transacao() as cur:
        cur.execute('''INSERT INTO COBRANCA_ASSINATURA (PROFISSIONAL_ID, PLANO, METODO, ID_COBRANCA_ARKHE,
                VALOR_CENTAVOS, STATUS, PAGO_EM) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING COBRANCA_ID''',
                    (profissional_id, plano, metodo, id_arkhe, centavos, 'PAGO' if pago else 'PENDENTE',
                     datetime.now() if pago else None))
        cobranca_id = cur.fetchone()[0]
        auditar(cur, profissional_id, 'ASSINATURA_ALTERADA', 'COBRANCA_ASSINATURA', cobranca_id)
        emails = _ativar(cur, profissional_id, plano, centavos) if pago else []
        estado = _estado(cur, profissional_id)
    notificacoes_service.enviar_emails(emails)
    return estado, {**cobranca, 'cobranca_id': cobranca_id, 'plano': plano}


def cancelar(profissional_id):
    """Desliga a renovacao: o plano vale ate o fim do ciclo pago, sem reembolso."""
    with transacao() as cur:
        cur.execute('''UPDATE ASSINATURA SET RENOVAR = FALSE, PLANO_PROXIMO = NULL
            WHERE PROFISSIONAL_ID = ? AND VALIDA_ATE >= CURRENT_DATE RETURNING PROFISSIONAL_ID''', (profissional_id,))
        linha = cur.fetchone()
        if linha is None or linha[0] is None:
            raise ErroNegocio('Nenhuma assinatura ativa', 404)
        auditar(cur, profissional_id, 'ASSINATURA_ALTERADA', 'ASSINATURA', profissional_id)
        return _estado(cur, profissional_id)


def boleto_pdf(profissional_id, cobranca_id):
    """O PDF do boleto de uma cobrança de assinatura do próprio profissional."""
    with transacao() as cur:
        cur.execute('''SELECT ID_COBRANCA_ARKHE FROM COBRANCA_ASSINATURA
            WHERE COBRANCA_ID = ? AND PROFISSIONAL_ID = ? AND METODO = 'BOLETO' ''', (cobranca_id, profissional_id))
        linha = cur.fetchone()
    if linha is None:
        raise ErroNegocio('Cobranca nao encontrada', 404)
    id_cobranca_arkhe = linha[0]
    return pagamentos_service.pdf_boleto(id_cobranca_arkhe)


def processar():
    """Job da thread de tarefas: confirma as cobrancas pagas e gera o Pix das renovacoes.

    Devolve (confirmadas, renovacoes).
    ponytail: confere todas as pendentes na Arkhe a cada minuto; espacar se passarem de algumas dezenas
    """
    with transacao() as cur:
        cur.execute('''SELECT COBRANCA_ID, METODO, ID_COBRANCA_ARKHE FROM COBRANCA_ASSINATURA
            WHERE STATUS = 'PENDENTE' AND CRIADO_EM >= ?''', (_desde(CONFERIR_POR),))
        pendentes = cur.fetchall()
    confirmadas = 0
    for cobranca_id, metodo, id_arkhe in pendentes:
        try:
            if pagamentos_service.consultar_arkhe(metodo, id_arkhe)['status'] == 1:
                _confirmar(cobranca_id)
                confirmadas += 1
        except Exception:
            logger.exception('Nao conferiu a cobranca de assinatura %s', cobranca_id)

    hoje = date.today()
    with transacao() as cur:
        # Assinaturas que vencem nos próximos 3 dias e vão renovar. Quem já tem cobrança aberta dos últimos
        # dias (do job ou da tela) não ganha outra. COALESCE: se há troca de plano agendada, renova no novo
        cur.execute('''SELECT a.PROFISSIONAL_ID, COALESCE(a.PLANO_PROXIMO, a.PLANO), a.VALIDA_ATE FROM ASSINATURA a
            WHERE a.RENOVAR = TRUE AND a.VALIDA_ATE BETWEEN ? AND ?
              AND NOT EXISTS (SELECT 1 FROM COBRANCA_ASSINATURA c WHERE c.PROFISSIONAL_ID = a.PROFISSIONAL_ID
                  AND c.STATUS = 'PENDENTE' AND c.CRIADO_EM >= ?)''',
                    (hoje, hoje + RENOVAR_ANTES, _desde(RENOVAR_ANTES)))
        vencendo = cur.fetchall()
    renovacoes = 0
    for profissional_id, plano, valida_ate in vencendo:
        try:
            _gerar_renovacao(profissional_id, plano, valida_ate)
            renovacoes += 1
        except Exception:
            # ponytail: com a Arkhé fora, tenta de novo no minuto seguinte e só registra no log
            logger.exception('Nao gerou a renovacao da assinatura de %s', profissional_id)
    return confirmadas, renovacoes


def _gerar_renovacao(profissional_id, plano, valida_ate):
    """Sem cartao salvo nem recorrencia na Arkhé, a renovacao e um Pix enviado por e-mail e WhatsApp."""
    nome, _, centavos, _ = PLANOS[plano]
    id_arkhe, cobranca, _ = pagamentos_service.cobrar('PIX', centavos)
    with transacao() as cur:
        cur.execute('''INSERT INTO COBRANCA_ASSINATURA (PROFISSIONAL_ID, PLANO, METODO, ID_COBRANCA_ARKHE, VALOR_CENTAVOS)
            VALUES (?, ?, 'PIX', ?, ?) RETURNING COBRANCA_ID''', (profissional_id, plano, id_arkhe, centavos))
        cobranca_id = cur.fetchone()[0]
        emails = [notificacoes_service.notificar(
            cur, profissional_id, 'ASSINATURA_RENOVACAO', 'Renove sua assinatura',
            f'Sua assinatura vence em {valida_ate:%d/%m/%Y}. Para continuar no plano {nome} e visível no PsicoDaily, '
            f'pague {formatar_reais(centavos)} pelo Pix {cobranca.get("codigo_pagamento")} ou pela tela de assinatura.',
            {'cobranca_id': cobranca_id})]
    notificacoes_service.enviar_emails(emails)
