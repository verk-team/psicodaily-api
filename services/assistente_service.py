import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from time import sleep
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from funcao import formatar_reais
from services import consultas_service, disponibilidade_service, profissionais_service, whatsapp_service
from services.erros import ErroNegocio

logger = logging.getLogger(__name__)

# Gemini no plano gratuito, pelo endpoint no formato da OpenAI (trocado do Groq em 22/10/2026, que era lento).
# Outro provedor compativel = trocar URL, modelo e a variavel da chave.
URL_LLM = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
# Flash-lite: o 3.5 Flash gratis so da 20 requisicoes por dia (03/11/2026), e cada pergunta gasta de 1 a 3;
# o 3.8 nao aceita 'minimal' e vivia sobrecarregado (503)
MODELO = 'gemini-3.5-flash-lite'
MAXIMO_MENSAGENS = 40
MAXIMO_CARACTERES = 2000
MAXIMO_RODADAS = 8
MAXIMO_HORARIOS = 20
# No plano gratuito o Gemini devolve 503 (sobrecarga) e 429 (limite por minuto) com frequencia, e costuma passar
# em segundos. Timeout nao entra: ja esperou 30 s.
ERROS_PASSAGEIROS = {429, 500, 503, 504}
INDISPONIVEL = 'O assistente está indisponível no momento. Tente novamente em instantes.'
ESPERAS_NOVA_TENTATIVA = (2, 5)

SISTEMA = '''Voce e o assistente virtual do PsicoDaily chamado Psicosinho (sempre comece se apresentando caso o user cumprimente), plataforma que conecta pacientes a psicologos e psiquiatras.
Fale em portugues do Brasil, com tom profissional, cordial e objetivo, como o atendimento de uma clinica:
- trate o paciente por "voce", sem girias, sem emojis e sem exclamacoes em excesso;
- responda em ate 4 frases curtas ou numa lista; com varios profissionais ou horarios, use uma lista com um item por linha;
- seja acolhedor sem ser intimo; nao faca promessas sobre o tratamento;
- termine, quando fizer sentido, com o proximo passo concreto (ex.: "Quer que eu verifique os horarios dela?").

Voce ajuda o paciente a: entender como a plataforma funciona, encontrar profissionais, ver horarios livres e montar um agendamento.
As ferramentas usam precos em centavos; para o paciente, mostre sempre em reais (15000 = R$ 150,00).
Use as ferramentas para qualquer dado de profissionais, horarios ou consultas; nunca invente nomes, precos ou horarios.
Chame juntas, na mesma rodada, as ferramentas que nao dependem uma da outra.
Para agendar, chame propor_agendamento; para cancelar, chame propor_cancelamento com o sessao_id de minhas_consultas. O paciente confirma depois (botao no site ou SIM no WhatsApp): nunca diga que a consulta ja foi marcada ou cancelada.

Como a plataforma funciona:
- Questionario inicial (Perfil > Preferencias): melhora as recomendacoes de profissionais.
- Busca: filtros por especialidade, abordagem, modalidade, preco, idioma, area de atuacao, avaliacao, experiencia e disponibilidade.
- Favoritos: o paciente salva profissionais e ve o proximo horario livre de cada um.
- Lista de espera: se nao houver horario, o paciente entra na lista do profissional e e avisado quando uma vaga abrir (a vaga nao fica reservada).
- Pagamento: na tela da consulta, por PIX, boleto ou cartao (credito ou debito), todos pelo banco Arkhé. O boleto vai para o CPF do paciente, que precisa ter conta na Arkhé, e vence no dia da consulta; o cartao precisa ser emitido pela Arkhé. A consulta so fica marcada com o pagamento: ao escolher o horario, ele fica reservado por 30 minutos e, sem pagamento, a reserva e cancelada. Pago, passa de AGENDADO para CONFIRMADO (no cartao, na hora).
- Cancelamento: o paciente cancela pela tela da consulta ate 15 minutos antes do horario, com reembolso integral do que pagou; depois disso nao da mais para cancelar. Se o profissional cancelar, o reembolso tambem e integral. O reembolso e devolvido pela equipe da plataforma.
- Reagendamento: o paciente cancela e marca outro horario, ou pede ao profissional pelo chat.
- Chat: liberado entre paciente e profissional depois que existe uma consulta; aceita texto, PDF, imagens e audio ate 10 MB.
- Videochamada: pelo botao de entrar na sala da consulta paga, a partir de 15 minutos antes do horario.
- Avaliacao: depois da consulta, o paciente da uma nota de 1 a 5 e pode deixar um comentario publico (anonimo, no perfil do profissional) ou privado (so o profissional le).

Limites:
- Voce nao faz atendimento psicologico, diagnostico nem indica medicacao. Acolha e sugira um profissional.
- Se a pessoa falar em se machucar, suicidio ou risco imediato, oriente a ligar 188 (CVV, 24h) ou 192 (SAMU) agora, antes de qualquer outra coisa.
- Assuntos fora da plataforma e da saude mental: diga gentilmente que so ajuda com o PsicoDaily.'''

