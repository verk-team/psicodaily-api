import json
import threading
import uuid

from simple_websocket import ConnectionClosed

from database.db import transacao
from services.erros import ErroNegocio
from services.upload_service import upload_service

MAXIMO_TEXTO = 2000
MAXIMO_ARQUIVO = 10 * 1024 * 1024
# O tipo vem dos primeiros bytes, nao do nome nem do Content-Type que o cliente manda
ASSINATURAS = {b'%PDF-': ('application/pdf', '.pdf'), b'\x89PNG\r\n\x1a\n': ('image/png', '.png'),
               b'\xff\xd8\xff': ('image/jpeg', '.jpg'),
               # Audio: OGG (WhatsApp), WebM (gravador do Chrome/Firefox), MP3 com e sem etiqueta ID3
               b'OggS': ('audio/ogg', '.ogg'), b'\x1aE\xdf\xa3': ('audio/webm', '.webm'),
               b'ID3': ('audio/mpeg', '.mp3'), b'\xff\xfb': ('audio/mpeg', '.mp3'), b'\xff\xf3': ('audio/mpeg', '.mp3')}
VALIDADE_URL = 300
COLUNAS = 'MENSAGEM_ID, REMETENTE_ID, TEXTO, LIDA, CRIADO_EM, ARQUIVO_NOME, ARQUIVO_TIPO, ARQUIVO_TAMANHO'

# usuario_id -> WebSockets abertos (abas e aparelhos). Em memoria, como as salas do signaling: exige processo unico.
# ponytail: um lock para todas as conexoes e envios; por usuario se houver muitos conectados
_conexoes = {}
_lock = threading.Lock()


def _colunas(papel):
    """(coluna do usuario, coluna do outro) em SESSAO e MENSAGEM."""
    if papel == 'PACIENTE':
        return 'PACIENTE_ID', 'PROFISSIONAL_ID'
    if papel in ('PSICOLOGO', 'PSIQUIATRA'):
        return 'PROFISSIONAL_ID', 'PACIENTE_ID'
    raise ErroNegocio('Chat exclusivo de pacientes e profissionais', 403)


def _par(cur, usuario_id, papel, outro_id):
    """(paciente_id, profissional_id) se existe vinculo (sessao nao cancelada); senao 404."""
    eu, outro = _colunas(papel)
    cur.execute(f'''SELECT FIRST 1 PACIENTE_ID, PROFISSIONAL_ID FROM SESSAO
        WHERE {eu} = ? AND {outro} = ? AND STATUS <> 'CANCELADO' ''', (usuario_id, outro_id))
    par = cur.fetchone()
    if par is None:
        raise ErroNegocio('Conversa nao encontrada', 404)
    return par


def _mensagem(mensagem_id, remetente_id, texto, lida, criado_em, arquivo_nome, arquivo_tipo, arquivo_tamanho):
    """`arquivo` so traz o que exibir; o download pede a URL em GET /api/chat/arquivo/<mensagem_id>."""
    arquivo = {'nome': arquivo_nome, 'tipo': arquivo_tipo, 'tamanho': arquivo_tamanho} if arquivo_tipo else None
    return {'mensagem_id': mensagem_id, 'remetente_id': remetente_id, 'texto': texto, 'lida': lida,
            'criado_em': criado_em.isoformat(), 'arquivo': arquivo}


