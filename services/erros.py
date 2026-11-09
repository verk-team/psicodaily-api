"""O erro que os serviços levantam quando uma regra de negócio é violada.

Exemplo: `raise ErroNegocio('Sessao nao encontrada', 404)`. O app.py transforma em resposta
`{"error": "Sessao nao encontrada"}` com status 404. Argumentos extras viram chaves a mais no JSON:
`ErroNegocio('...', 409, cancelada=True)` responde `{"error": "...", "cancelada": true}`.
"""


class ErroNegocio(Exception):
    """Regra violada; o app.py devolve {'error': mensagem, **extra} com o status."""

    def __init__(self, mensagem, status=400, **extra):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = status
        self.extra = extra
