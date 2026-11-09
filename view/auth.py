"""Rotas de login e senha (/api/auth).

O login devolve um token JWT: um texto assinado com a SECRET_KEY que diz quem é o usuário e até quando vale.
No site ele vai num cookie; no app mobile, no corpo da resposta.
"""
from flask import Blueprint, jsonify, make_response, request

from services import auth_service
from util.usuarios import usuario_autenticado

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')


def _corpo():
    return request.get_json(silent=True) or {}


def _com_cookie(response, token):
    """Põe o token no cookie access_token da resposta."""
    response.set_cookie(
        'access_token',
        token,
        path='/',
        max_age=auth_service.DURACAO_TOKEN,
        # httponly: o JavaScript da página não consegue ler o cookie, então um script malicioso não rouba o token
        httponly=True,
        # secure: o cookie só viaja por HTTPS
        secure=True,
        # None: o front (Vercel) e a API (Northflank) estão em domínios diferentes, e o cookie precisa ir mesmo assim
        samesite="None",
    )
    return response


@auth_bp.route('/login', methods=['POST'])
def login():
    """POST /api/auth/login com {email, senha}: login do site, com o token no cookie."""
    corpo = _corpo()
    usuario, token = auth_service.autenticar(corpo.get('email'), corpo.get('senha'))
    return _com_cookie(make_response({"message": "Usuario logado com sucesso", "usuario": usuario}), token)


@auth_bp.route('/login_mobile', methods=['POST'])
def login_mobile():
    """POST /api/auth/login_mobile com {email, senha}: login do app, com o token no corpo (sem cookie)."""
    corpo = _corpo()
    usuario, token = auth_service.autenticar(corpo.get('email'), corpo.get('senha'))
    usuario.pop('foto_url')
    return make_response({"message": "Usuario logado com sucesso", "usuario": {**usuario, "token": token}})


@auth_bp.route('/verificar_codigo', methods=['POST'])
def verificar_codigo():
    """POST /api/auth/verificar_codigo com {email, codigo}: ativa a conta com o código enviado por e-mail."""
    corpo = _corpo()
    if not corpo:
        return jsonify({"error": "Formato invalido"}), 400
    auth_service.verificar_codigo(corpo.get('email'), corpo.get('codigo'))
    return jsonify({"message": "Email verificado com sucesso"}), 200


@auth_bp.route('/esqueci_senha', methods=['POST'])
def esqueci_senha():
    """POST /api/auth/esqueci_senha com {email}: manda o código de recuperação por e-mail."""
    auth_service.esqueci_senha(_corpo().get('email'))
    return jsonify({"message": "Codigo enviado para o e-mail"}), 200


@auth_bp.route('/alterar_senha', methods=['POST'])
def alterar_senha():
    """POST /api/auth/alterar_senha com {email, codigo, senha}: troca a senha com o código de recuperação."""
    corpo = _corpo()
    auth_service.alterar_senha(corpo.get('email'), corpo.get('codigo'), corpo.get('senha'))
    return jsonify({"message": "Senha alterada com sucesso"}), 200


@auth_bp.route('/alterar_senha_logado', methods=['POST'])
@usuario_autenticado
def alterar_senha_logado(dados_usuario):
    """POST /api/auth/alterar_senha_logado com {senha_atual, nova_senha}.

    Trocar a senha invalida os tokens antigos, então a resposta já traz um cookie novo para o usuário continuar logado.
    """
    corpo = _corpo()
    token = auth_service.alterar_senha_logado(dados_usuario.get('id_usuario'), dados_usuario.get('usuario_role'),
                                              corpo.get('senha_atual'), corpo.get('nova_senha'))
    return _com_cookie(jsonify({"message": "Senha alterada com sucesso"}), token)


@auth_bp.route('/logout', methods=['POST'])
def logout():
    """POST /api/auth/logout: apaga o cookie do token."""
    response = make_response(jsonify({'message': 'Logout realizado com sucesso'}), 200)
    response.delete_cookie('access_token')
    return response