def conversas(usuario_id, papel):
    """Todos com quem ha vinculo, com a ultima mensagem e quantas recebidas nao foram lidas."""
    eu, outro = _colunas(papel)
    with transacao() as cur:
        cur.execute(f'''SELECT v.OUTRO, u.NOME, COALESCE(r.NAO_LIDAS, 0), m.MENSAGEM_ID, m.REMETENTE_ID, m.TEXTO,
                m.LIDA, m.CRIADO_EM, m.ARQUIVO_NOME, m.ARQUIVO_TIPO, m.ARQUIVO_TAMANHO
            FROM (SELECT DISTINCT {outro} AS OUTRO FROM SESSAO WHERE {eu} = ? AND STATUS <> 'CANCELADO') v
            JOIN USUARIO u ON u.ID_USUARIO = v.OUTRO
            LEFT JOIN (SELECT {outro} AS OUTRO, MAX(MENSAGEM_ID) AS ULTIMA,
                    SUM(IIF(LIDA = FALSE AND REMETENTE_ID <> ?, 1, 0)) AS NAO_LIDAS
                FROM MENSAGEM WHERE {eu} = ? GROUP BY {outro}) r ON r.OUTRO = v.OUTRO
            LEFT JOIN MENSAGEM m ON m.MENSAGEM_ID = r.ULTIMA
            ORDER BY m.MENSAGEM_ID DESC NULLS LAST, u.NOME''', (usuario_id, usuario_id, usuario_id))
        linhas = cur.fetchall()
    return [{'id_usuario': outro_id, 'nome': nome, 'foto_url': upload_service.url_foto_usuario(outro_id),
             'nao_lidas': nao_lidas, 'ultima_mensagem': _mensagem(*ultima) if ultima[0] else None}
            for outro_id, nome, nao_lidas, *ultima in linhas]


def mensagens(usuario_id, papel, outro_id, limite, antes=None, depois=None):
    """Sem cursor: as ultimas `limite`. `antes`: historico mais antigo. `depois`: as novas (polling).
    Sempre em ordem cronologica; marca como lidas as recebidas."""
    with transacao() as cur:
        paciente_id, profissional_id = _par(cur, usuario_id, papel, outro_id)
        filtro, parametros, ordem = '', (), 'DESC'
        if depois is not None:
            filtro, parametros, ordem = ' AND MENSAGEM_ID > ?', (depois,), 'ASC'
        elif antes is not None:
            filtro, parametros = ' AND MENSAGEM_ID < ?', (antes,)
        cur.execute(f'''SELECT FIRST ? {COLUNAS} FROM MENSAGEM
            WHERE PACIENTE_ID = ? AND PROFISSIONAL_ID = ?{filtro} ORDER BY MENSAGEM_ID {ordem}''',
                    (limite, paciente_id, profissional_id, *parametros))
        linhas = cur.fetchall()
        cur.execute('''UPDATE MENSAGEM SET LIDA = TRUE
            WHERE PACIENTE_ID = ? AND PROFISSIONAL_ID = ? AND REMETENTE_ID <> ? AND LIDA = FALSE''',
                    (paciente_id, profissional_id, usuario_id))
    itens = [_mensagem(*linha) for linha in linhas]
    return itens if ordem == 'ASC' else itens[::-1]


def _texto(texto, minimo=1):
    texto = texto.strip() if isinstance(texto, str) else None
    if texto is None or not minimo <= len(texto) <= MAXIMO_TEXTO:
        raise ErroNegocio(f'A mensagem deve ter de {minimo} a {MAXIMO_TEXTO} caracteres')
    return texto


def _gravar(cur, usuario_id, paciente_id, profissional_id, texto, arquivo=(None, None, None, None)):
    cur.execute(f'''INSERT INTO MENSAGEM (PACIENTE_ID, PROFISSIONAL_ID, REMETENTE_ID, TEXTO,
            ARQUIVO_CHAVE, ARQUIVO_NOME, ARQUIVO_TIPO, ARQUIVO_TAMANHO) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING {COLUNAS}''', (paciente_id, profissional_id, usuario_id, texto, *arquivo))
    return _mensagem(*cur.fetchone())


def _publicar(paciente_id, profissional_id, mensagem):
    # Depois do commit; quem estiver desconectado ve no GET (nao_lidas / depois=)
    _avisar((paciente_id, profissional_id), {'type': 'mensagem', 'paciente_id': paciente_id,
                                             'profissional_id': profissional_id, 'mensagem': mensagem})
    return mensagem


def enviar(usuario_id, papel, outro_id, dados):
    if not isinstance(dados, dict) or set(dados) != {'texto'}:
        raise ErroNegocio('Envie apenas texto')
    texto = _texto(dados['texto'])
    with transacao() as cur:
        paciente_id, profissional_id = _par(cur, usuario_id, papel, outro_id)
        mensagem = _gravar(cur, usuario_id, paciente_id, profissional_id, texto)
    return _publicar(paciente_id, profissional_id, mensagem)


