"""Configurações do app Flask (app.config.from_pyfile lê este arquivo) e origens permitidas do front."""
import os.path
from dotenv import load_dotenv

load_dotenv()

# Chave que assina os tokens de login (JWT). Fica só no .env: quem tem a chave consegue criar tokens válidos
SECRET_KEY = os.getenv("SECRET_KEY")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Endereços do front autorizados a chamar a API: usados pelo CORS do app.py e na checagem de origem dos
# WebSockets (util/usuarios.origem_permitida). Front novo precisa entrar aqui. O app mobile não manda Origin.
ORIGENS_FRONT = [
    'http://localhost:5173',
    # Front buildado servido pelo nginx local (Dockerfile do front), que repassa /api e /ws
    'http://localhost:8080',
    'https://psicodaily-front.vercel.app',
    'http://10.92.11.34:5173',
]
