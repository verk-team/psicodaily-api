"""Conta do usuário: cadastro de paciente, a parte comum do cadastro de qualquer papel, perfil e foto."""
import logging

from database.db import transacao
from funcao import (obter_campo, somente_digitos, validar_cpf, validar_email, validar_imagem_usuario,
                    validar_nome, validar_senha, validar_telefone)
from services import email_service
from services.auditoria_service import auditar
from services.auth_service import criar_hash_senha, gerar_codigo
from services.erros import ErroNegocio
from services.upload_service import upload_service

# Campo da API -> coluna do banco. Só estes campos podem ser editados pelo próprio usuário
CAMPOS_PERFIL = {"nome": "NOME", "email": "EMAIL", "telefone": "TELEFONE", "cpf": "CPF"}
logger = logging.getLogger(__name__)


def salvar_imagem_usuario(imagem, id_usuario):
    """Envia a foto de perfil para o R2, se veio uma."""
    if not imagem or not imagem.filename:
        return
    upload_service.upload_file(imagem.stream, id_usuario)


def salvar_foto_do_cadastro(imagem, id_usuario):
    """Depois do commit do cadastro: a foto e opcional, entao falha nela nao desfaz a conta (troca-se em Preferencias)."""
    try:
        salvar_imagem_usuario(imagem, id_usuario)
    except ErroNegocio as erro:
        logger.warning('Cadastro %s sem foto: %s', id_usuario, erro.mensagem)


def criar_usuario_base(cur, formulario, imagem, usuario_role, exigir_confirmacao_senha=False):
    """Valida e insere o USUARIO dentro da transacao do chamador; nao commita.

    Usado pelo cadastro de paciente e pelo de profissional (que grava o resto em PROFISSIONAL na mesma transação).
    A conta nasce inativa, com um código de ativação que vai por e-mail.
    """
    email = obter_campo(formulario, 'email').lower()
    nome = obter_campo(formulario, 'nome')
    telefone = obter_campo(formulario, 'telefone')
    senha = obter_campo(formulario, 'senha')
    confirmar_senha = obter_campo(formulario, 'confirmar_senha', 'confirmarSenha')
    cpf = somente_digitos(obter_campo(formulario, 'cpf'))

    if not nome or not email or not telefone or not senha or not cpf:
        raise ErroNegocio("Todos os campos de usuario sao obrigatorios")
    if not validar_nome(nome):
        raise ErroNegocio("Nome invalido")
    if not validar_email(email):
        raise ErroNegocio("Email invalido")
    if not validar_cpf(cpf):
        raise ErroNegocio("CPF invalido")
    if not validar_telefone(telefone):
        raise ErroNegocio("Telefone invalido")
    if not validar_senha(senha):
        raise ErroNegocio("Senha nao atende aos requisitos")
    if exigir_confirmacao_senha and not confirmar_senha:
        raise ErroNegocio("Confirmacao de senha e obrigatoria")
    if exigir_confirmacao_senha and senha != confirmar_senha:
        raise ErroNegocio("As senhas nao sao iguais")
    if not validar_imagem_usuario(imagem):
        raise ErroNegocio("Imagem deve ser um arquivo JPG ou JPEG")

    cur.execute("SELECT id_usuario FROM usuario WHERE email = ?", (email,))
    if cur.fetchone():
        raise ErroNegocio("Usuario ja cadastrado")
    cur.execute("SELECT id_usuario FROM usuario WHERE cpf = ?", (cpf,))
    if cur.fetchone():
        raise ErroNegocio("CPF ja cadastrado")

    codigo = gerar_codigo()
    cur.execute("""INSERT INTO usuario (nome, email, telefone, senha, cpf, codigo, usuario_role)
                      VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id_usuario""",
                (nome, email, telefone, criar_hash_senha(senha), cpf, codigo, usuario_role))
    usuario_criado = cur.fetchone()
    if usuario_criado is None:
        raise RuntimeError("Banco nao retornou o usuario criado")

    id_usuario = usuario_criado[0]
    return {"id_usuario": id_usuario, "email": email, "codigo": codigo, "nome": nome}


def enviar_ativacao(usuario):
    """Manda o código de ativação por e-mail, em segundo plano."""
    email_service.enviar_email_ativacao(usuario["email"], usuario["codigo"], usuario["nome"])


def cadastrar_paciente(formulario, imagem):
    with transacao() as cur:
        usuario = criar_usuario_base(cur, formulario, imagem, 'PACIENTE', exigir_confirmacao_senha=True)
    # Foto e e-mail só depois do commit: se o cadastro falhar, não sobra foto nem e-mail de uma conta que não existe
    salvar_foto_do_cadastro(imagem, usuario["id_usuario"])
    enviar_ativacao(usuario)
    return usuario


