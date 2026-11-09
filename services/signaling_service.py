"""Salas da videochamada: guarda quem está em cada sala e repassa as mensagens do WebRTC entre os dois lados.

WebRTC é a tecnologia do navegador que faz a chamada de vídeo direto entre os dois computadores. Antes de
conectar, cada lado precisa mandar ao outro:
- offer / answer: a "proposta" de conexão (formatos de áudio e vídeo) e a resposta do outro lado;
- ice-candidate: os endereços de rede por onde o outro lado pode tentar chegar.
O servidor não entende essas mensagens: só repassa para os outros da sala.

As salas ficam na memória deste processo. Por isso o servidor roda num processo só: com dois, cada um teria
as suas salas, e os dois participantes poderiam cair em processos diferentes sem se enxergar.
"""
import json
import threading

from simple_websocket import ConnectionClosed

# Tipos de mensagem que um participante pode mandar para os outros; qualquer outro tipo é ignorado
TIPOS_REPASSADOS = {"offer", "answer", "ice-candidate"}


class SignalingService:
    def __init__(self):
        # nome da sala -> {websocket: participante público ({id_usuario, nome})}
        self.salas = {}
        # ponytail: um lock para todas as salas e envios; por sala se houver muitas chamadas simultâneas
        self.lock = threading.Lock()

    def presentes(self, nome_da_sala):
        """ids de quem está na sala agora."""
        with self.lock:
            return {p["id_usuario"] for p in self.salas.get(nome_da_sala, {}).values()}

    def atender(self, ws, nome_da_sala, participante):
        """Põe o participante na sala e repassa as mensagens dele até a conexão fechar."""
        self._entrar(ws, nome_da_sala, participante)
        try:
            while True:
                mensagem_bruta = ws.receive()
                if mensagem_bruta is None:
                    break
                try:
                    mensagem = json.loads(mensagem_bruta)
                except ValueError:
                    continue  # não é JSON: ignora
                if isinstance(mensagem, dict) and mensagem.get("type") in TIPOS_REPASSADOS:
                    self._repassar(ws, nome_da_sala, mensagem)
        except ConnectionClosed:
            pass
        finally:
            self._sair(ws, nome_da_sala)

    def _entrar(self, ws, nome_da_sala, participante):
        with self.lock:
            sala = self.salas.setdefault(nome_da_sala, {})
            ja_na_sala = list(sala.values())
            sala[ws] = participante
            # Quem entrou recebe a lista de quem já estava; os que já estavam ficam sabendo que alguém entrou
            self._enviar(ws, {"type": "peers", "participantes": ja_na_sala})
            self._enviar_aos_outros(ws, nome_da_sala, {"type": "peer-joined", **participante})

    def _sair(self, ws, nome_da_sala):
        with self.lock:
            sala = self.salas.get(nome_da_sala, {})
            participante = sala.pop(ws, None)
            if participante:
                self._enviar_aos_outros(ws, nome_da_sala, {"type": "peer-left", **participante})
            # Sala vazia sai da memória
            if not sala:
                self.salas.pop(nome_da_sala, None)

    def _repassar(self, remetente, nome_da_sala, mensagem):
        with self.lock:
            self._enviar_aos_outros(remetente, nome_da_sala, mensagem)

    def _enviar_aos_outros(self, remetente, nome_da_sala, mensagem):
        for conexao in self.salas.get(nome_da_sala, {}):
            if conexao is not remetente:
                self._enviar(conexao, mensagem)

    @staticmethod
    def _enviar(conexao, mensagem):
        try:
            conexao.send(json.dumps(mensagem))
        except ConnectionClosed:
            # O receive() dessa conexão vai terminar e chamar _sair().
            pass


# Uma instância só, compartilhada por todas as conexões
signaling_service = SignalingService()
