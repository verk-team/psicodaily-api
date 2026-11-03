from flask import Blueprint, jsonify, request
from services import disponibilidade_service, profissionais_service
from util.usuarios import profissional_autenticado, usuario_autenticado

prof_bp = Blueprint('profissionais', __name__, url_prefix='/api/profissionais')


@prof_bp.route('/', methods=['GET'])
def profissionais():
	return jsonify({'message': 'Profissionais obtidos com sucesso', **profissionais_service.buscar(request.args)}), 200


@prof_bp.route('/recomendados', methods=['GET'])
@usuario_autenticado
def recomendados(dados_usuario):
	resposta = jsonify(profissionais_service.recomendados(dados_usuario['id_usuario'], dados_usuario.get('usuario_role')))
	resposta.headers['Cache-Control'] = 'private, no-store'
	return resposta


@prof_bp.route('/<int:id>', methods=['GET'])
def profissional(id):
	return jsonify({'message': 'Profissionais obtidos com sucesso', 'profissionais': profissionais_service.detalhe(id)}), 200


@prof_bp.route('/<int:id>/avaliacoes', methods=['GET'])
def avaliacoes(id):
	return jsonify({'avaliacoes': profissionais_service.avaliacoes_publicas(id)}), 200


@prof_bp.route('/<int:id>/disponibilidade', methods=['GET'])
def disponibilidade(id):
	return jsonify({'janelas': disponibilidade_service.listar(id)}), 200


@prof_bp.route('/me/disponibilidade', methods=['PUT'])
@profissional_autenticado
def substituir_disponibilidade(dados_usuario):
	janelas = disponibilidade_service.substituir(dados_usuario['id_usuario'], request.get_json(silent=True))
	return jsonify({'janelas': janelas}), 200


@prof_bp.route('/me/bloqueios', methods=['GET'])
@profissional_autenticado
def bloqueios(dados_usuario):
	resposta = jsonify({'bloqueios': disponibilidade_service.listar_bloqueios(dados_usuario['id_usuario'])})
	resposta.headers['Cache-Control'] = 'private, no-store'
	return resposta


@prof_bp.route('/me/bloqueios', methods=['POST'])
@profissional_autenticado
def criar_bloqueio(dados_usuario):
	return jsonify(disponibilidade_service.criar_bloqueio(dados_usuario['id_usuario'], request.get_json(silent=True))), 201


@prof_bp.route('/me/bloqueios/<int:bloqueio_id>', methods=['DELETE'])
@profissional_autenticado
def apagar_bloqueio(dados_usuario, bloqueio_id):
	disponibilidade_service.apagar_bloqueio(dados_usuario['id_usuario'], bloqueio_id)
	return jsonify({'message': 'Bloqueio removido'}), 200


@prof_bp.route('/me/desempenho', methods=['GET'])
@profissional_autenticado
def desempenho(dados_usuario):
	return jsonify(profissionais_service.desempenho(dados_usuario['id_usuario'], request.args.get('dias', '30'))), 200


@prof_bp.route('/<int:id>/horarios', methods=['GET'])
def horarios(id):
	argumentos = request.args
	return jsonify(disponibilidade_service.horarios_livres(
		id, argumentos.get('de'), argumentos.get('ate'), argumentos.get('duracao'))), 200


@prof_bp.route('/', methods=['POST'])
def cadastro_profissional():
	cadastro = profissionais_service.cadastrar(request.form, request.files.get('imagem'))
	return jsonify({"message": "Profissional cadastrado com sucesso", **cadastro}), 201


@prof_bp.route('/me', methods=['PATCH'])
@profissional_autenticado
def atualizar_profissional(dados_usuario):
	profissionais_service.atualizar(dados_usuario.get('id_usuario'), request.get_json(silent=True) or {})
	return jsonify({'message': 'Perfil profissional atualizado'}), 200
