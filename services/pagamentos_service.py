import json
import math
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from database.db import transacao
from funcao import formatar_data_hora, formatar_reais
from services import notificacoes_service, planos_service
from services.erros import ErroNegocio

ARKHE_BASE_URL = os.getenv(
    'ARKHE_BASE_URL',
    'https://arkhe-backend.zbbquj.easypanel.host'
).rstrip('/')


def _arkhe_request(method, path, body=None, bruto=False, cabecalhos=None):
    """(status, corpo JSON); com bruto=True o corpo de sucesso volta em bytes (PDF do boleto)."""
    client_id = os.getenv('ARKHE_CLIENT_ID') or os.getenv('CLIENT_ID')
    client_secret = os.getenv('ARKHE_CLIENT_SECRET') or os.getenv('CLIENT_SECRET')
    if not client_id or not client_secret:
        raise ErroNegocio('Credenciais da Arkhé nao configuradas', 503)

    headers = {
        'X-Client-ID': client_id,
        'X-Client-Secret': client_secret,
        'Accept': 'application/json',
    }
    if method == 'POST':
        headers['Content-Type'] = 'application/json'
    headers.update(cabecalhos or {})

    dados = json.dumps(body).encode('utf-8') if body is not None else None
    req = Request(f'{ARKHE_BASE_URL}{path}', data=dados, method=method, headers=headers)
    try:
        with urlopen(req, timeout=15) as resposta:
            status, corpo = resposta.status, resposta.read()
            if not bruto:
                corpo = json.loads(corpo.decode('utf-8'))
    except HTTPError as erro:
        corpo = erro.read().decode('utf-8', errors='replace')
        try:
            detalhe = json.loads(corpo)
        except json.JSONDecodeError:
            detalhe = None
        if not isinstance(detalhe, dict):
            # Erros fora da API (404 do Flask, proxy) chegam em HTML
            detalhe = {'error': f'Arkhé respondeu HTTP {erro.code}'}
        return erro.code, detalhe
    except (URLError, OSError):
        # OSError cobre o timeout de leitura, que nao vira URLError
        raise ErroNegocio('Nao foi possivel conectar a Arkhé', 503)
    except ValueError:
        raise ErroNegocio('Arkhé retornou uma resposta invalida', 503)

    if bruto:
        return status, corpo
    if not isinstance(corpo, dict):
        raise ErroNegocio('Arkhé retornou uma resposta invalida', 503)
    return status, corpo


METODOS = ('PIX', 'BOLETO', 'CARTAO')
# Pix e boleto sao cobrancas que o paciente paga depois; o cartao e uma venda, aprovada ou recusada na hora
CAMINHOS = {'PIX': '/api/v1/cobrancas/pix', 'BOLETO': '/api/v1/cobrancas/boletos'}
FORMATOS_CARTAO = {
    'numero_cartao': re.compile(r'\d{16}'),
    'validade': re.compile(r'(0[1-9]|1[0-2])/\d{4}'),
    'cvv': re.compile(r'\d{3}'),
    'tipo': re.compile(r'CREDITO|DEBITO'),
}
MAXIMO_PARCELAS = 15  # o mesmo limite da Arkhé


def _ler_pedido(dados):
    if not isinstance(dados, dict) or not set(dados) <= {'id_sessao', 'metodo', 'cartao'}:
        raise ErroNegocio('Envie id_sessao, metodo (PIX, BOLETO ou CARTAO) e, se for CARTAO, cartao')
    if not dados.get('id_sessao'):
        raise ErroNegocio('id_sessao e obrigatorio')
    return dados['id_sessao'], *ler_metodo(dados)


def ler_metodo(dados):
    """(metodo, cartao) do pedido, PIX por padrao; quem chama ja conferiu os outros campos."""
    metodo = dados.get('metodo', 'PIX')
    if metodo not in METODOS:
        raise ErroNegocio('metodo deve ser PIX, BOLETO ou CARTAO')
    if (metodo == 'CARTAO') != ('cartao' in dados):
        raise ErroNegocio('Envie cartao somente com metodo CARTAO')
    return metodo, _ler_cartao(dados['cartao']) if metodo == 'CARTAO' else None