FERRAMENTAS = [
    {
        'name': 'buscar_profissionais',
        'description': 'Busca profissionais ativos por filtros. Todos opcionais; precos em centavos. Devolve ate 5.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'nome': {'type': 'string'},
                'especialidade': {'type': 'string', 'enum': ['Psicologia', 'Psiquiatria']},
                'abordagem': {'type': 'string', 'description': 'Ex.: TCC, Psicanalise, Humanista'},
                'modalidade': {'type': 'string', 'enum': ['Online', 'Presencial']},
                'preco_max': {'type': 'integer', 'description': 'Centavos, ex.: 15000 = R$ 150'},
                'idioma': {'type': 'string'},
                'area_atuacao': {'type': 'string', 'description': 'Ex.: Ansiedade, Depressao'},
                'avaliacao_min': {'type': 'number'},
                'ordenar': {'type': 'string', 'enum': ['preco', 'avaliacao', 'experiencia']},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'recomendados',
        'description': 'Profissionais mais adequados ao questionario inicial do paciente.',
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'horarios_livres',
        'description': 'Horarios livres de um profissional. Datas AAAA-MM-DD; sem datas, os proximos 14 dias.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'id_profissional': {'type': 'integer'},
                'de': {'type': 'string'},
                'ate': {'type': 'string'},
            },
            'required': ['id_profissional'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'minhas_consultas',
        'description': 'Consultas do paciente (mais recentes primeiro), com status e valor.',
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'propor_agendamento',
        'description': ('Mostra ao paciente um botao para confirmar a consulta. Se o paciente ja disse dia e hora, chame direto, '
                        'sem horarios_livres (fim = inicio + 1h); se o horario nao estiver livre, o erro traz os livres do dia.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'id_profissional': {'type': 'integer'},
                'inicio': {'type': 'string', 'description': 'AAAA-MM-DDTHH:MM'},
                'fim': {'type': 'string', 'description': 'AAAA-MM-DDTHH:MM'},
            },
            'required': ['id_profissional', 'inicio', 'fim'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'propor_cancelamento',
        'description': 'Pede ao paciente a confirmacao para cancelar uma consulta dele (ate 15 min antes, reembolso integral).',
        'input_schema': {
            'type': 'object',
            'properties': {'sessao_id': {'type': 'integer'}},
            'required': ['sessao_id'],
            'additionalProperties': False,
        },
    },
]
# Ferramentas que so propoem: a acao fica para o paciente confirmar, e a proposta volta na resposta
PROPOSTAS = {'propor_agendamento': 'agendamento', 'propor_cancelamento': 'cancelamento'}

FERRAMENTAS_API = [{'type': 'function', 'function': {'name': f['name'], 'description': f['description'],
                                                     'parameters': f['input_schema']}} for f in FERRAMENTAS]

CAMPOS_PROFISSIONAL = ('id_usuario', 'nome', 'especialidade', 'abordagem', 'modalidade', 'preco_centavos',
                       'avaliacao', 'total_avaliacoes', 'areas_atuacao', 'idiomas', 'anos_experiencia')
CAMPOS_CONSULTA = ('sessao_id', 'profissional_nome', 'data_hora_inicio', 'data_hora_fim', 'status', 'valor_centavos')


def _chamar(mensagens):
    """Uma ida ao modelo; devolve a mensagem do assistente ({content, tool_calls})."""
    # low: quase tao rapido quanto minimal e escolhe melhor a ferramenta
    corpo = json.dumps({'model': MODELO, 'messages': mensagens, 'tools': FERRAMENTAS_API,
                        'reasoning_effort': 'low'}).encode()
    req = Request(URL_LLM, data=corpo, method='POST', headers={
        'Authorization': f'Bearer {os.getenv("GEMINI_API_KEY", "")}',
        'Content-Type': 'application/json',
        'User-Agent': 'psicodaily-api',
    })
    for espera in (*ESPERAS_NOVA_TENTATIVA, None):
        try:
            with urlopen(req, timeout=30) as resposta:
                return json.load(resposta)['choices'][0]['message']
        except HTTPError as erro:
            # O corpo diz qual limite estourou (por minuto ou por dia); sem isso nao da para escolher a correcao
            logger.warning('Gemini respondeu %s: %s', erro.code, erro.read(500).decode(errors='replace'))
            if erro.code == 429 and espera is None:
                raise ErroNegocio('O assistente está com muitas solicitações no momento. Tente novamente em alguns minutos.', 503)
            if erro.code not in ERROS_PASSAGEIROS or espera is None:
                raise ErroNegocio(INDISPONIVEL, 503)
        except OSError:
            # URLError e timeout de leitura
            logger.warning('Gemini sem resposta', exc_info=True)
            raise ErroNegocio(INDISPONIVEL, 503)
        sleep(espera)


def _resumo(itens, campos):
    return [{campo: item.get(campo) for campo in campos} for item in itens]


def _propor(id_profissional, inicio, fim):
    inicio_dt, _ = consultas_service.ler_intervalo(inicio, fim)
    dia = inicio_dt.date().isoformat()
    livres = disponibilidade_service.horarios_livres(id_profissional, dia, dia)['horarios']
    # Vale o horario exato que a agenda devolveu; o POST /api/consultas revalida tudo na confirmacao.
    if not any(h['inicio'] == inicio[:16] and h['fim'] == fim[:16] for h in livres):
        # Os livres vao junto para o modelo corrigir sem gastar outra rodada com horarios_livres
        raise ErroNegocio(f'Horario nao esta livre. Livres neste dia: {json.dumps(livres[:MAXIMO_HORARIOS])}')
    return {'id_profissional': id_profissional, 'timestamp_inicio': inicio[:16], 'timestamp_fim': fim[:16]}


def _quando(texto_iso):
    momento = datetime.fromisoformat(texto_iso)
    return f'{momento:%d/%m} às {momento:%H:%M}'


def _texto_da_proposta(tipo, proposta):
    if tipo == 'agendamento':
        return f'O horário de {_quando(proposta["timestamp_inicio"])} está livre.'
    reembolso = ', com reembolso integral do que você pagou' if proposta['reembolso_integral'] else ''
    return f'Posso cancelar sua consulta com {proposta["profissional_nome"]} de {_quando(proposta["inicio"])}{reembolso}.'


def executar_ferramenta(nome, entrada, paciente_id):
    """Roda uma ferramenta pedida pelo modelo. O paciente vem sempre do token, nunca da entrada."""
    if nome == 'buscar_profissionais':
        filtros = {campo: str(valor) for campo, valor in entrada.items()}
        # A ordem padrao da busca poe o Premium primeiro; o assistente indica sem considerar patrocinio
        pedido = {'ordenar': 'avaliacao', **filtros, 'page_size': 5}
        return _resumo(profissionais_service.buscar(pedido)['profissionais'], CAMPOS_PROFISSIONAL)
    if nome == 'recomendados':
        resultado = profissionais_service.recomendados(paciente_id, 'PACIENTE')
        return {'questionario_respondido': resultado['questionario_respondido'],
                'profissionais': _resumo(resultado['profissionais'][:5], CAMPOS_PROFISSIONAL)}
    if nome == 'horarios_livres':
        resultado = disponibilidade_service.horarios_livres(entrada['id_profissional'], entrada.get('de'), entrada.get('ate'))
        return {**resultado, 'horarios': resultado['horarios'][:MAXIMO_HORARIOS]}
    if nome == 'minhas_consultas':
        return _resumo(consultas_service.listar(paciente_id, 'PACIENTE')[:10], CAMPOS_CONSULTA)
    if nome == 'propor_agendamento':
        return _propor(entrada['id_profissional'], entrada['inicio'], entrada['fim'])
    if nome == 'propor_cancelamento':
        return consultas_service.checar_cancelamento(paciente_id, entrada['sessao_id'])
    raise ErroNegocio(f'Ferramenta desconhecida: {nome}')


def _validar(dados):
    if not isinstance(dados, dict) or set(dados) != {'mensagens'}:
        raise ErroNegocio('Envie {"mensagens": [{"role": "user", "content": "..."}]}')
    mensagens = dados['mensagens']
    if not isinstance(mensagens, list) or not 1 <= len(mensagens) <= MAXIMO_MENSAGENS:
        raise ErroNegocio(f'mensagens deve ser uma lista de 1 a {MAXIMO_MENSAGENS} itens')
    for mensagem in mensagens:
        if (not isinstance(mensagem, dict) or set(mensagem) != {'role', 'content'}
                or mensagem['role'] not in ('user', 'assistant')
                or not isinstance(mensagem['content'], str)
                or not 1 <= len(mensagem['content'].strip()) <= MAXIMO_CARACTERES):
            raise ErroNegocio(f'Cada mensagem tem role (user ou assistant) e content de 1 a {MAXIMO_CARACTERES} caracteres')
    if mensagens[0]['role'] != 'user' or mensagens[-1]['role'] != 'user':
        raise ErroNegocio('A conversa comeca e termina com mensagem do paciente')
    return [dict(m) for m in mensagens]


def conversar(paciente_id, papel, dados):
    if papel != 'PACIENTE':
        raise ErroNegocio('Assistente disponivel apenas para pacientes', 403)
    sistema = f'{SISTEMA}\n\nHoje: {date.today().isoformat()}'
    mensagens = [{'role': 'system', 'content': sistema}, *_validar(dados)]

    propostas = dict.fromkeys(PROPOSTAS.values())
    for _ in range(MAXIMO_RODADAS):
        resposta = _chamar(mensagens)
        chamadas = resposta.get('tool_calls') or []
        if not chamadas:
            return {'resposta': resposta.get('content') or '', **propostas}

        # So os campos do protocolo voltam; campos extras do provedor (ex.: reasoning) podem dar 400. Os tool_calls
        # voltam intactos: no Gemini 3 eles levam a thought signature (extra_content), exigida na rodada seguinte.
        mensagens.append({'role': 'assistant', 'content': resposta.get('content') or '', 'tool_calls': chamadas})
        for chamada in chamadas:
            nome = chamada['function']['name']
            try:
                saida = executar_ferramenta(nome, json.loads(chamada['function']['arguments'] or '{}'), paciente_id)
                if nome in PROPOSTAS:
                    propostas[PROPOSTAS[nome]] = saida
                conteudo = json.dumps(saida, ensure_ascii=False, default=str)
            except ErroNegocio as erro:
                conteudo = f'Erro: {erro.mensagem}'
            except (ValueError, KeyError, TypeError):
                # Argumentos fora do schema: o modelo ve o erro e tenta de novo
                conteudo = 'Erro: argumentos invalidos para esta ferramenta'
            mensagens.append({'role': 'tool', 'tool_call_id': chamada['id'], 'name': nome, 'content': conteudo})

        # Com a proposta pronta, a resposta e fixa: o site e o WhatsApp ja mostram a confirmacao, e o modelo nao gasta
        # mais uma chamada do limite por minuto so para dizer "confirme abaixo"
        textos = [_texto_da_proposta(tipo, proposta) for tipo, proposta in propostas.items() if proposta]
        if textos:
            return {'resposta': ' '.join(textos), **propostas}

    raise ErroNegocio('O assistente nao concluiu a resposta, tente reformular a pergunta', 502)


# WhatsApp (12 e 31): o mesmo assistente, com historico por numero em memoria (processo unico, como o chat).
# ponytail: o historico some no restart do servidor; tabela se precisar guardar conversas
TTL_CONVERSA = timedelta(hours=2)
MAXIMO_HISTORICO = 20
SIM = {'sim', 's', 'confirmo', 'confirmar', 'ok', 'pode'}
NAO = {'nao', 'não', 'n'}
_conversas = {}
_conversas_lock = threading.Lock()


def receber_whatsapp(evento):
    """Evento do webhook do WAHA: responde em segundo plano mensagem de texto de pessoa (nao de grupo nem nossa)."""
    payload = evento.get('payload') if isinstance(evento, dict) else None
    if evento.get('event') != 'message' or not isinstance(payload, dict) or payload.get('fromMe'):
        return False
    remetente, texto = payload.get('from'), payload.get('body')
    if (not isinstance(remetente, str) or not isinstance(texto, str) or not texto.strip()
            or remetente.endswith(('@g.us', '@broadcast', '@newsletter'))):
        return False
    threading.Thread(target=responder_whatsapp, args=(remetente, texto), daemon=True).start()
    return True


def responder_whatsapp(remetente, texto):
    telefone = whatsapp_service.telefone_do_remetente(remetente)
    usuario = whatsapp_service.usuario_por_telefone(telefone) if telefone else None
    try:
        resposta = _responder(usuario, remetente, texto.strip()[:MAXIMO_CARACTERES])
    except ErroNegocio as erro:
        resposta = erro.mensagem
    except Exception:
        logger.exception('Falha ao responder o WhatsApp de %s', remetente)
        resposta = 'Tive um problema para responder agora. Tente de novo em instantes.'
    whatsapp_service.enviar_async(remetente, resposta)


def _responder(usuario, chave, texto):
    if usuario is None:
        site = os.getenv('FRONT_URL', 'https://psicodaily-front.vercel.app')
        return f'Não encontrei este número no PsicoDaily. Cadastre-se em {site} com este telefone para conversar comigo por aqui.'
    usuario_id, papel, _ = usuario
    if papel != 'PACIENTE':
        return 'Por aqui eu atendo pacientes. Os avisos das suas consultas continuam chegando neste número.'

    with _conversas_lock:
        conversa = _conversas.get(chave)
        if conversa is None or conversa['expira'] < datetime.now():
            conversa = {'mensagens': [], 'pendente': None}
        conversa['expira'] = datetime.now() + TTL_CONVERSA
        _conversas[chave] = conversa
        pendente, conversa['pendente'] = conversa['pendente'], None

    curta = texto.lower().strip(' .!')
    if pendente and curta in SIM:
        return _executar(usuario_id, *pendente)
    if pendente and curta in NAO:
        return 'Tudo bem, não fiz nada.'

    historico = (conversa['mensagens'] + [{'role': 'user', 'content': texto}])[-MAXIMO_HISTORICO:]
    while historico[0]['role'] != 'user':
        historico.pop(0)
    resultado = conversar(usuario_id, 'PACIENTE', {'mensagens': historico})
    resposta = resultado['resposta'] or 'Pode repetir, por favor?'
    for tipo, acao in (('agendamento', 'o agendamento'), ('cancelamento', 'o cancelamento')):
        if resultado[tipo]:
            conversa['pendente'] = (tipo, resultado[tipo])
            resposta += f'\n\nResponda *SIM* para confirmar {acao}.'
            break
    conversa['mensagens'] = historico + [{'role': 'assistant', 'content': resposta[:MAXIMO_CARACTERES]}]
    return resposta


def _executar(paciente_id, tipo, dados):
    """O SIM do paciente: agenda ou cancela pelas mesmas regras das rotas do site."""
    if tipo == 'agendamento':
        consultas_service.agendar(paciente_id, 'PACIENTE', dados)
        return (f'Horário reservado por {consultas_service.MINUTOS_RESERVA} minutos. A consulta só é confirmada com o pagamento, '
                'feito na tela da consulta no site.')
    reembolso = consultas_service.cancelar(paciente_id, 'PACIENTE', dados['sessao_id'])['reembolso_centavos']
    if reembolso:
        return f'Consulta cancelada. Vamos devolver {formatar_reais(reembolso)} integralmente.'
    return 'Consulta cancelada.'
