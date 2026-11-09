"""Rotas dos profissionais (/api/profissionais): busca, perfil público, cadastro e a agenda.

Rotas com /me são do profissional logado; as com /<id> são públicas e mostram o perfil de um profissional.
"""
from flask import Blueprint, jsonify, request

from services import disponibilidade_service, profissionais_service
from util.rotas import sem_cache
from util.usuarios import profissional_autenticado, usuario_autenticado

profissionais_bp = Blueprint('profissionais', __name__, url_prefix='/api/profissionais')


@profissionais_bp.route('/', methods=['GET'])
def profissionais():
    """GET /api/profissionais/: busca com filtros e paginação pela query string (ver profissionais_service)."""
    return jsonify({'message': 'Profissionais obtidos com sucesso', **profissionais_service.buscar(request.args)}), 200


@profissionais_bp.route('/recomendados', methods=['GET'])
@usuario_autenticado
def recomendados(dados_usuario):
    """GET /api/profissionais/recomendados: os que mais combinam com o questionário do paciente."""
    recomendacao = profissionais_service.recomendados(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'))
    return sem_cache(jsonify(recomendacao))


@profissionais_bp.route('/<int:id>', methods=['GET'])
def profissional(id):
    """GET /api/profissionais/<id>: o perfil público de um profissional."""
    perfil = profissionais_service.detalhe(id)
    return jsonify({'message': 'Profissionais obtidos com sucesso', 'profissionais': perfil}), 200


@profissionais_bp.route('/<int:id>/avaliacoes', methods=['GET'])
def avaliacoes(id):
    """GET /api/profissionais/<id>/avaliacoes: os comentários públicos, sem o nome de quem avaliou."""
    return jsonify({'avaliacoes': profissionais_service.avaliacoes_publicas(id)}), 200


@profissionais_bp.route('/<int:id>/disponibilidade', methods=['GET'])
def disponibilidade(id):
    """GET /api/profissionais/<id>/disponibilidade: as janelas da agenda semanal (ex.: segunda das 8h às 12h)."""
    return jsonify({'janelas': disponibilidade_service.listar(id)}), 200


@profissionais_bp.route('/me/disponibilidade', methods=['PUT'])
@profissional_autenticado
def substituir_disponibilidade(dados_usuario):
    """PUT /api/profissionais/me/disponibilidade com {"janelas": [...]}: troca a agenda semanal inteira."""
    janelas = disponibilidade_service.substituir(dados_usuario['id_usuario'], request.get_json(silent=True))
    return jsonify({'janelas': janelas}), 200


@profissionais_bp.route('/me/bloqueios', methods=['GET'])
@profissional_autenticado
def bloqueios(dados_usuario):
    """GET /api/profissionais/me/bloqueios: os dias em que o profissional não atende (férias, folga)."""
    return sem_cache(jsonify({'bloqueios': disponibilidade_service.listar_bloqueios(dados_usuario['id_usuario'])}))


@profissionais_bp.route('/me/bloqueios', methods=['POST'])
@profissional_autenticado
def criar_bloqueio(dados_usuario):
    """POST /api/profissionais/me/bloqueios com {inicio, fim}: bloqueia esses dias da agenda."""
    bloqueio = disponibilidade_service.criar_bloqueio(dados_usuario['id_usuario'], request.get_json(silent=True))
    return jsonify(bloqueio), 201


@profissionais_bp.route('/me/bloqueios/<int:bloqueio_id>', methods=['DELETE'])
@profissional_autenticado
def apagar_bloqueio(dados_usuario, bloqueio_id):
    """DELETE /api/profissionais/me/bloqueios/<id>: libera os dias de novo."""
    disponibilidade_service.apagar_bloqueio(dados_usuario['id_usuario'], bloqueio_id)
    return jsonify({'message': 'Bloqueio removido'}), 200


@profissionais_bp.route('/me/desempenho', methods=['GET'])
@profissional_autenticado
def desempenho(dados_usuario):
    """GET /api/profissionais/me/desempenho?dias=30: visitas ao perfil, consultas e favoritos no período."""
    dias = request.args.get('dias', '30')
    return jsonify(profissionais_service.desempenho(dados_usuario['id_usuario'], dias)), 200


@profissionais_bp.route('/<int:id>/horarios', methods=['GET'])
def horarios(id):
    """GET /api/profissionais/<id>/horarios?de=AAAA-MM-DD&ate=AAAA-MM-DD&duracao=: horários livres para agendar."""
    argumentos = request.args
    livres = disponibilidade_service.horarios_livres(
        id, argumentos.get('de'), argumentos.get('ate'), argumentos.get('duracao'))
    return jsonify(livres), 200


@profissionais_bp.route('/', methods=['POST'])
def cadastro_profissional():
    """POST /api/profissionais/ (multipart): cadastro de psicólogo ou psiquiatra, com foto opcional."""
    cadastro = profissionais_service.cadastrar(request.form, request.files.get('imagem'))
    return jsonify({"message": "Profissional cadastrado com sucesso", **cadastro}), 201


@profissionais_bp.route('/me', methods=['PATCH'])
@profissional_autenticado
def atualizar_profissional(dados_usuario):
    """PATCH /api/profissionais/me: o profissional edita o próprio perfil (só os campos enviados mudam)."""
    profissionais_service.atualizar(dados_usuario.get('id_usuario'), request.get_json(silent=True) or {})
    return jsonify({'message': 'Perfil profissional atualizado'}), 200