def _ler_cartao(cartao):
    # Os dados do cartao vao direto para a Arkhé: nao sao gravados nem auditados aqui
    if not isinstance(cartao, dict) or not set(FORMATOS_CARTAO) <= set(cartao) <= {*FORMATOS_CARTAO, 'parcelas'}:
        raise ErroNegocio('cartao tem numero_cartao, validade, cvv, tipo e parcelas (opcional)')
    for campo, formato in FORMATOS_CARTAO.items():
        if not isinstance(cartao[campo], str) or not formato.fullmatch(cartao[campo]):
            raise ErroNegocio('Cartao invalido: numero_cartao com 16 digitos, validade MM/AAAA, '
                              'cvv com 3 digitos e tipo CREDITO ou DEBITO')
    parcelas = cartao.get('parcelas', 1)
    if type(parcelas) is not int or not 1 <= parcelas <= MAXIMO_PARCELAS or (cartao['tipo'] == 'DEBITO' and parcelas != 1):
        raise ErroNegocio(f'parcelas deve ser de 1 a {MAXIMO_PARCELAS}, e o debito e sempre em 1')
    return {**cartao, 'parcelas': parcelas}


def _recusa(status, resposta, mensagem):
    """Erro da Arkhé: recusa com motivo vai ao paciente (cartao sem limite, CPF sem conta); o resto e 502."""
    if status in (400, 404, 409):
        raise ErroNegocio(resposta.get('mensagem') or mensagem, 402)
    raise ErroNegocio(mensagem, 502, detalhes=resposta)


def _conferir(resposta, campo_id, centavos):
    try:
        valor = Decimal(str(resposta.get('valor')))
    except InvalidOperation:
        valor = Decimal('NaN')
    if not valor.is_finite() or valor * 100 != Decimal(str(centavos)):
        raise ErroNegocio('Arkhé retornou um valor invalido', 502)
    if not isinstance(resposta.get(campo_id), int):
        raise ErroNegocio('Arkhé retornou uma cobranca sem id', 502)


def _apresentar(metodo, resposta):
    # O url_pdf da Arkhé exige as credenciais dela; o PDF sai por GET /api/pagamentos/cobranca/<id>/pdf
    return {**{chave: valor for chave, valor in resposta.items() if chave != 'url_pdf'}, 'metodo': metodo}


