"""Rotas do chat entre paciente e profissional (/api/chat) e o WebSocket /ws/chat.

As mensagens são enviadas pelas rotas HTTP (POST). O WebSocket só recebe: o servidor empurra por ele cada
mensagem nova das conversas do usuário, para a tela atualizar na hora sem ficar perguntando.
"""
from flask import Blueprint, jsonify, request

from extensions.websocket import sock
from services import chat_service
from services.auth_service import usuario_do_token
from services.erros import ErroNegocio
from util.rotas import sem_cache
from util.usuarios import origem_permitida, token_da_requisicao, usuario_autenticado

chat_bp = Blueprint('chat', __name__, url_prefix='/api/chat')


@chat_bp.route('/', methods=['GET'])
@usuario_autenticado
def conversas(dados_usuario):
    """GET /api/chat/: as conversas do usuário logado."""
    lista = chat_service.conversas(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'))
    return sem_cache(jsonify({'conversas': lista}))


@chat_bp.route('/<int:outro_id>', methods=['GET'])
@usuario_autenticado
def mensagens(dados_usuario, outro_id):
    """GET /api/chat/<outro_id>: mensagens da conversa com outro_id.

    Paginação: limite (1 a 100) e, no máximo, um entre antes (mensagens mais antigas que este id) e
    depois (mensagens mais novas que este id, usado para buscar o que chegou enquanto a tela estava desconectada).
    """
    try:
        limite = int(request.args.get('limite', 50))
        antes = int(request.args['antes']) if 'antes' in request.args else None
        depois = int(request.args['depois']) if 'depois' in request.args else None
        if not 1 <= limite <= 100 or (antes is not None and depois is not None):
            raise ValueError()
    except ValueError:
        return jsonify({'error': 'Use limite de 1 a 100 e no maximo um entre antes e depois'}), 400
    itens = chat_service.mensagens(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'), outro_id,
                                   limite, antes, depois)
    return sem_cache(jsonify({'mensagens': itens}))


@chat_bp.route('/<int:outro_id>', methods=['POST'])
@usuario_autenticado
def enviar(dados_usuario, outro_id):
    """POST /api/chat/<outro_id>: envia uma mensagem de texto."""
    mensagem = chat_service.enviar(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'), outro_id,
                                   request.get_json(silent=True))
    return jsonify({'mensagem': mensagem}), 201


@chat_bp.route('/<int:outro_id>/arquivo', methods=['POST'])
@usuario_autenticado
def enviar_arquivo(dados_usuario, outro_id):
    """POST /api/chat/<outro_id>/arquivo, multipart: `arquivo` (PDF, JPG ou PNG, ate 10 MB) e `texto` opcional."""
    arquivo = request.files.get('arquivo')
    nome_arquivo = arquivo.filename if arquivo else None
    mensagem = chat_service.enviar_arquivo(dados_usuario['id_usuario'], dados_usuario.get('usuario_role'), outro_id,
                                           nome_arquivo, arquivo, request.form.get('texto', ''))
    return jsonify({'mensagem': mensagem}), 201


@chat_bp.route('/arquivo/<int:mensagem_id>', methods=['GET'])
@usuario_autenticado
def baixar_arquivo(dados_usuario, mensagem_id):
    """GET /api/chat/arquivo/<mensagem_id>: link temporário para baixar o anexo, só para quem está na conversa."""
    return sem_cache(jsonify(chat_service.url_do_arquivo(dados_usuario['id_usuario'], mensagem_id)))


@sock.route('/ws/chat')
def tempo_real(ws):
    """Recebe {"type": "mensagem", "paciente_id", "profissional_id", "mensagem"} de toda conversa do usuario."""
    token = token_da_requisicao()
    try:
        if not origem_permitida() or not token:
            raise ErroNegocio('Conexao recusada', 401)
        dados = usuario_do_token(token)
        # Fica preso aqui enquanto a conexão estiver aberta
        chat_service.manter_conexao(dados['id_usuario'], dados.get('usuario_role'), ws)
    except ErroNegocio:
        ws.close()
