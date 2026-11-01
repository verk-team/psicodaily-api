import re

from database.db import transacao
from services import assinaturas_service
from services.erros import ErroNegocio
from services.upload_service import upload_service

COLUNAS = ('conteudo_id', 'profissional_id', 'titulo', 'corpo', 'publicado_em', 'atualizado_em')
LIMITES = {'titulo': (3, 200), 'corpo': (1, 20000)}
# Texto puro ou Markdown: o front renderiza, entao tag HTML e recusada (XSS)
TAG_HTML = re.compile(r'<\s*/?\s*[a-zA-Z!]')
MAXIMO_POR_LISTA = 100


def serializar(linha):
    conteudo = dict(zip(COLUNAS, linha))
    if hasattr(conteudo['corpo'], 'read'):
        conteudo['corpo'] = conteudo['corpo'].read()
    for campo in ('publicado_em', 'atualizado_em'):
        if conteudo[campo] is not None:
            conteudo[campo] = conteudo[campo].isoformat()
    return conteudo


def validar(dados, parcial=False):
    if not isinstance(dados, dict):
        raise ErroNegocio('Envie um objeto JSON')
    if set(dados) - set(LIMITES):
        raise ErroNegocio('Campos permitidos: titulo e corpo')
    if parcial and not dados:
        raise ErroNegocio('Nada para atualizar')
    limpos = {}
    for campo, (minimo, maximo) in LIMITES.items():
        if campo not in dados and parcial:
            continue
        valor = dados.get(campo)
        if not isinstance(valor, str) or not minimo <= len(valor.strip()) <= maximo:
            raise ErroNegocio(f'{campo} deve ter entre {minimo} e {maximo} caracteres')
        if TAG_HTML.search(valor):
            raise ErroNegocio(f'{campo} nao pode conter HTML; use texto ou Markdown')
        limpos[campo] = valor.strip()
    return limpos


def listar(profissional_id):
    # ponytail: sem paginacao, devolve os 100 mais recentes; paginar se alguem passar disso
    with transacao() as cur:
        cur.execute('''SELECT 1 FROM PROFISSIONAL p JOIN USUARIO u ON u.ID_USUARIO = p.USUARIO_ID
            WHERE p.USUARIO_ID = ? AND u.ATIVO = TRUE''', (profissional_id,))
        if cur.fetchone() is None:
            raise ErroNegocio('Profissional não encontrado', 404)
        # Divulgacao do Premium (9): abaixo dele os conteudos ficam guardados, mas fora do perfil
        if not assinaturas_service.libera(assinaturas_service.plano_ativo(cur, profissional_id), 'PREMIUM'):
            return []
        cur.execute(f'''SELECT FIRST {MAXIMO_POR_LISTA} {', '.join(COLUNAS)} FROM CONTEUDO
            WHERE PROFISSIONAL_ID = ? ORDER BY PUBLICADO_EM DESC, CONTEUDO_ID DESC''', (profissional_id,))
        return [serializar(linha) for linha in cur.fetchall()]


def destaques(limite):
    """Artigos em destaque no site publico: os mais recentes de profissionais ativos com Premium valido."""
    colunas = ', '.join(f'c.{coluna}' for coluna in COLUNAS)
    with transacao() as cur:
        cur.execute(f'''SELECT FIRST ? {colunas}, u.NOME FROM CONTEUDO c
            JOIN USUARIO u ON u.ID_USUARIO = c.PROFISSIONAL_ID
            JOIN ASSINATURA a ON a.PROFISSIONAL_ID = c.PROFISSIONAL_ID
            WHERE u.ATIVO = TRUE AND a.PLANO = 'PREMIUM' AND a.VALIDA_ATE >= CURRENT_DATE
            ORDER BY c.PUBLICADO_EM DESC, c.CONTEUDO_ID DESC''', (limite,))
        return [{**serializar(linha), 'profissional_nome': linha[-1],
                 'profissional_foto_url': upload_service.url_foto_usuario(linha[1])} for linha in cur.fetchall()]


def criar(profissional_id, dados):
    limpos = validar(dados)
    with transacao() as cur:
        assinaturas_service.exigir(cur, profissional_id, 'PREMIUM')
        cur.execute(f'''INSERT INTO CONTEUDO (PROFISSIONAL_ID, TITULO, CORPO) VALUES (?, ?, ?)
            RETURNING {', '.join(COLUNAS)}''', (profissional_id, limpos['titulo'], limpos['corpo']))
        return serializar(cur.fetchone())


def atualizar(profissional_id, conteudo_id, dados):
    limpos = validar(dados, parcial=True)
    definicoes = ', '.join(f'{campo.upper()} = ?' for campo in limpos)
    with transacao() as cur:
        assinaturas_service.exigir(cur, profissional_id, 'PREMIUM')
        cur.execute(f'''UPDATE CONTEUDO SET {definicoes}, ATUALIZADO_EM = CURRENT_TIMESTAMP
            WHERE CONTEUDO_ID = ? AND PROFISSIONAL_ID = ? RETURNING {', '.join(COLUNAS)}''',
                    (*limpos.values(), conteudo_id, profissional_id))
        linha = cur.fetchone()
        # Sem linha afetada, o RETURNING do Firebird devolve uma linha de NULLs
        if linha is None or linha[0] is None:
            raise ErroNegocio('Conteúdo não encontrado', 404)
        return serializar(linha)


def apagar(profissional_id, conteudo_id):
    with transacao() as cur:
        cur.execute('DELETE FROM CONTEUDO WHERE CONTEUDO_ID = ? AND PROFISSIONAL_ID = ? RETURNING CONTEUDO_ID',
                    (conteudo_id, profissional_id))
        linha = cur.fetchone()
        if linha is None or linha[0] is None:
            raise ErroNegocio('Conteúdo não encontrado', 404)