def criar_cobranca(paciente_id, papel, dados):
    if papel != 'PACIENTE':
        raise ErroNegocio('Somente pacientes podem pagar consultas', 403)
    sessao_id, metodo, cartao = _ler_pedido(dados)

    with transacao() as cur:
        cur.execute('''
            SELECT s.STATUS, s.VALOR_CENTAVOS, s.PROFISSIONAL_ID, s.DATA_HORA_INICIO, u.CPF
            FROM SESSAO s
            INNER JOIN USUARIO u ON u.ID_USUARIO = s.PACIENTE_ID
            WHERE s.SESSAO_ID = ? AND s.PACIENTE_ID = ?
        ''', (sessao_id, paciente_id))
        sessao = cur.fetchone()
        cur.execute('SELECT ID_COBRANCA_ARKHE, STATUS FROM PAGAMENTO WHERE SESSAO_ID = ? ORDER BY PAGAMENTO_ID DESC',
                    (sessao_id,))
        pagamentos = cur.fetchall()
    if sessao is None:
        raise ErroNegocio('Sessao nao encontrada', 404)
    status_sessao, centavos, profissional_id, inicio, cpf = sessao
    if status_sessao == 'CANCELADO':
        # `cancelada` diz ao front que a reserva expirou (ou foi cancelada) e que e preciso escolher o horario de novo
        raise ErroNegocio('Esta consulta foi cancelada e nao aceita pagamento', 409, cancelada=True)
    if any(status == 'PAGO' for _, status in pagamentos):
        raise ErroNegocio('Esta sessao ja foi paga', 409)

    # Pendentes (sempre Pix ou boleto) podem ter sido pagos sem ninguem consultar: atualiza antes de cobrar de novo
    pendentes = [consultar_cobranca(paciente_id, id_cobranca) for id_cobranca, _ in pagamentos]
    if any(cobranca['status'] == 1 for cobranca in pendentes):
        raise ErroNegocio('Esta sessao ja foi paga', 409)
    # Reaproveita a pendente do mesmo metodo: duas iguais abertas deixariam pagar duas vezes.
    # ponytail: outro metodo abre outra cobranca e a Arkhé nao cancela a antiga; pagar as duas vira reembolso (24)
    mesma = next((cobranca for cobranca in pendentes if cobranca['metodo'] == metodo), None)
    if mesma:
        return mesma

    if metodo == 'CARTAO':
        return _pagar_cartao(sessao_id, profissional_id, inicio, centavos, cartao)
    # O boleto vence no dia da consulta (ou hoje, se ela ja passou); o CPF do paciente precisa ter conta na Arkhé
    id_cobranca, cobranca, _ = cobrar(metodo, centavos, cpf=cpf, vencimento=max(date.today(), inicio.date()))

    with transacao() as cur:
        # A taxa fica gravada: trocar de plano depois nao muda o que este pagamento rende ao profissional
        cur.execute('''INSERT INTO PAGAMENTO (SESSAO_ID, ID_COBRANCA_ARKHE, METODO, VALOR_CENTAVOS,
                TAXA_PLATAFORMA_PERCENTUAL) VALUES (?, ?, ?, ?, ?)''',
                    (sessao_id, id_cobranca, metodo, centavos, planos_service.taxa_atual(cur, profissional_id)))
    return cobranca


def cobrar(metodo, centavos, cartao=None, cpf=None, vencimento=None):
    """Cobra na Arkhé (consulta ou assinatura): (id na Arkhé, resposta para o front, taxa da Arkhé em centavos).

    Pix e boleto voltam pendentes; o cartao volta aprovado ou levanta ErroNegocio. So o credito tem taxa."""
    # Arkhé recebe reais; VALOR_CENTAVOS permanece inteiro no banco.
    valor = float(Decimal(str(centavos)) / 100)
    if metodo == 'CARTAO':
        status, resposta = _arkhe_request('POST', '/api/v1/vendas/cartao', {**cartao, 'valor': valor})
        if status >= 400:
            _recusa(status, resposta, 'Arkhé nao processou o cartao')
        if resposta.get('aprovado') is not True:
            raise ErroNegocio('Arkhé retornou uma venda invalida', 502)
        _conferir(resposta, 'id_compra', centavos)
        # A Arkhé fica com 5% no credito (taxa = valor - valor_recebido); na consulta, entra no financeiro do
        # profissional (32); na assinatura, a plataforma absorve
        taxa = _numero(resposta.get('taxa'))
        taxa = int((Decimal(str(taxa)) * 100).quantize(Decimal('1'))) if taxa and taxa > 0 else None
        return resposta['id_compra'], _apresentar(metodo, resposta), taxa

    corpo = {'valor': valor}
    if metodo == 'BOLETO':
        corpo |= {'documento_pagador': cpf, 'data_vencimento': vencimento.isoformat()}
    status, resposta = _arkhe_request('POST', CAMINHOS[metodo], corpo)
    if status >= 400:
        _recusa(status, resposta, 'Arkhé nao criou a cobranca')
    _conferir(resposta, 'id_cobranca', centavos)
    return resposta['id_cobranca'], _apresentar(metodo, resposta), None