def _tipo_do_arquivo(conteudo):
    # M4A (iPhone, Safari) e um MP4: a marca 'ftyp' vem depois dos 4 bytes de tamanho, entao nao cabe em ASSINATURAS
    if conteudo[4:8] == b'ftyp':
        return 'audio/mp4', '.m4a'
    return next((tipo for assinatura, tipo in ASSINATURAS.items() if conteudo.startswith(assinatura)), (None, None))


def enviar_arquivo(usuario_id, papel, outro_id, nome, arquivo, texto=''):
    """Anexo PDF, JPG, PNG ou audio de ate 10 MB, com legenda opcional, no bucket privado (F6)."""
    if arquivo is None:
        raise ErroNegocio('Envie o arquivo no campo "arquivo"')
    conteudo = arquivo.read(MAXIMO_ARQUIVO + 1)
    if len(conteudo) > MAXIMO_ARQUIVO:
        raise ErroNegocio('O arquivo deve ter no maximo 10 MB', 413)
    tipo, extensao = _tipo_do_arquivo(conteudo)
    if tipo is None:
        raise ErroNegocio('Envie um arquivo PDF, JPG, PNG ou de áudio (MP3, M4A, OGG, WebM)')
    texto = _texto(texto or '', minimo=0)
    # Nome so para exibir: sem pastas do aparelho de quem enviou
    nome = (nome or '').replace('\\', '/').rsplit('/', 1)[-1].strip()[:200] or f'arquivo{extensao}'

    with transacao() as cur:
        paciente_id, profissional_id = _par(cur, usuario_id, papel, outro_id)
    # Upload fora da transacao para nao segurar uma conexao do pool durante a transferencia.
    # ponytail: se o INSERT falhar depois do upload, o objeto fica orfao no bucket; limpar por job (F7) se acontecer
    chave = f'chat/{paciente_id}-{profissional_id}/{uuid.uuid4().hex}{extensao}'
    upload_service.enviar_privado(conteudo, chave, tipo)
    with transacao() as cur:
        mensagem = _gravar(cur, usuario_id, paciente_id, profissional_id, texto, (chave, nome, tipo, len(conteudo)))
    return _publicar(paciente_id, profissional_id, mensagem)


def url_do_arquivo(usuario_id, mensagem_id):
    """URL assinada de curta duracao, so para quem participa da conversa."""
    with transacao() as cur:
        cur.execute('''SELECT ARQUIVO_CHAVE, ARQUIVO_NOME, ARQUIVO_TIPO FROM MENSAGEM
            WHERE MENSAGEM_ID = ? AND (PACIENTE_ID = ? OR PROFISSIONAL_ID = ?)''', (mensagem_id, usuario_id, usuario_id))
        arquivo = cur.fetchone()
    if arquivo is None or arquivo[0] is None:
        raise ErroNegocio('Arquivo nao encontrado', 404)
    return {'url': upload_service.url_assinada(*arquivo, VALIDADE_URL), 'nome': arquivo[1],
            'expira_em_segundos': VALIDADE_URL}


def manter_conexao(usuario_id, papel, ws):
    """Segura o WS do usuario ate fechar; o servidor so empurra mensagens, o cliente envia pelo POST."""
    _colunas(papel)
    with _lock:
        _conexoes.setdefault(usuario_id, set()).add(ws)
    try:
        while ws.receive() is not None:
            pass
    except ConnectionClosed:
        pass
    finally:
        with _lock:
            conexoes = _conexoes.get(usuario_id, set())
            conexoes.discard(ws)
            if not conexoes:
                _conexoes.pop(usuario_id, None)


def _avisar(usuario_ids, evento):
    texto = json.dumps(evento)
    with _lock:
        for ws in [ws for usuario_id in usuario_ids for ws in _conexoes.get(usuario_id, ())]:
            try:
                ws.send(texto)
            except ConnectionClosed:
                pass  # o receive() dessa conexao termina e a remove