def perfil(id_usuario):
    """Os dados da conta de quem está logado."""
    with transacao() as cur:
        # A meta mensal sai aqui, e não no perfil público do profissional: só o dono vê (null para paciente e admin)
        cur.execute('''SELECT u.ID_USUARIO, u.NOME, u.EMAIL, u.USUARIO_ROLE, u.TELEFONE, u.CPF, p.META_MENSAL_CENTAVOS
            FROM USUARIO u LEFT JOIN PROFISSIONAL p ON p.USUARIO_ID = u.ID_USUARIO
            WHERE u.ID_USUARIO = ? AND u.ATIVO = TRUE''', (id_usuario,))
        usuario = cur.fetchone()
    if usuario is None:
        raise ErroNegocio("Usuario indisponivel", 401)
    id_usuario, nome, email, papel, telefone, cpf, meta_mensal = usuario
    return {
        "id_usuario": id_usuario, "nome": nome,
        "email": email, "tipo_usuario": papel,
        "telefone": telefone, "cpf": cpf,
        "foto_url": upload_service.url_foto_usuario(id_usuario),
        "meta_mensal_centavos": meta_mensal,
    }


def atualizar_perfil(id_usuario, formulario, imagem, substituir_tudo):
    """Edita nome, e-mail, telefone, CPF e a foto. substituir_tudo=True e o PUT: exige todos os campos."""
    with transacao() as cur:
        cur.execute("SELECT ATIVO, EMAIL, CPF FROM USUARIO WHERE ID_USUARIO = ?", (id_usuario,))
        usuario = cur.fetchone()
        if not usuario:
            raise ErroNegocio("Usuario indisponivel", 401)
        ativo, email_salvo, cpf_salvo = usuario
        if not ativo:
            raise ErroNegocio("Usuario indisponivel", 401)
        email_atual = (email_salvo or "").strip().lower()
        cpf_atual = somente_digitos(cpf_salvo or "")

        if not hasattr(formulario, "keys"):
            raise ErroNegocio("Formato de atualizacao invalido")
        if not set(formulario).issubset(CAMPOS_PERFIL):
            raise ErroNegocio("Campo de perfil invalido")

        # Valida cada campo enviado com o validador dele (e normaliza e-mail e CPF antes)
        atualizacoes = {}
        for campo in formulario:
            valor = obter_campo(formulario, campo)
            if campo == "email":
                valor = valor.lower()
                valido = validar_email(valor)
            elif campo == "nome":
                valido = validar_nome(valor)
            elif campo == "telefone":
                valido = validar_telefone(valor)
            else:
                valor = somente_digitos(valor)
                valido = validar_cpf(valor)
            if not valido:
                raise ErroNegocio(f"{campo.capitalize()} invalido")
            atualizacoes[campo] = valor
        if substituir_tudo and set(atualizacoes) != set(CAMPOS_PERFIL):
            raise ErroNegocio("PUT exige nome, email, telefone e CPF")
        if not atualizacoes and not (imagem and imagem.filename):
            raise ErroNegocio("Informe um campo para atualizar")
        if not validar_imagem_usuario(imagem):
            raise ErroNegocio("Imagem deve ser um arquivo JPG ou JPEG")

        # E-mail e CPF são únicos: não podem ficar iguais aos de outra conta
        if "email" in atualizacoes and atualizacoes["email"] != email_atual:
            cur.execute("SELECT FIRST 1 1 FROM USUARIO WHERE LOWER(EMAIL) = ? AND ID_USUARIO <> ?",
                        (atualizacoes["email"], id_usuario))
            if cur.fetchone():
                raise ErroNegocio("Email ja cadastrado", 409)
        if "cpf" in atualizacoes and atualizacoes["cpf"] != cpf_atual:
            cur.execute("SELECT FIRST 1 1 FROM USUARIO WHERE CPF = ? AND ID_USUARIO <> ?",
                        (atualizacoes["cpf"], id_usuario))
            if cur.fetchone():
                raise ErroNegocio("CPF ja cadastrado", 409)

        if atualizacoes:
            # Os nomes das colunas vêm de CAMPOS_PERFIL (fixo no código), nunca da requisição: sem risco de SQL injection
            definicoes = ", ".join(f"{CAMPOS_PERFIL[campo]} = ?" for campo in atualizacoes)
            cur.execute(f"UPDATE USUARIO SET {definicoes} WHERE ID_USUARIO = ?", (*atualizacoes.values(), id_usuario))
            if atualizacoes.get("email", email_atual) != email_atual:
                auditar(cur, id_usuario, 'EMAIL_ALTERADO', 'USUARIO', id_usuario)
        salvar_imagem_usuario(imagem, id_usuario)


def listar():
    """Todos os usuários, para o admin."""
    with transacao() as cur:
        cur.execute("SELECT ID_USUARIO, NOME, EMAIL, USUARIO_ROLE FROM USUARIO ORDER BY NOME")
        return [
            {"id_usuario": id_usuario, "nome": nome, "email": email, "tipo": papel,
             "foto_url": upload_service.url_foto_usuario(id_usuario)}
            for id_usuario, nome, email, papel in cur.fetchall()
        ]
