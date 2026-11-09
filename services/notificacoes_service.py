"""Notificações: o aviso gravado no banco (o sininho do site), que também sai por e-mail e WhatsApp.

Como usar, dentro da transação de quem está avisando:

    emails = [notificacoes_service.notificar(cur, usuario_id, 'TIPO', 'Título', 'Mensagem')]
    ... fim do `with transacao()` (commit) ...
    notificacoes_service.enviar_emails(emails)

O envio fica para depois do commit: se a transação for desfeita, o aviso some do banco e o e-mail não sai.
"""
import json

from database.db import transacao
from services import email_service, whatsapp_service
from services.erros import ErroNegocio

TIPOS = {'VAGA_LISTA_ESPERA', 'PAGAMENTO_CONFIRMADO', 'PAGAMENTO_RECEBIDO', 'SESSAO_AGENDADA', 'SESSAO_ALTERADA',
         'SESSAO_CANCELADA', 'LEMBRETE_CONSULTA', 'REEMBOLSO_PAGO', 'REPASSE_PAGO',
         'ASSINATURA_RENOVACAO', 'ASSINATURA_ATIVA'}


def notificar(cur, usuario_id, tipo, titulo, mensagem, dados=None):
    """Grava na transacao do chamador e devolve (email, titulo, corpo, telefone); quem chamou envia com
    enviar_emails depois do commit."""
    # assert pega erro de programação: um tipo escrito errado quebra o teste em vez de gravar lixo no banco
    assert tipo in TIPOS, tipo
    cur.execute('''INSERT INTO NOTIFICACAO (USUARIO_ID, TIPO, TITULO, MENSAGEM, DADOS)
        VALUES (?, ?, ?, ?, ?)''', (usuario_id, tipo, titulo, mensagem, json.dumps(dados) if dados else None))
    cur.execute('SELECT EMAIL, NOME, TELEFONE FROM USUARIO WHERE ID_USUARIO = ?', (usuario_id,))
    email, nome, telefone = cur.fetchone()
    return email, titulo, f'Olá, {nome}.\n\n{mensagem}', telefone


def enviar_emails(emails):
    """Envia cada aviso por e-mail e por WhatsApp, em segundo plano. Recebe a lista que notificar() devolveu."""
    for email, titulo, corpo, telefone in emails:
        email_service.enviar_email_async(email, titulo, corpo)
        # No WhatsApp, *texto* sai em negrito
        whatsapp_service.enviar_async(telefone, f'*{titulo}*\n\n{corpo}')


def listar(usuario_id, limite, antes):
    """Notificações com id menor que `antes`, das mais novas para as mais antigas, e o total de não lidas."""
    with transacao() as cur:
        # Busca uma a mais que o limite só para saber se existe uma próxima página
        cur.execute('''SELECT FIRST ? NOTIFICACAO_ID, TIPO, TITULO, MENSAGEM, DADOS, LIDA, CRIADO_EM
            FROM NOTIFICACAO WHERE USUARIO_ID = ? AND NOTIFICACAO_ID < ?
            ORDER BY NOTIFICACAO_ID DESC''', (limite + 1, usuario_id, antes))
        linhas = cur.fetchall()
        cur.execute('SELECT COUNT(*) FROM NOTIFICACAO WHERE USUARIO_ID = ? AND LIDA = FALSE', (usuario_id,))
        nao_lidas = cur.fetchone()[0]
    itens = [{'notificacao_id': notificacao_id, 'tipo': tipo, 'titulo': titulo, 'mensagem': mensagem,
              'dados': json.loads(dados) if dados else None, 'lida': lida, 'criado_em': criado.isoformat()}
             for notificacao_id, tipo, titulo, mensagem, dados, lida, criado in linhas[:limite]]
    tem_proxima_pagina = len(linhas) > limite
    return {'notificacoes': itens, 'nao_lidas': nao_lidas,
            'proximo_cursor': itens[-1]['notificacao_id'] if tem_proxima_pagina else None}


def marcar_lidas(usuario_id, dados):
    """Sem 'ids', marca todas. Devolve quantas mudaram."""
    if not isinstance(dados, dict) or set(dados) - {'ids'}:
        raise ErroNegocio('Envie apenas ids')
    ids = dados.get('ids')
    if ids is not None and (not isinstance(ids, list) or not ids or len(ids) > 100
                            or any(type(i) is not int for i in ids)):
        raise ErroNegocio('ids deve ser uma lista de 1 a 100 numeros inteiros')

    filtro, parametros = '', ()
    if ids:
        # Um '?' para cada id: IN (?, ?, ?)
        filtro, parametros = f" AND NOTIFICACAO_ID IN ({', '.join('?' * len(ids))})", tuple(ids)
    with transacao() as cur:
        # USUARIO_ID no WHERE: ninguém marca como lida a notificação de outra pessoa
        cur.execute(f'UPDATE NOTIFICACAO SET LIDA = TRUE WHERE USUARIO_ID = ? AND LIDA = FALSE{filtro}',
                    (usuario_id, *parametros))
        return cur.rowcount
