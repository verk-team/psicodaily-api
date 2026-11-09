"""Login, token JWT, código por e-mail (ativação e recuperação) e troca de senha.

Termos:
- JWT: um texto com os dados do usuário (id e papel) e a validade, assinado com a SECRET_KEY. Quem não tem
  a chave não consegue alterar o token sem invalidar a assinatura. `iat` é quando foi emitido; `exp`, quando vence.
- hash de senha (bcrypt): a senha nunca é gravada; grava-se um resumo dela que não dá para desfazer.
  No login, a senha digitada passa pelo mesmo cálculo e os resultados são comparados.
"""
import calendar
import secrets
import time
from datetime import datetime, timezone

import jwt
from flask_bcrypt import check_password_hash, generate_password_hash

import config
from database.db import transacao
from funcao import validar_senha
from services import email_service
from services.auditoria_service import auditar, auditar_avulso
from services.erros import ErroNegocio
from services.upload_service import upload_service


DURACAO_TOKEN = 8 * 3600  # segundos: o login vale 8 horas
VALIDADE_CODIGO_MIN = 15
MAXIMO_TENTATIVAS = 5
# Trecho de UPDATE que apaga o código: depois de usado, vencido ou com tentativas demais, ele não vale mais
_LIMPA_CODIGO = "CODIGO = NULL, CODIGO_EXPIRA_EM = NULL, CODIGO_TENTATIVAS = 0"


def gerar_token(payload):
    agora = int(time.time())
    return jwt.encode({**payload, 'iat': agora, 'exp': agora + DURACAO_TOKEN}, config.SECRET_KEY, algorithm='HS256')


def decodificar_token(token):
    """Confere a assinatura e a validade do token. Recusa token sem exp ou iat (os antigos não tinham validade)."""
    return jwt.decode(token, config.SECRET_KEY, algorithms=['HS256'], options={'require': ['exp', 'iat']})


def _agora_utc():
    # SENHA_ALTERADA_EM fica em UTC e sem fração de segundo, para comparar com o iat do token
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def usuario_do_token(token):
    """Payload do token se ele vale: assinatura e exp ok, usuario ativo e token emitido depois da ultima troca de senha."""
    try:
        dados = decodificar_token(token)
    except jwt.ExpiredSignatureError:
        raise ErroNegocio("Expired token", 401)
    except jwt.InvalidTokenError:
        raise ErroNegocio("Token invalido", 401)

    with transacao() as cur:
        cur.execute("SELECT ATIVO, SENHA_ALTERADA_EM FROM USUARIO WHERE ID_USUARIO = ?", (dados.get('id_usuario'),))
        usuario = cur.fetchone()
    if not usuario:
        raise ErroNegocio("Token invalido", 401)
    ativo, senha_alterada_em = usuario
    # Trocar a senha derruba os logins anteriores: um token emitido antes da troca não vale mais
    emitido_antes_da_troca = senha_alterada_em and dados['iat'] < calendar.timegm(senha_alterada_em.timetuple())
    if not ativo or emitido_antes_da_troca:
        raise ErroNegocio("Token invalido", 401)
    return dados


def senha_correta(senha_hash, senha):
    return check_password_hash(senha_hash, senha)


def criar_hash_senha(senha):
    return generate_password_hash(senha)


def gerar_codigo():
    """Código de 6 dígitos ('004213'). `secrets` sorteia de forma imprevisível, ao contrário de `random`."""
    return f"{secrets.randbelow(1000000):06d}"


def _conferir_codigo(cur, email, codigo):
    """(id_usuario, None) se o codigo confere; senao (None, mensagem), ja contando a tentativa.
    Devolve o erro em vez de levantar: a excecao desfaria a contagem, porque transacao() so commita sem excecao."""
    cur.execute("""SELECT ID_USUARIO, CODIGO, CASE WHEN CODIGO_EXPIRA_EM < CURRENT_TIMESTAMP THEN 1 ELSE 0 END
        FROM USUARIO WHERE LOWER(EMAIL) = ?""", (email,))
    usuario = cur.fetchone()
    if not usuario:
        return None, "Codigo invalido"
    id_usuario, codigo_salvo, expirou = usuario
    if codigo_salvo is None:
        return None, "Codigo invalido"
    if expirou:
        cur.execute(f"UPDATE USUARIO SET {_LIMPA_CODIGO} WHERE ID_USUARIO = ?", (id_usuario,))
        return None, "Codigo expirado, solicite um novo"
    # compare_digest leva o mesmo tempo acertando ou errando, então não dá para descobrir o código medindo o tempo
    if not secrets.compare_digest(str(codigo_salvo).strip().encode(), codigo.encode()):
        cur.execute("""UPDATE USUARIO SET CODIGO_TENTATIVAS = COALESCE(CODIGO_TENTATIVAS, 0) + 1
            WHERE ID_USUARIO = ? RETURNING CODIGO_TENTATIVAS""", (id_usuario,))
        # Sem limite, alguém poderia testar os 1 milhão de códigos até acertar
        if cur.fetchone()[0] >= MAXIMO_TENTATIVAS:
            cur.execute(f"UPDATE USUARIO SET {_LIMPA_CODIGO} WHERE ID_USUARIO = ?", (id_usuario,))
            return None, "Codigo invalidado apos muitas tentativas, solicite um novo"
        return None, "Codigo invalido"
    return id_usuario, None


