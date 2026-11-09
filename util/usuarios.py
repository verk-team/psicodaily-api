"""Autenticação das rotas: lê o token da requisição e confere o papel do usuário.

Decorator é uma função que "embrulha" outra: `@usuario_autenticado` em cima de uma rota faz a checagem do token
rodar antes dela. Se a checagem passa, a rota recebe os dados do token (`id_usuario`, `usuario_role`) como
primeiro argumento; se não passa, a rota nem roda e a resposta já é o erro (401 ou 403).
"""
from functools import wraps

from flask import jsonify, request

from config import ORIGENS_FRONT
from services.auth_service import usuario_do_token


def origem_permitida():
    """True se o WebSocket foi aberto pelo nosso front (ou por um cliente fora do navegador)."""
    # O navegador manda o cookie de login na abertura do WebSocket, e essa abertura não passa pelo CORS.
    # Sem esta checagem, qualquer site aberto em outra aba conseguiria entrar no WS com a sessão do usuário.
    # Sem Origin = cliente fora do navegador (app mobile), que precisa mandar o próprio token.
    origem = request.headers.get("Origin")
    return origem is None or origem in ORIGENS_FRONT


def token_da_requisicao():
    """O token de login: no cabeçalho Authorization (app mobile) ou no cookie access_token (site)."""
    cabecalho = request.headers.get("Authorization", "")
    if cabecalho.startswith("Bearer "):
        return cabecalho[7:]
    return request.cookies.get("access_token")


def usuario_autenticado(funcao):
    """Libera a rota para qualquer usuário logado."""
    @wraps(funcao)
    def wrapper(*args, **kwargs):
        token = token_da_requisicao()

        if not token:
            return jsonify({ "error": "Usuario nao autenticado" }), 401

        # Token vencido, inválido ou de antes da última troca de senha: usuario_do_token levanta ErroNegocio 401
        return funcao(usuario_do_token(token), *args, **kwargs)

    return wrapper


def profissional_autenticado(funcao):
    """Libera a rota só para psicólogos e psiquiatras logados."""
    @usuario_autenticado
    @wraps(funcao)
    def wrapper(dados, *args, **kwargs):
        if dados.get('usuario_role') not in ['PSICOLOGO', 'PSIQUIATRA']:
            return jsonify({ "error": "Acesso negado" }), 403
        return funcao(dados, *args, **kwargs)

    return wrapper


def admin_autenticado(funcao):
    """Libera a rota só para administradores logados."""
    @usuario_autenticado
    @wraps(funcao)
    def wrapper(dados, *args, **kwargs):
        if dados.get('usuario_role') != 'ADMIN':
            return jsonify({ "error": "Acesso negado" }), 403
        return funcao(dados, *args, **kwargs)

    return wrapper
