"""Envio de e-mail pelo Gmail (SMTP) e o e-mail com código de ativação ou de recuperação de senha.

SMTP é o protocolo de envio de e-mail. As funções *_async mandam numa thread separada, para a requisição
não ficar esperando o Gmail responder (isso leva alguns segundos).
"""
import logging
import os
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)


def enviar_email(destinatario, assunto, mensagem, html=None):
    """Envia na hora (a função espera o Gmail). Com `html`, o e-mail leva as duas versões, texto e HTML."""
    # Credenciais só no ambiente (.env): no código elas vazavam pelo repositório público
    user = os.getenv('SMTP_USER')
    senha = os.getenv('SMTP_PASSWORD')
    if not user or not senha:
        # Dev e CI sem credencial não enviam; o aviso no log mostra se faltar em produção
        logger.warning('SMTP_USER/SMTP_PASSWORD ausentes: e-mail para %s nao enviado', destinatario)
        return

    if html:
        # "alternative": o programa de e-mail mostra o HTML e, se não conseguir, o texto puro
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(mensagem, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(mensagem, "plain", "utf-8")
    msg['Subject'] = assunto
    msg['From'] = user
    msg['To'] = destinatario

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(user, senha)
        server.send_message(msg)


def enviar_email_async(destinatario, assunto, mensagem):
    """Envia em segundo plano. `daemon=True`: a thread não impede o servidor de desligar."""
    threading.Thread(target=enviar_email, args=(destinatario, assunto, mensagem), daemon=True).start()


def enviar_email_codigo(email, nome, codigo, finalidade):
    """E-mail com o código de 6 dígitos. `finalidade` é 'ativacao' (cadastro) ou 'recuperacao' (esqueci a senha)."""
    conteudos = {
        "ativacao": {
            "assunto": "Ative sua conta no PSICOdaily",
            "mensagem": "Use o código abaixo para confirmar seu e-mail e ativar sua conta.",
            "mensagem_secundaria": "Digite este código na tela de ativação. Ele pode ser usado uma única vez.",
        },
        "recuperacao": {
            "assunto": "Código para redefinir sua senha",
            "mensagem": "Recebemos uma solicitação para redefinir a senha da sua conta.",
            "mensagem_secundaria": "Se você não solicitou a redefinição, ignore este e-mail. "
                                   "Sua senha atual continuará válida.",
        },
    }
    conteudo = conteudos.get(finalidade)
    if conteudo is None:
        raise ValueError("Finalidade de e-mail inválida")

    # Jinja preenche o modelo templates/email_codigo.html; autoescape impede que um nome com HTML quebre o e-mail
    diretorio_templates = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
    ambiente = Environment(
        loader=FileSystemLoader(diretorio_templates),
        autoescape=select_autoescape(["html"]),
    )
    html = ambiente.get_template("email_codigo.html").render(
        nome=nome or "",
        codigo=codigo,
        mensagem=conteudo["mensagem"],
        mensagem_secundaria=conteudo["mensagem_secundaria"],
    )

    texto = f"{conteudo['mensagem']}\n\nCódigo: {codigo}\n\n{conteudo['mensagem_secundaria']}"
    enviar_email(email, conteudo["assunto"], texto, html)


def enviar_email_codigo_async(email, nome, codigo, finalidade):
    threading.Thread(
        target=enviar_email_codigo,
        args=(email, nome, codigo, finalidade),
        daemon=True,
    ).start()


def enviar_email_ativacao(email, codigo, nome=""):
    enviar_email_codigo_async(email, nome, codigo, "ativacao")
