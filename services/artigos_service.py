"""Artigos escritos pelos profissionais do plano Premium, com curtidas.

No banco a tabela se chama CONTEUDO; na API o nome é artigo. O texto (corpo) é Markdown, um jeito de
formatar texto com símbolos simples (# título, **negrito**), que o front transforma em HTML.
"""
import re

from database.db import transacao
from services import assinaturas_service
from services.erros import ErroNegocio
from services.upload_service import upload_service

LIMITES = {'titulo': (3, 200), 'subtitulo': (3, 300), 'corpo': (1, 20000)}
# Markdown sem HTML: o front renderiza o Markdown, então tag HTML é recusada. Sem isso, um autor poderia
# esconder um <script> no artigo e ele rodaria no navegador de quem lê (ataque XSS)
TAG_HTML = re.compile(r'<\s*/?\s*[a-zA-Z!]')
TAMANHO_PAGINA, MAXIMO_PAGINA, MAXIMO_NUMERO_PAGINA = 10, 50, 100000
ORDEM = 'ORDER BY c.PUBLICADO_EM DESC, c.CONTEUDO_ID DESC'

# Artigo no ar = autor ativo com Premium válido (artigos são um benefício do Premium). Se o autor sai do
# Premium, o artigo fica guardado, fora do ar, e volta se ele assinar de novo.
ORIGEM = '''FROM CONTEUDO c
    JOIN USUARIO u ON u.ID_USUARIO = c.PROFISSIONAL_ID
    JOIN ASSINATURA a ON a.PROFISSIONAL_ID = c.PROFISSIONAL_ID
    WHERE u.ATIVO = TRUE AND a.PLANO = 'PREMIUM' AND a.VALIDA_ATE >= CURRENT_DATE'''


def _colunas(com_corpo):
    """O primeiro ? e o usuario que esta lendo, para saber se ele curtiu."""
    return f'''c.CONTEUDO_ID, c.TITULO, c.SUBTITULO, c.PUBLICADO_EM, c.ATUALIZADO_EM, c.PROFISSIONAL_ID, u.NOME,
        (SELECT COUNT(*) FROM CURTIDA_ARTIGO k WHERE k.CONTEUDO_ID = c.CONTEUDO_ID),
        (SELECT COUNT(*) FROM CURTIDA_ARTIGO k WHERE k.CONTEUDO_ID = c.CONTEUDO_ID AND k.USUARIO_ID = ?)
        {', c.CORPO' if com_corpo else ''}'''


def _serializar(linha):
    artigo_id, titulo, subtitulo, publicado_em, atualizado_em, autor_id, autor_nome, curtidas, curtido, *corpo = linha
    artigo = {
        'artigo_id': artigo_id, 'titulo': titulo, 'subtitulo': subtitulo,
        'publicado_em': publicado_em.isoformat(),
        'atualizado_em': atualizado_em.isoformat() if atualizado_em else None,
        'autor': {'id_usuario': autor_id, 'nome': autor_nome, 'foto_url': upload_service.url_foto_usuario(autor_id)},
        'curtidas': curtidas, 'curtido': curtido > 0,
    }
    if corpo:
        # Texto longo pode vir do fdb como leitor de BLOB em vez de str
        artigo['corpo'] = corpo[0].read() if hasattr(corpo[0], 'read') else corpo[0]
    return artigo


def _inteiro(args, nome, padrao, maximo):
    """Parâmetro inteiro da query string, de 1 a `maximo`."""
    try:
        valor = int(args.get(nome, padrao))
    except (TypeError, ValueError):
        valor = 0
    if not 1 <= valor <= maximo:
        raise ErroNegocio(f'{nome} deve ser um inteiro de 1 a {maximo}')
    return valor


def validar(dados, parcial=False):
    """Confere título, subtítulo e corpo. parcial=True (edição): só os campos enviados são exigidos."""
    if not isinstance(dados, dict):
        raise ErroNegocio('Envie um objeto JSON')
    if set(dados) - set(LIMITES):
        raise ErroNegocio('Campos permitidos: titulo, subtitulo e corpo')
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


