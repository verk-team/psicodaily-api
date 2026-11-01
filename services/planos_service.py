from decimal import Decimal

# Modulo separado de assinaturas_service porque pagamentos_service precisa da taxa e assinaturas_service ja usa
# pagamentos_service.cobrar: morando la, o import seria circular.

# 9: plano -> (nome, nivel, preco em centavos, taxa da plataforma em %), como na pagina de assinaturas do front. Sem
# plano valido o profissional some da busca; Profissional libera video e lista de espera; Premium, conteudos e o destaque.
PLANOS = {
    'ESSENCIAL': ('Essencial', 1, 5000, Decimal('10')),
    'PROFISSIONAL': ('Profissional', 2, 10000, Decimal('8')),
    'PREMIUM': ('Premium', 3, 15000, Decimal('6')),
}
TAXA_SEM_PLANO = Decimal('10')
# Pagamentos anteriores a taxa gravada: e historico, entao nao acompanha uma mudanca futura nos valores acima
TAXA_PAGAMENTO_ANTIGO = Decimal('10')


def plano_ativo(cur, profissional_id):
    cur.execute('SELECT PLANO FROM ASSINATURA WHERE PROFISSIONAL_ID = ? AND VALIDA_ATE >= CURRENT_DATE',
                (profissional_id,))
    linha = cur.fetchone()
    return linha[0] if linha else None


def taxa_atual(cur, profissional_id):
    """Percentual que a plataforma cobra hoje do profissional, pelo plano ativo."""
    plano = plano_ativo(cur, profissional_id)
    return PLANOS[plano][3] if plano else TAXA_SEM_PLANO
