from flask import Blueprint, jsonify, make_response, request
from services import auth_service
from util.usuarios import usuario_autenticado

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')


def _corpo():
    return request.get_json(silent=True) or {}


def _com_cookie(response, token):
    response.set_cookie(
        'access_token',
        token,
        path='/',
        max_age=auth_service.DURACAO_TOKEN,
        httponly=True,
        secure=True,
        samesite="None",
    )
    return response


@auth_bp.route('/login', methods=['POST'])
def login():
    data = _corpo()
    usuario, token = auth_service.autenticar(data.get('email'), data.get('senha'))
    return _com_cookie(make_response({"message": "Usuario logado com sucesso", "usuario": usuario}), token)


@auth_bp.route('/login_mobile', methods=['POST'])
def login_mobile():
    data = _corpo()
    usuario, token = auth_service.autenticar(data.get('email'), data.get('senha'))
    usuario.pop('foto_url')
    return make_response({"message": "Usuario logado com sucesso", "usuario": {**usuario, "token": token}})


@auth_bp.route('/verificar_codigo', methods=['POST'])
def verificar_codigo():
    data = _corpo()
    if not data:
        return jsonify({"error": "Formato invalido"}), 400
    auth_service.verificar_codigo(data.get('email'), data.get('codigo'))
    return jsonify({"message": "Email verificado com sucesso"}), 200


@auth_bp.route('/esqueci_senha', methods=['POST'])
def esqueci_senha():
    auth_service.esqueci_senha(_corpo().get('email'))
    return jsonify({"message": "Codigo enviado para o e-mail"}), 200


@auth_bp.route('/alterar_senha', methods=['POST'])
def alterar_senha():
    data = _corpo()
    auth_service.alterar_senha(data.get('email'), data.get('codigo'), data.get('senha'))
    return jsonify({"message": "Senha alterada com sucesso"}), 200


@auth_bp.route('/alterar_senha_logado', methods=['POST'])
@usuario_autenticado
def alterar_senha_logado(dados_usuario):
    data = _corpo()
    token = auth_service.alterar_senha_logado(dados_usuario.get('id_usuario'), dados_usuario.get('usuario_role'),
                                              data.get('senha_atual'), data.get('nova_senha'))
    return _com_cookie(jsonify({"message": "Senha alterada com sucesso"}), token)


@auth_bp.route('/logout', methods=['POST'])
def logout():
    response = make_response(jsonify({'message': 'Logout realizado com sucesso'}), 200)
    response.delete_cookie('access_token')
    return response
