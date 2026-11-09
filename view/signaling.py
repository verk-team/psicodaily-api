"""WebSocket da videochamada (/ws/signaling?sessao_id=<id>).

A chamada é P2P: o vídeo vai direto de um navegador para o outro, sem passar pelo servidor. Mas, para os dois
navegadores se acharem, eles precisam trocar antes alguns dados de conexão (offer/answer e candidatos ICE, do
WebRTC). Esse "aperto de mão" é o signaling: o servidor só repassa essas mensagens de um lado para o outro.
"""
from flask import Blueprint, request

from extensions.websocket import sock
from services.auth_service import usuario_do_token
from services.consultas_service import avisar_entrada_na_sala, sala_da_sessao
from services.signaling_service import signaling_service
from util.usuarios import origem_permitida, token_da_requisicao

signaling_bp = Blueprint('signaling', __name__)


@sock.route("/ws/signaling", bp=signaling_bp)
def signaling(ws):
    """Entra na sala da consulta se o usuário é participante e a sala está aberta; senão fecha a conexão."""
    try:
        if not origem_permitida():
            ws.close()
            return
        token = token_da_requisicao()
        id_usuario = usuario_do_token(token).get("id_usuario") if token else None
        try:
            sessao_id = int(request.args.get("sessao_id", ""))
        except (TypeError, ValueError):
            sessao_id = None

        # sala_da_sessao lê o banco e já devolve a conexão ao pool antes da chamada começar: uma videochamada
        # pode durar uma hora, e segurar uma conexão do banco esse tempo todo esgotaria o pool
        sala = sala_da_sessao(id_usuario, sessao_id) if id_usuario and sessao_id else None
        if not sala:
            ws.close()
            return

        nome_da_sala = f"sessao:{sessao_id}"
        avisar_entrada_na_sala(sala, id_usuario, signaling_service.presentes(nome_da_sala))
        participante = sala['participantes'][id_usuario]
        # O e-mail fica no servidor: na sala, cada um só fica sabendo o id e o nome do outro
        signaling_service.handle_connection(ws, nome_da_sala, {'id_usuario': id_usuario, 'nome': participante['nome']})
    except Exception as e:
        print("ERRO WS:", repr(e))
        ws.close()