def listar(usuario_id, args):
    """Pagina de artigos no ar, mais recentes primeiro, sem o corpo; autor= filtra por profissional."""
    page = _inteiro(args, 'page', 1, MAXIMO_NUMERO_PAGINA)
    page_size = _inteiro(args, 'page_size', TAMANHO_PAGINA, MAXIMO_PAGINA)
    filtro, params = ORIGEM, []
    if 'autor' in args:
        filtro += ' AND c.PROFISSIONAL_ID = ?'
        # 2147483647: o maior INTEGER do Firebird
        params.append(_inteiro(args, 'autor', None, 2147483647))
    with transacao() as cur:
        cur.execute(f'SELECT FIRST ? SKIP ? {_colunas(False)} {filtro} {ORDEM}',
                    (page_size, (page - 1) * page_size, usuario_id, *params))
        artigos = [_serializar(linha) for linha in cur.fetchall()]
        cur.execute(f'SELECT COUNT(*) {filtro}', params)
        total = cur.fetchone()[0]
    return {'page': page, 'page_size': page_size, 'total': total,
            'total_pages': (total + page_size - 1) // page_size, 'artigos': artigos}


def _no_ar(cur, usuario_id, artigo_id):
    """O artigo completo, se ele está no ar; senão 404."""
    cur.execute(f'SELECT {_colunas(True)} {ORIGEM} AND c.CONTEUDO_ID = ?', (usuario_id, artigo_id))
    linha = cur.fetchone()
    if linha is None:
        raise ErroNegocio('Artigo não encontrado', 404)
    return _serializar(linha)


def detalhe(usuario_id, artigo_id):
    with transacao() as cur:
        return _no_ar(cur, usuario_id, artigo_id)


def destaques(limite):
    """Site publico (Home, sem login): vem com o corpo, porque o visitante le o artigo ali mesmo."""
    with transacao() as cur:
        cur.execute(f'SELECT FIRST ? {_colunas(True)} {ORIGEM} {ORDEM}', (limite, None))
        return [_serializar(linha) for linha in cur.fetchall()]


def criar(profissional_id, dados):
    """Publica um artigo (exige o plano Premium)."""
    limpos = validar(dados)
    with transacao() as cur:
        assinaturas_service.exigir(cur, profissional_id, 'PREMIUM')
        cur.execute('''INSERT INTO CONTEUDO (PROFISSIONAL_ID, TITULO, SUBTITULO, CORPO) VALUES (?, ?, ?, ?)
            RETURNING CONTEUDO_ID''', (profissional_id, limpos['titulo'], limpos['subtitulo'], limpos['corpo']))
        return _no_ar(cur, profissional_id, cur.fetchone()[0])


def atualizar(profissional_id, artigo_id, dados):
    """O autor edita o próprio artigo (exige o plano Premium)."""
    limpos = validar(dados, parcial=True)
    definicoes = ', '.join(f'{campo.upper()} = ?' for campo in limpos)
    with transacao() as cur:
        assinaturas_service.exigir(cur, profissional_id, 'PREMIUM')
        cur.execute(f'''UPDATE CONTEUDO SET {definicoes}, ATUALIZADO_EM = CURRENT_TIMESTAMP
            WHERE CONTEUDO_ID = ? AND PROFISSIONAL_ID = ? RETURNING CONTEUDO_ID''',
                    (*limpos.values(), artigo_id, profissional_id))
        linha = cur.fetchone()
        # Sem linha afetada, o RETURNING do Firebird devolve uma linha de NULLs
        if linha is None or linha[0] is None:
            raise ErroNegocio('Artigo não encontrado', 404)
        return _no_ar(cur, profissional_id, artigo_id)


def apagar(profissional_id, artigo_id):
    # Sem exigir Premium: quem perdeu o plano ainda pode tirar o que escreveu
    with transacao() as cur:
        cur.execute('DELETE FROM CONTEUDO WHERE CONTEUDO_ID = ? AND PROFISSIONAL_ID = ? RETURNING CONTEUDO_ID',
                    (artigo_id, profissional_id))
        linha = cur.fetchone()
        if linha is None or linha[0] is None:
            raise ErroNegocio('Artigo não encontrado', 404)


def curtir(usuario_id, artigo_id, curtido):
    """Liga ou desliga a curtida. Repetir o pedido nao muda nada, entao o front pode reenviar sem contar duas vezes."""
    with transacao() as cur:
        cur.execute(f'SELECT 1 {ORIGEM} AND c.CONTEUDO_ID = ?', (artigo_id,))
        if cur.fetchone() is None:
            raise ErroNegocio('Artigo não encontrado', 404)
        if curtido:
            cur.execute('''UPDATE OR INSERT INTO CURTIDA_ARTIGO (CONTEUDO_ID, USUARIO_ID) VALUES (?, ?)
                MATCHING (CONTEUDO_ID, USUARIO_ID)''', (artigo_id, usuario_id))
        else:
            cur.execute('DELETE FROM CURTIDA_ARTIGO WHERE CONTEUDO_ID = ? AND USUARIO_ID = ?', (artigo_id, usuario_id))
        cur.execute('SELECT COUNT(*) FROM CURTIDA_ARTIGO WHERE CONTEUDO_ID = ?', (artigo_id,))
        return {'curtidas': cur.fetchone()[0], 'curtido': curtido}
