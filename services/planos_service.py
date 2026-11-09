"""Os planos de assinatura do profissional e a taxa que a plataforma cobra em cada consulta.

Módulo separado de assinaturas_service porque pagamentos_service precisa da taxa, e assinaturas_service já usa
pagamentos_service.cobrar: se a taxa morasse lá, um importaria o outro (import circular).
"""
from collections import namedtuple
from decimal import Decimal

# namedtuple é uma tupla com nome nos campos: PLANOS['PREMIUM'].taxa em vez de PLANOS['PREMIUM'][3]
Plano = namedtuple('Plano', 'nome nivel preco_centavos taxa')

# Plano -> (nome, nível, preço em centavos, taxa da plataforma em %), como na página de assinaturas do front.
# Sem plano válido o profissional some da busca; Profissional libera vídeo e lista de espera; Premium, artigos e
# o destaque na busca. Preços pensados para subir de plano compensar: a taxa menor paga a diferença a partir de
# R$ 1.500/mês em consultas (Profissional) e R$ 2.000/mês (Premium). Com 20 consultas de R$ 150, o Premium sai
# R$ 50 mais barato que o Essencial.
PLANOS = {
    'ESSENCIAL': Plano('Essencial', 1, 4999, Decimal('10')),
    'PROFISSIONAL': Plano('Profissional', 2, 7999, Decimal('8')),
    'PREMIUM': Plano('Premium', 3, 11999, Decimal('6')),
}
TAXA_SEM_PLANO = Decimal('10')
# Pagamentos de antes de a taxa ser gravada: é histórico, então não acompanha uma mudança futura nos valores acima
TAXA_PAGAMENTO_ANTIGO = Decimal('10')


def plano_ativo(cur, profissional_id):
    """O plano com validade até hoje ou depois, ou None se o profissional não tem plano válido."""
    cur.execute('SELECT PLANO FROM ASSINATURA WHERE PROFISSIONAL_ID = ? AND VALIDA_ATE >= CURRENT_DATE',
                (profissional_id,))
    linha = cur.fetchone()
    return linha[0] if linha else None


def taxa_atual(cur, profissional_id):
    """Percentual que a plataforma cobra hoje do profissional, pelo plano ativo."""
    plano = plano_ativo(cur, profissional_id)
    return PLANOS[plano].taxa if plano else TAXA_SEM_PLANO
