"""Instância do flask-sock, a extensão de WebSocket, criada aqui e ligada ao app em app.py.

WebSocket é uma conexão que fica aberta entre o navegador e o servidor, e os dois lados podem mandar
mensagens a qualquer momento. É usado no chat (/ws/chat) e na videochamada (/ws/signaling).
Fica num arquivo próprio para as views importarem `sock` sem importar o app (o que daria import circular).
"""
from flask_sock import Sock

sock = Sock()
