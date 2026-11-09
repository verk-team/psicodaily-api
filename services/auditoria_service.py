"""Auditoria: registro de quem fez cada ação sensível (login, acesso a prontuário, pagamento...) e de qual IP.

O admin lê esses registros em GET /api/admin/auditoria.
"""
from contextvars import ContextVar

from database.db import transacao

# IP de quem fez a requisição atual. O app.py preenche a cada requisição, e assim os serviços não precisam
# importar o Flask. ContextVar guarda um valor separado por requisição, mesmo com várias rodando ao mesmo tempo.
IP_ATUAL = ContextVar('ip_atual', default=None)

ACOES = {'LOGIN', 'LOGIN_FALHA', 'PRONTUARIO_ACESSO', 'SESSAO_AGENDADA', 'SESSAO_ALTERADA',
         'ENCAMINHAMENTO_CRIADO', 'SENHA_ALTERADA', 'EMAIL_ALTERADO', 'SESSAO_CANCELADA', 'REEMBOLSO_PAGO',
         'REPASSE_CRIADO', 'REPASSE_PAGO', 'ASSINATURA_ALTERADA', 'ASSINATURA_PAGA'}


def auditar(cur, usuario_id, acao, entidade=None, entidade_id=None):
    """Grava na mesma transacao da acao: se ela for desfeita, o registro tambem e."""
    assert acao in ACOES, acao
    # A coluna IP tem 45 caracteres (o tamanho máximo de um IPv6)
    ip = (IP_ATUAL.get() or '')[:45] or None
    cur.execute('''INSERT INTO AUDITORIA (USUARIO_ID, ACAO, ENTIDADE, ENTIDADE_ID, IP)
        VALUES (?, ?, ?, ?, ?)''', (usuario_id, acao, entidade, entidade_id, ip))


def auditar_avulso(usuario_id, acao, entidade=None, entidade_id=None):
    """Transacao propria, para o que precisa ficar mesmo quando a requisicao falha (ex.: login errado)."""
    with transacao() as cur:
        auditar(cur, usuario_id, acao, entidade, entidade_id)
