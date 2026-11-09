"""Rotas da conta do usuário (/api/usuarios): cadastro de paciente, perfil e preferências."""
from flask import Blueprint, jsonify, make_response, request

from services import preferencias_service, usuarios_service
from util.rotas import sem_cache
from util.usuarios import admin_autenticado, usuario_autenticado

usuarios_bp = Blueprint("usuarios", __name__, url_prefix="/api/usuarios")


@usuarios_bp.route("/me", methods=["GET"])
@usuario_autenticado
def usuario_atual(dados_usuario):
    """GET /api/usuarios/me: o perfil de quem está logado."""
    return sem_cache(jsonify({"usuario": usuarios_service.perfil(dados_usuario.get("id_usuario"))}))


@usuarios_bp.route("/me", methods=["PUT", "PATCH"])
@usuario_autenticado
def atualizar_usuario(dados_usuario):
    """PUT ou PATCH /api/usuarios/me: edita o perfil. Aceita multipart (com foto) ou JSON."""
    formulario = request.form if request.form else (request.get_json(silent=True) or {})
    usuarios_service.atualizar_perfil(dados_usuario.get("id_usuario"), formulario,
                                      request.files.get("imagem"), request.method == "PUT")
    return sem_cache(jsonify({"message": "Perfil atualizado"}))


@usuarios_bp.route("/me/preferencias", methods=["GET"])
@usuario_autenticado
def preferencias(dados_usuario):
    """GET /api/usuarios/me/preferencias: as respostas do questionário inicial do paciente."""
    respostas = preferencias_service.obter(dados_usuario.get("id_usuario"), dados_usuario.get("usuario_role"))
    return sem_cache(jsonify(respostas))


@usuarios_bp.route("/me/preferencias", methods=["PUT"])
@usuario_autenticado
def salvar_preferencias(dados_usuario):
    """PUT /api/usuarios/me/preferencias: salva o questionário, que melhora as recomendações de profissionais."""
    respostas = preferencias_service.salvar(dados_usuario.get("id_usuario"), dados_usuario.get("usuario_role"),
                                            request.get_json(silent=True))
    return sem_cache(jsonify(respostas))


@usuarios_bp.route("/", methods=["GET"])
@admin_autenticado
def listar_usuarios(dados_usuario):
    """GET /api/usuarios/: todos os usuários (só para o admin)."""
    return sem_cache(jsonify({"usuarios": usuarios_service.listar()}))


@usuarios_bp.route("/logout", methods=["POST"])
def logout():
    """POST /api/usuarios/logout: apaga o cookie do token (igual a /api/auth/logout)."""
    resposta = make_response(jsonify({"message": "Logout realizado com sucesso"}))
    resposta.delete_cookie("access_token")
    return resposta


@usuarios_bp.route('/', methods=['POST'])
def cadastro():
    """POST /api/usuarios/ (multipart): cadastro de paciente, com foto opcional."""
    usuario = usuarios_service.cadastrar_paciente(request.form, request.files.get('imagem'))
    return jsonify({
        "message": "Usuario cadastrado com sucesso",
        "usuario": {
            "id_usuario": usuario["id_usuario"],
            "tipo_usuario": "PACIENTE"
        }
    }), 201