def _pagar_cartao(sessao_id, profissional_id, inicio, centavos, cartao):
    id_compra, venda, taxa = cobrar('CARTAO', centavos, cartao)
    # ponytail: se o INSERT falhar apos a aprovacao, a venda fica so na Arkhé (o 500 vai para o log); repetir
    # nao cobra de novo, porque ela recusa venda igual por 5 minutos
    with transacao() as cur:
        cur.execute('''INSERT INTO PAGAMENTO (SESSAO_ID, ID_COBRANCA_ARKHE, METODO, VALOR_CENTAVOS, STATUS, PAGO_EM,
                TAXA_ARKHE_CENTAVOS, TAXA_PLATAFORMA_PERCENTUAL)
            VALUES (?, ?, 'CARTAO', ?, 'PAGO', CURRENT_TIMESTAMP, ?, ?)''',
                    (sessao_id, id_compra, centavos, taxa, planos_service.taxa_atual(cur, profissional_id)))
        emails = _sessao_paga(cur, sessao_id, profissional_id, inicio, 'CARTAO', id_compra)
    notificacoes_service.enviar_emails(emails)
    return venda


def _pagamento_do_participante(cur, usuario_id, id_cobranca, metodos):
    cur.execute(f'''SELECT p.METODO, p.STATUS, s.SESSAO_ID, s.PROFISSIONAL_ID, s.DATA_HORA_INICIO
        FROM PAGAMENTO p JOIN SESSAO s ON s.SESSAO_ID = p.SESSAO_ID
        WHERE p.ID_COBRANCA_ARKHE = ? AND p.METODO IN ({', '.join('?' * len(metodos))})
          AND ? IN (s.PACIENTE_ID, s.PROFISSIONAL_ID)''', (id_cobranca, *metodos, usuario_id))
    pagamento = cur.fetchone()
    if pagamento is None:
        raise ErroNegocio('Cobranca nao encontrada', 404)
    return pagamento


def consultar_cobranca(usuario_id, id_cobranca):
    # Pix e boleto saem da mesma tabela da Arkhé, entao o id nao se repete entre os dois
    with transacao() as cur:
        metodo, status_local, *sessao = _pagamento_do_participante(cur, usuario_id, id_cobranca, tuple(CAMINHOS))

    cobranca = consultar_arkhe(metodo, id_cobranca)
    if cobranca['status'] == 1 and status_local == 'PENDENTE':
        _confirmar(metodo, id_cobranca, *sessao)
    return cobranca


def consultar_arkhe(metodo, id_cobranca):
    """Cobranca Pix ou boleto como a Arkhé a ve agora: status 0 (pendente) ou 1 (pago)."""
    status, resposta = _arkhe_request('GET', f'{CAMINHOS[metodo]}/{id_cobranca}')
    if status >= 400:
        raise ErroNegocio('Arkhé nao consultou a cobranca', 502, detalhes=resposta)
    if resposta.get('status') not in (0, 1):
        raise ErroNegocio('Arkhé retornou status de pagamento desconhecido', 502)
    return _apresentar(metodo, resposta)


def boleto_pdf(usuario_id, id_cobranca):
    with transacao() as cur:
        _pagamento_do_participante(cur, usuario_id, id_cobranca, ('BOLETO',))
    return pdf_boleto(id_cobranca)


def pdf_boleto(id_cobranca):
    status, pdf = _arkhe_request('GET', f'/api/v1/cobrancas/boletos/{id_cobranca}/pdf', bruto=True)
    if status >= 400:
        raise ErroNegocio('Arkhé nao gerou o PDF do boleto', 502, detalhes=pdf)
    return pdf


def _confirmar(metodo, id_cobranca, sessao_id, profissional_id, inicio):
    # shortcut: o status so muda quando alguem consulta a cobranca; webhook ou job (F7) se a Arkhé tiver
    with transacao() as cur:
        cur.execute('''UPDATE PAGAMENTO SET STATUS = 'PAGO', PAGO_EM = CURRENT_TIMESTAMP
            WHERE METODO = ? AND ID_COBRANCA_ARKHE = ? AND STATUS = 'PENDENTE' RETURNING PAGAMENTO_ID''',
                    (metodo, id_cobranca))
        if cur.fetchone()[0] is None:
            return  # outra requisicao confirmou antes
        emails = _sessao_paga(cur, sessao_id, profissional_id, inicio, metodo, id_cobranca)
    notificacoes_service.enviar_emails(emails)


