"""WhatsApp pelo WAHA: enviar avisos e identificar quem mandou mensagem.

WAHA (github.com/devlikeapro/waha) é um WhatsApp não oficial controlado por HTTP. Roda como um serviço
nosso no Northflank, logado no número da plataforma. Sem WAHA_URL (dev, CI), nada é enviado.
chatId é como o WhatsApp identifica uma conversa: '5511999999999@c.us' (55 = Brasil, 11 = DDD).
"""
import hashlib
import hmac
import json
import logging
import os
import re
import threading
from urllib.request import Request, urlopen

from database.db import transacao

logger = logging.getLogger(__name__)

SESSAO = 'default'  # nome da sessão (o número logado) dentro do WAHA


def _waha(metodo, caminho, corpo=None):
    """Chama a API HTTP do WAHA e devolve o JSON da resposta."""
    req = Request(f"{os.getenv('WAHA_URL', '').rstrip('/')}{caminho}", method=metodo,
                  data=json.dumps(corpo).encode() if corpo is not None else None,
                  headers={'X-Api-Key': os.getenv('WAHA_API_KEY', ''), 'Content-Type': 'application/json',
                           'Accept': 'application/json'})
    with urlopen(req, timeout=15) as resposta:
        return json.load(resposta)


def chat_id(telefone):
    """Telefone do cadastro ('(11) 99999-9999') -> chatId do WhatsApp ('5511999999999@c.us'); None se nao der."""
    digitos = re.sub(r'\D', '', telefone or '')
    # Sem o código do país (DDD + número): põe o 55 do Brasil na frente
    if len(digitos) in (10, 11):
        digitos = '55' + digitos
    return f'{digitos}@c.us' if len(digitos) in (12, 13) else None


def _enviar(telefone, texto):
    # Aceita o chatId de quem mandou mensagem (inclusive @lid) ou o telefone do cadastro
    destino = telefone if '@' in (telefone or '') else chat_id(telefone)
    if not destino or not os.getenv('WAHA_URL'):
        return
    try:
        _waha('POST', '/api/sendText', {'session': SESSAO, 'chatId': destino, 'text': texto})
    except Exception:
        # Melhor esforço, como o e-mail: o aviso já está gravado em NOTIFICACAO
        logger.exception('WhatsApp nao enviado para %s', destino)


def enviar_async(telefone, texto):
    """Envia em segundo plano, sem a requisição esperar o WAHA."""
    threading.Thread(target=_enviar, args=(telefone, texto), daemon=True).start()


def assinatura_valida(corpo, assinatura):
    """HMAC sha512 do corpo bruto com WAHA_HMAC_KEY (header X-Webhook-Hmac do WAHA).

    HMAC é uma "assinatura" calculada a partir do corpo e de uma chave secreta. Recalculamos aqui e comparamos:
    se bate, a mensagem veio de quem tem a chave (o WAHA) e não foi alterada no caminho.
    """
    chave = os.getenv('WAHA_HMAC_KEY')
    if not chave or not assinatura:
        return False
    esperado = hmac.new(chave.encode(), corpo, hashlib.sha512).hexdigest()
    # compare_digest leva o mesmo tempo acertando ou errando, então não dá para adivinhar a assinatura pelo tempo
    return hmac.compare_digest(esperado, assinatura)


def telefone_do_remetente(remetente):
    """'5511999999999@c.us' -> '5511999999999'. O WhatsApp tambem manda ids ocultos (@lid): o WAHA traduz."""
    if remetente.endswith('@c.us'):
        return remetente.split('@')[0]
    if remetente.endswith('@lid'):
        lid = remetente.split("@")[0]
        try:
            traduzido = _waha('GET', f'/api/{SESSAO}/lids/{lid}').get('pn') or ''
            return traduzido.split('@')[0] or None
        except Exception:
            logger.exception('Nao traduziu o @lid %s', remetente)
    return None


def usuario_por_telefone(telefone):
    """(id, papel, nome) de quem tem esse numero no cadastro, comparando DDD + 8 ultimos digitos.

    Os 8 ultimos cobrem o celular com ou sem o 9 da frente, que o WhatsApp omite em numeros antigos.
    ponytail: varre os usuarios ativos em Python; coluna normalizada e indice se o cadastro crescer
    """
    digitos = re.sub(r'\D', '', telefone or '')
    if digitos.startswith('55') and len(digitos) in (12, 13):
        digitos = digitos[2:]
    if len(digitos) not in (10, 11):
        return None
    chave = (digitos[:2], digitos[-8:])
    with transacao() as cur:
        cur.execute('SELECT ID_USUARIO, USUARIO_ROLE, NOME, TELEFONE FROM USUARIO WHERE ATIVO = TRUE')
        for usuario_id, papel, nome, cadastrado in cur.fetchall():
            numero = re.sub(r'\D', '', cadastrado or '')
            if len(numero) in (10, 11) and (numero[:2], numero[-8:]) == chave:
                return usuario_id, papel, nome
    return None
