"""Ajudantes usados pelas rotas de view/."""

# Maior número que cabe numa coluna INTEGER do Firebird. Nas listas paginadas, o parâmetro `antes` traz os
# registros com id menor que ele; sem `antes`, este valor faz a lista começar pelo registro mais novo.
MAIOR_ID = 2147483647


def sem_cache(resposta):
    """Proíbe o navegador e qualquer proxy no caminho de guardar uma cópia da resposta.

    Usado nas respostas com dados pessoais (consultas, prontuário, mensagens...): `private` diz que só o
    navegador do próprio usuário pode guardar, e `no-store` diz que nem ele pode.
    """
    resposta.headers['Cache-Control'] = 'private, no-store'
    return resposta