def confirmar_pendentes(sessao_id):
    """Confere na Arkhé as cobranças pendentes da sessão e confirma as pagas. True se alguma estava paga."""
    with transacao() as cur:
        cur.execute('''SELECT p.METODO, p.ID_COBRANCA_ARKHE, s.PROFISSIONAL_ID, s.DATA_HORA_INICIO
            FROM PAGAMENTO p JOIN SESSAO s ON s.SESSAO_ID = p.SESSAO_ID
            WHERE p.SESSAO_ID = ? AND p.STATUS = 'PENDENTE' ''', (sessao_id,))
        pendentes = cur.fetchall()
    pagas = [(metodo, id_cobranca, profissional_id, inicio) for metodo, id_cobranca, profissional_id, inicio in pendentes
             if consultar_arkhe(metodo, id_cobranca)['status'] == 1]
    for metodo, id_cobranca, profissional_id, inicio in pagas:
        _confirmar(metodo, id_cobranca, sessao_id, profissional_id, inicio)
    return bool(pagas)


def registrar_reembolso(cur, sessao_id):
    """Na transacao do cancelamento: reembolso integral de cada pagamento pago da sessao que ainda nao tem um.

    Devolve o total reembolsado da sessao, em centavos."""
    cur.execute('''INSERT INTO REEMBOLSO (PAGAMENTO_ID, VALOR_CENTAVOS)
        SELECT p.PAGAMENTO_ID, p.VALOR_CENTAVOS FROM PAGAMENTO p
        WHERE p.SESSAO_ID = ? AND p.STATUS = 'PAGO'
          AND NOT EXISTS (SELECT 1 FROM REEMBOLSO r WHERE r.PAGAMENTO_ID = p.PAGAMENTO_ID)''', (sessao_id,))
    cur.execute('''SELECT COALESCE(SUM(r.VALOR_CENTAVOS), 0) FROM REEMBOLSO r
        JOIN PAGAMENTO p ON p.PAGAMENTO_ID = r.PAGAMENTO_ID WHERE p.SESSAO_ID = ?''', (sessao_id,))
    return cur.fetchone()[0]


def pix_saida(cpf, centavos, chave):
    """Pix da conta da plataforma para a chave CPF de alguem (reembolso, repasse); devolve o id da movimentacao.

    So funciona se a Arkhé liberar o nosso Client ID para pagamentos de saida (ARKHE_API_PAGAMENTOS_CLIENT_IDS,
    do lado dela) e se a pessoa tiver o CPF cadastrado como chave Pix la, o que nao e automatico.
    ponytail: a Arkhé guarda a resposta de cada chave de idempotencia por 7 dias; repetir depois de uma recusa
    devolve a mesma recusa, entao o admin paga por fora e marca como pago
    """
    status, resposta = _arkhe_request(
        'POST', '/api/v1/pagamentos/pix',
        {'tipo_chave': 'cpf', 'chave_pix': cpf, 'valor': float(Decimal(str(centavos)) / 100)},
        cabecalhos={'X-Idempotency-Key': chave})
    if status != 201 or not isinstance(resposta.get('id_movimentacao'), int):
        motivo = resposta.get('mensagem') or f'HTTP {status}'
        raise ErroNegocio(f'Arkhé nao fez o Pix ({motivo}). Pague por fora e marque como pago', 502)
    return resposta['id_movimentacao']