def autenticar(email, senha):
    """Confere email/senha e devolve (usuario, token)."""
    if not email or not senha:
        raise ErroNegocio("Email e senha sao obrigatorios")

    with transacao() as cur:
        cur.execute("SELECT id_usuario, senha, ativo, usuario_role, nome, email FROM usuario WHERE LOWER(email) = LOWER(?)",
                    (email.strip(),))
        usuario = cur.fetchone()

    if not usuario:
        auditar_avulso(None, 'LOGIN_FALHA')
        raise ErroNegocio("Usuario nao encontrado", 404)
    id_usuario, senha_hash, ativo, papel, nome, email_cadastrado = usuario
    if not senha_correta(senha_hash, senha):
        auditar_avulso(id_usuario, 'LOGIN_FALHA', 'USUARIO', id_usuario)
        raise ErroNegocio("Senha incorreta", 401)
    if not ativo:
        raise ErroNegocio("Usuario inativo", 403)
    auditar_avulso(id_usuario, 'LOGIN', 'USUARIO', id_usuario)

    token = gerar_token({'id_usuario': id_usuario, 'usuario_role': papel})
    return {
        "id_usuario": id_usuario,
        "tipo_usuario": papel,
        "nome": nome,
        "email": email_cadastrado,
        "foto_url": upload_service.url_foto_usuario(id_usuario),
    }, token


def verificar_codigo(email, codigo):
    """Ativa a conta com o código recebido por e-mail."""
    if not email or not codigo:
        raise ErroNegocio("Email e codigo sao obrigatorios")
    email = str(email).strip().lower()

    with transacao() as cur:
        id_usuario, erro = _conferir_codigo(cur, email, str(codigo).strip())
        if id_usuario:
            cur.execute(f"UPDATE USUARIO SET ATIVO = TRUE, {_LIMPA_CODIGO} WHERE ID_USUARIO = ?", (id_usuario,))
    # O erro só é levantado depois do commit, para a tentativa errada continuar contada
    if erro:
        raise ErroNegocio(erro, 401)


def esqueci_senha(email):
    """Gera um código de recuperação que vale 15 minutos e manda por e-mail."""
    if not email:
        raise ErroNegocio("Email e obrigatorio")

    with transacao() as cur:
        cur.execute("SELECT id_usuario, email, nome FROM usuario WHERE LOWER(email) = ?", (email.strip().lower(),))
        usuario = cur.fetchone()
        if not usuario:
            raise ErroNegocio("Usuario nao encontrado", 404)
        id_usuario, email_cadastrado, nome = usuario
        codigo = gerar_codigo()
        # O código de ativação do cadastro não expira (não há rota de reenvio); este expira e também ativa a conta
        cur.execute(f"""UPDATE USUARIO SET CODIGO = ?, CODIGO_TENTATIVAS = 0,
            CODIGO_EXPIRA_EM = DATEADD({VALIDADE_CODIGO_MIN} MINUTE TO CURRENT_TIMESTAMP) WHERE ID_USUARIO = ?""",
                    (codigo, id_usuario))

    email_service.enviar_email_codigo_async(email_cadastrado, nome, codigo, "recuperacao")


def alterar_senha(email, codigo, nova_senha):
    """Troca a senha de quem esqueceu, com o código de recuperação."""
    email = (email or '').strip().lower()
    if not codigo or not nova_senha or not email:
        raise ErroNegocio("Email, codigo e senha sao obrigatorios")
    if not validar_senha(nova_senha):
        raise ErroNegocio("Senha nao atende aos requisitos")

    with transacao() as cur:
        id_usuario, erro = _conferir_codigo(cur, email, str(codigo).strip())
        if id_usuario:
            cur.execute(f"UPDATE USUARIO SET SENHA = ?, SENHA_ALTERADA_EM = ?, {_LIMPA_CODIGO} WHERE ID_USUARIO = ?",
                        (criar_hash_senha(nova_senha), _agora_utc(), id_usuario))
            auditar(cur, id_usuario, 'SENHA_ALTERADA', 'USUARIO', id_usuario)
    if erro:
        raise ErroNegocio(erro, 401)


def alterar_senha_logado(id_usuario, usuario_role, senha_atual, nova_senha):
    """Troca a senha e devolve um token novo: os anteriores, inclusive o desta sessao, deixam de valer."""
    if not senha_atual or not nova_senha:
        raise ErroNegocio("Informe a senha atual e a nova senha")
    if not validar_senha(nova_senha):
        raise ErroNegocio("A nova senha deve ter 8 a 12 caracteres e incluir maiuscula, minuscula, numero e simbolo")

    with transacao() as cur:
        cur.execute("SELECT SENHA FROM USUARIO WHERE ID_USUARIO = ? AND ATIVO = TRUE", (id_usuario,))
        usuario = cur.fetchone()
        if not usuario:
            raise ErroNegocio("Usuario indisponivel", 401)
        senha_hash = usuario[0]
        if not senha_correta(senha_hash, senha_atual):
            raise ErroNegocio("Senha atual incorreta", 401)
        cur.execute("UPDATE USUARIO SET SENHA = ?, SENHA_ALTERADA_EM = ? WHERE ID_USUARIO = ?",
                    (criar_hash_senha(nova_senha), _agora_utc(), id_usuario))
        auditar(cur, id_usuario, 'SENHA_ALTERADA', 'USUARIO', id_usuario)
    return gerar_token({'id_usuario': id_usuario, 'usuario_role': usuario_role})
