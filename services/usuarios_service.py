from database.db import transacao
from funcao import (obter_campo, somente_digitos, validar_cpf, validar_email, validar_imagem_usuario,
                    validar_nome, validar_senha, validar_telefone)
from services import email_service
from services.auditoria_service import auditar
from services.auth_service import criar_hash_senha, gerar_codigo
from services.erros import ErroNegocio
from services.upload_service import upload_service

CAMPOS_PERFIL = {"nome": "NOME", "email": "EMAIL", "telefone": "TELEFONE", "cpf": "CPF"}


def salvar_imagem_usuario(imagem, id_usuario):
    if not imagem or not imagem.filename:
        return
    upload_service.upload_file(imagem.stream, id_usuario)


def criar_usuario_base(cur, formulario, imagem, usuario_role, exigir_confirmacao_senha=False):
    """Valida e insere o USUARIO dentro da transacao do chamador; nao commita."""
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
    salvar_imagem_usuario(imagem, id_usuario)
    return {"id_usuario": id_usuario, "email": email, "codigo": codigo, "nome": nome}


def enviar_ativacao(usuario):
    email_service.enviar_email_ativacao(usuario["email"], usuario["codigo"], usuario["nome"])


def cadastrar_paciente(formulario, imagem):
    with transacao() as cur:
        usuario = criar_usuario_base(cur, formulario, imagem, 'PACIENTE', exigir_confirmacao_senha=True)
    enviar_ativacao(usuario)
    return usuario


def perfil(id_usuario):
    with transacao() as cur:
        # A meta mensal sai aqui, e nao no perfil publico do profissional: so o dono ve (null para paciente e admin)
        cur.execute('''SELECT u.ID_USUARIO, u.NOME, u.EMAIL, u.USUARIO_ROLE, u.TELEFONE, u.CPF, p.META_MENSAL_CENTAVOS
            FROM USUARIO u LEFT JOIN PROFISSIONAL p ON p.USUARIO_ID = u.ID_USUARIO
            WHERE u.ID_USUARIO = ? AND u.ATIVO = TRUE''', (id_usuario,))
        usuario = cur.fetchone()
    if usuario is None:
        raise ErroNegocio("Usuario indisponivel", 401)
    return {
        "id_usuario": usuario[0], "nome": usuario[1],
        "email": usuario[2], "tipo_usuario": usuario[3],
        "telefone": usuario[4], "cpf": usuario[5],
        "foto_url": upload_service.url_foto_usuario(usuario[0]),
        "meta_mensal_centavos": usuario[6],
    }


def atualizar_perfil(id_usuario, formulario, imagem, substituir_tudo):
    """substituir_tudo=True e o PUT: exige todos os campos."""
    with transacao() as cur:
        cur.execute("SELECT ATIVO, EMAIL, CPF FROM USUARIO WHERE ID_USUARIO = ?", (id_usuario,))
        usuario = cur.fetchone()
        if not usuario or not usuario[0]:
            raise ErroNegocio("Usuario indisponivel", 401)
        email_atual = (usuario[1] or "").strip().lower()
        cpf_atual = somente_digitos(usuario[2] or "")

        if not hasattr(formulario, "keys"):
            raise ErroNegocio("Formato de atualizacao invalido")
        if not set(formulario).issubset(CAMPOS_PERFIL):
            raise ErroNegocio("Campo de perfil invalido")
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
            definicoes = ", ".join(f"{CAMPOS_PERFIL[campo]} = ?" for campo in atualizacoes)
            cur.execute(f"UPDATE USUARIO SET {definicoes} WHERE ID_USUARIO = ?", (*atualizacoes.values(), id_usuario))
            if atualizacoes.get("email", email_atual) != email_atual:
                auditar(cur, id_usuario, 'EMAIL_ALTERADO', 'USUARIO', id_usuario)
        salvar_imagem_usuario(imagem, id_usuario)


def listar():
    with transacao() as cur:
        cur.execute("SELECT ID_USUARIO, NOME, EMAIL, USUARIO_ROLE FROM USUARIO ORDER BY NOME")
        return [
            {"id_usuario": linha[0], "nome": linha[1], "email": linha[2], "tipo": linha[3],
             "foto_url": upload_service.url_foto_usuario(linha[0])}
            for linha in cur.fetchall()
        ]