def _sessao_paga(cur, sessao_id, profissional_id, inicio, metodo, id_arkhe):
    """Confirma a sessao e devolve os avisos: ao profissional e o comprovante ao paciente (13)."""
    # O lock espera um cancelamento em andamento (paciente ou reserva expirando) terminar antes de decidir
    cur.execute('SELECT STATUS, PACIENTE_ID, VALOR_CENTAVOS FROM SESSAO WHERE SESSAO_ID = ? WITH LOCK', (sessao_id,))
    status, paciente_id, centavos = cur.fetchone()
    if status == 'CANCELADO':
        # Pago depois do cancelamento (ou da reserva expirar): a Arkhé nao cancela cobranca, entao o valor volta
        registrar_reembolso(cur, sessao_id)
        return []
    cur.execute("UPDATE SESSAO SET STATUS = 'CONFIRMADO' WHERE SESSAO_ID = ? AND STATUS = 'AGENDADO'", (sessao_id,))
    cur.execute('SELECT NOME FROM USUARIO WHERE ID_USUARIO = ?', (paciente_id,))
    nome_paciente = cur.fetchone()[0]
    quando = formatar_data_hora(inicio)
    valor = formatar_reais(centavos)
    dados = {'sessao_id': sessao_id}
    return [
        # So aqui o profissional fica sabendo: consulta sem pagamento nao conta como marcada
        notificacoes_service.notificar(cur, profissional_id, 'PAGAMENTO_CONFIRMADO', 'Nova consulta confirmada',
                                       f'{nome_paciente} pagou a consulta de {quando}.', dados),
        notificacoes_service.notificar(cur, paciente_id, 'PAGAMENTO_RECEBIDO', 'Comprovante de pagamento',
                                       f'Recebemos {valor} por {metodo.lower()} pela consulta de {quando}, '
                                       f'que está confirmada. Comprovante Arkhé nº {id_arkhe}.', dados),
    ]


def financeiro(papel, dias):
    if papel not in ('PSICOLOGO', 'PSIQUIATRA'):
        raise ErroNegocio('Somente profissionais podem ver o financeiro', 403)
    # 365 alimenta o historico mensal da carteira
    if dias not in ('7', '30', '90', '365'):
        raise ErroNegocio('dias deve ser 7, 30, 90 ou 365')

    hoje = date.today()
    periodo = urlencode({
        'data_inicio': (hoje - timedelta(days=int(dias) - 1)).isoformat(),
        'data_fim': hoje.isoformat(),
    })

    # ponytail: credencial global do .env, todos os profissionais veem a mesma conta PJ
    respostas = {}
    for chave, caminho in (
        ('saldo', '/api/v1/saldo'),
        ('conta', '/api/v1/conta'),
        ('resumo', '/api/v1/resumo-financeiro'),
        ('movimentacoes', f'/api/v1/movimentacoes?{periodo}'),
    ):
        status, resposta = _arkhe_request('GET', caminho)
        if status >= 400:
            raise ErroNegocio('Arkhé nao retornou o financeiro', 502, detalhes=resposta)
        respostas[chave] = resposta

    saldo = _numero(respostas['saldo'].get('saldo'))
    resumo = {campo: _numero(respostas['resumo'].get(campo))
              for campo in ('total_receitas', 'total_despesas', 'saldo_periodo')}
    movimentacoes = respostas['movimentacoes'].get('movimentacoes')
    if saldo is None or None in resumo.values() or not isinstance(movimentacoes, list):
        raise ErroNegocio('Arkhé retornou dados financeiros invalidos', 502)

    return {
        'saldo': saldo,
        'conta': _conta(respostas['conta']),
        'resumo': resumo,
        # Um registro malformado nao derruba o dashboard: so fica de fora
        'movimentacoes': [m for m in map(_movimentacao, movimentacoes) if m],
    }


def _conta(conta):
    # So o necessario para exibir; a Arkhé manda numero_conta como inteiro
    nome, agencia, numero = (str(conta.get(campo) or '') for campo in ('nome', 'agencia', 'numero_conta'))
    if not numero:
        return None
    return {'nome': nome, 'agencia': agencia, 'final': numero[-4:]}


def _numero(valor):
    if isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


def _movimentacao(item):
    if not isinstance(item, dict):
        return None
    valor = _numero(item.get('valor'))
    data = item.get('data_movimentacao')
    try:
        # A Arkhé manda microssegundos; nem todo navegador interpreta esse formato
        data = datetime.fromisoformat(str(data)).strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    if valor is None or item.get('tipo') not in ('entrada', 'saida'):
        return None
    return {
        'id_movimentacao': item.get('id_movimentacao'),
        'nome_contraparte': str(item.get('nome_contraparte') or 'Sem identificação'),
        'valor': valor,
        'data_movimentacao': data,
        'tipo': item['tipo'],
        'origem': str(item.get('origem') or ''),
    }
