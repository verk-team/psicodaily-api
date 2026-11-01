import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from database.db import transacao
from funcao import (
	AREAS_ATUACAO,
	ESPECIALIDADES_PROFISSIONAIS,
	IDIOMAS,
	MAXIMO_AREAS_ATUACAO,
	normalizar_abordagem,
	normalizar_ano_inicio,
	normalizar_conselho,
	normalizar_formacao,
	normalizar_lista,
	normalizar_modalidade,
	normalizar_video_url,
	normalizar_texto,
	normalizar_valor_sessao,
	obter_campo,
	obter_dias_atendimento,
)
from services import assinaturas_service, disponibilidade_service, preferencias_service
from services.erros import ErroNegocio
from services.upload_service import upload_service
from services.usuarios_service import criar_usuario_base, enviar_ativacao

logger = logging.getLogger(__name__)

LIMITE_PAGINACAO = 2147483647

COLUNAS_PROFISSIONAL = """
	u.ID_USUARIO
	 , u.NOME
	 , p.DESCRICAO
	 , p.CONSELHO_NUMERO
	 , p.CONSELHO_TIPO
	 , p.PRECO_HORA AS PRECO_CENTAVOS
	 , p.ESPECIALIDADE
	 , p.ABORDAGEM
	 , p.MODALIDADE
	 , p.FORMACAO
	 , p.ANO_INICIO_ATUACAO
	 , p.IDIOMAS
	 , p.AREAS_ATUACAO
	 , p.VIDEO_URL
	 , a.MEDIA AS AVALIACAO
	 , COALESCE(a.TOTAL, 0) AS TOTAL_AVALIACOES
	 , asn.PLANO
"""

# Tabela derivada evita GROUP BY com DESCRICAO (BLOB); o CAST impede o AVG de SMALLINT truncar.
# Sem assinatura valida (9) o profissional nao aparece na busca, no detalhe nem nos recomendados.
ORIGEM_PROFISSIONAL = """
FROM PROFISSIONAL p
INNER JOIN USUARIO u ON u.ID_USUARIO = p.USUARIO_ID
INNER JOIN ASSINATURA asn ON asn.PROFISSIONAL_ID = p.USUARIO_ID AND asn.VALIDA_ATE >= CURRENT_DATE
LEFT JOIN (
	SELECT PROFISSIONAL_ID,
		   AVG(CAST(NOTA AS NUMERIC(5,2))) AS MEDIA,
		   COUNT(*) AS TOTAL
	FROM AVALIACAO GROUP BY PROFISSIONAL_ID
) a ON a.PROFISSIONAL_ID = p.USUARIO_ID
"""

MENSAGEM_VALOR = "Valor em reais deve ser positivo, ate 100000, com no maximo duas casas decimais"
MENSAGEM_DESCRICAO = "Descricao deve ter entre 10 e 1000 caracteres"

# Campos opcionais do perfil: campo da API -> (normalizador, mensagem). A coluna e o nome em maiusculas.
CAMPOS_OPCIONAIS = {
	'abordagem': (normalizar_abordagem, "Abordagem invalida"),
	'modalidade': (normalizar_modalidade, "Modalidade deve ser Online, Presencial ou Ambas"),
	'formacao': (normalizar_formacao, "Formacao deve ter entre 3 e 1000 caracteres"),
	'ano_inicio_atuacao': (normalizar_ano_inicio, "ano_inicio_atuacao deve ser um ano entre 1950 e o atual"),
	'idiomas': (lambda valor: normalizar_lista(valor, IDIOMAS, len(IDIOMAS)),
				"Idiomas validos: " + ", ".join(IDIOMAS)),
	'areas_atuacao': (lambda valor: normalizar_lista(valor, AREAS_ATUACAO, MAXIMO_AREAS_ATUACAO),
					  f"Informe ate {MAXIMO_AREAS_ATUACAO} areas de atuacao entre: " + ", ".join(AREAS_ATUACAO)),
	'video_url': (normalizar_video_url, "video_url deve ser um link de video do YouTube ou do Vimeo"),
}
LISTAS = ('idiomas', 'areas_atuacao')

# ordenar= da busca; o ID desempata para a paginacao ser estavel. Ano de inicio menor = mais experiencia.
ORDENACOES = {
	'preco': "p.PRECO_HORA, u.ID_USUARIO",
	'avaliacao': "a.MEDIA DESC NULLS LAST, u.ID_USUARIO",
	'experiencia': "p.ANO_INICIO_ATUACAO NULLS LAST, u.ID_USUARIO",
}
# Sem ordenar=, o Premium vem primeiro (destaque patrocinado); a ordem escolhida pelo paciente nao muda.
# Rodizio: a fila de Premium que atendem aos filtros anda uma posicao por dia, entao cada um fica em primeiro a cada
# N dias e a ordem nao muda dentro do dia (a paginacao nao repete nem pula ninguem). O resto segue por ID.
ORDEM_PADRAO = """CASE WHEN asn.PLANO = 'PREMIUM' THEN 0 ELSE 1 END,
	CASE WHEN asn.PLANO = 'PREMIUM' THEN MOD(ROW_NUMBER() OVER (PARTITION BY asn.PLANO ORDER BY u.ID_USUARIO) + {dia},
		COUNT(*) OVER (PARTITION BY asn.PLANO)) END,
	u.ID_USUARIO"""


def _dia_do_rodizio():
	return date.today().toordinal()


def _apresentar_perfil(profissional):
	"""Colunas opcionais do banco -> formato da API: listas e anos de experiencia."""
	for campo in LISTAS:
		profissional[campo] = profissional[campo].split(',') if profissional.get(campo) else []
	ano = profissional.get('ano_inicio_atuacao')
	profissional['anos_experiencia'] = date.today().year - ano if ano else None
	return profissional


def serializar_profissional(colunas, linha):
	profissional = dict(zip(colunas, linha))
	avaliacao = profissional['avaliacao']
	profissional['avaliacao'] = round(float(avaliacao), 1) if avaliacao is not None else None
	profissional['foto_url'] = upload_service.url_foto_usuario(profissional['id_usuario'])
	# O plano nao sai na API: so o selo do Premium e o video, que e do Profissional para cima
	plano = profissional.pop('plano')
	profissional['patrocinado'] = plano == 'PREMIUM'
	if not assinaturas_service.libera(plano, 'PROFISSIONAL'):
		profissional['video_url'] = None
	return _apresentar_perfil(profissional)


def _especialidade(valor):
	recebida = normalizar_texto(valor.strip())
	return next((nome for nome in ESPECIALIDADES_PROFISSIONAIS if normalizar_texto(nome) == recebida), None)


def _filtros(args):
	"""Valida a query string da busca e devolve (sql_from_where, params, page, page_size, order_by)."""
	parametros: dict[str, int] = {}
	for nome, padrao, limite in (
		('page_size', 10, 100),
		('page', 1, LIMITE_PAGINACAO),
		('preco_min', None, LIMITE_PAGINACAO),
		('preco_max', None, LIMITE_PAGINACAO),
	):
		valor = args.get(nome, padrao)
		if valor is None:
			continue
		try:
			valor = int(valor)
		except (TypeError, ValueError):
			raise ErroNegocio(f'{nome} deve ser um numero inteiro')
		if valor < 1 or valor > limite:
			raise ErroNegocio(f'{nome} deve estar entre 1 e {limite}')
		parametros[nome] = valor

	page_size = parametros['page_size']
	page = parametros['page']
	preco_min = parametros.get('preco_min')
	preco_max = parametros.get('preco_max')
	if preco_min is not None and preco_max is not None and preco_min > preco_max:
		raise ErroNegocio('preco_min nao pode ser maior que preco_max')

	nome = args.get('nome', '').strip()

	especialidade = None
	if 'especialidade' in args:
		especialidade = _especialidade(args['especialidade'])
		if especialidade is None:
			raise ErroNegocio('Especialidade deve ser Psicologia ou Psiquiatria')

	conselho_padrao = ESPECIALIDADES_PROFISSIONAIS[especialidade]['conselho_tipo'] if especialidade else None
	conselho_recebido = args.get('tipo_conselho')
	conselho = conselho_recebido.strip().upper() if conselho_recebido else conselho_padrao
	if conselho is not None and conselho not in ('CRP', 'CRM'):
		raise ErroNegocio('tipo_conselho deve ser CRP ou CRM')
	if especialidade and conselho != conselho_padrao:
		raise ErroNegocio('tipo_conselho incompativel com a especialidade')

	abordagem = None
	if 'abordagem' in args:
		abordagem = normalizar_abordagem(args['abordagem'])
		if abordagem is None:
			raise ErroNegocio('Abordagem invalida')

	modalidade = None
	if 'modalidade' in args:
		modalidade = normalizar_modalidade(args['modalidade'])
		if modalidade is None:
			raise ErroNegocio('Modalidade deve ser Online ou Presencial')

	avaliacao_min = None
	if 'avaliacao_min' in args:
		try:
			avaliacao_min = float(args['avaliacao_min'])
		except ValueError:
			avaliacao_min = None
		if avaliacao_min is None or not 1 <= avaliacao_min <= 5:
			raise ErroNegocio('avaliacao_min deve ser um numero entre 1 e 5')

	# Filtros de lista: um nome canonico por vez, casado entre virgulas para "Ingles" nao achar parte de outro
	listas = {}
	for nome_arg, coluna, opcoes in (('idioma', 'p.IDIOMAS', IDIOMAS), ('area_atuacao', 'p.AREAS_ATUACAO', AREAS_ATUACAO)):
		if nome_arg in args:
			valor = normalizar_lista(args[nome_arg], opcoes, 1)
			if valor is None:
				raise ErroNegocio(f'{nome_arg} deve ser um entre: ' + ", ".join(opcoes))
			listas[coluna] = valor

	experiencia_min = None
	if 'experiencia_min' in args:
		try:
			experiencia_min = int(args['experiencia_min'])
		except ValueError:
			experiencia_min = None
		if experiencia_min is None or not 0 <= experiencia_min <= 80:
			raise ErroNegocio('experiencia_min deve ser um numero inteiro de anos entre 0 e 80')

	ordenar = args.get('ordenar')
	if ordenar is not None and ordenar not in ORDENACOES:
		raise ErroNegocio('ordenar deve ser preco, avaliacao ou experiencia')

	inicio = fim = None
	if 'disponibilidade_inicio' in args or 'disponibilidade_fim' in args:
		if not args.get('disponibilidade_inicio') or not args.get('disponibilidade_fim'):
			raise ErroNegocio('Informe disponibilidade_inicio e disponibilidade_fim juntos')
		try:
			inicio = datetime.strptime(args['disponibilidade_inicio'], '%Y-%m-%dT%H:%M')
			fim = datetime.strptime(args['disponibilidade_fim'], '%Y-%m-%dT%H:%M')
		except ValueError:
			raise ErroNegocio('Disponibilidade deve usar o formato AAAA-MM-DDTHH:MM, sem fuso horario')
		if fim <= inicio:
			raise ErroNegocio('disponibilidade_fim deve ser posterior a disponibilidade_inicio')

	if (page - 1) * page_size > LIMITE_PAGINACAO:
		raise ErroNegocio(f'offset deve ser no maximo {LIMITE_PAGINACAO}')

	filtros = ORIGEM_PROFISSIONAL + " WHERE u.ATIVO = TRUE"
	params: list[str | int | datetime | Decimal] = []

	if conselho is not None:
		filtros += " AND LOWER(p.CONSELHO_TIPO) = LOWER(?)"
		params.append(conselho)
	if especialidade is not None:
		filtros += " AND LOWER(p.ESPECIALIDADE) = LOWER(?)"
		params.append(especialidade)
	if nome:
		filtros += " AND UPPER(u.NOME) CONTAINING UPPER(?)"
		params.append(nome)
	if abordagem is not None:
		filtros += " AND p.ABORDAGEM = ?"
		params.append(abordagem)
	if modalidade is not None:
		# Quem atende nos dois formatos aparece em qualquer filtro de modalidade.
		filtros += " AND p.MODALIDADE IN (?, 'AMBAS')"
		params.append(modalidade)
	if avaliacao_min is not None:
		# Sem avaliacao a media e NULL e o profissional fica de fora.
		filtros += " AND a.MEDIA >= ?"
		params.append(Decimal(str(avaliacao_min)))
	for coluna, valor in listas.items():
		filtros += f" AND (',' || {coluna} || ',') CONTAINING ?"
		params.append(f",{valor},")
	if experiencia_min is not None:
		# Sem ano de inicio informado o profissional fica de fora.
		filtros += " AND p.ANO_INICIO_ATUACAO <= ?"
		params.append(date.today().year - experiencia_min)
	if preco_min is not None:
		filtros += " AND p.PRECO_HORA >= ?"
		params.append(preco_min)
	if preco_max is not None:
		filtros += " AND p.PRECO_HORA BETWEEN 0 AND ?"
		params.append(preco_max)
	if inicio is not None and fim is not None:
		filtros += """
		AND NOT EXISTS (
			SELECT 1 FROM SESSAO s
			WHERE s.PROFISSIONAL_ID = p.USUARIO_ID
			AND s.STATUS <> 'CANCELADO'
			AND s.DATA_HORA_INICIO < ?
			AND s.DATA_HORA_FIM > ?
		)
		"""
		params.extend([fim, inicio])
		na_agenda, parametros_agenda = disponibilidade_service.filtro_sql('p.USUARIO_ID', inicio, fim)
		filtros += " AND " + na_agenda
		params.extend(parametros_agenda)

	return filtros, params, page, page_size, ORDENACOES.get(ordenar) or ORDEM_PADRAO.format(dia=_dia_do_rodizio())


def buscar(args):
	filtros, params, page, page_size, ordem = _filtros(args)
	with transacao() as cur:
		cur.execute("SELECT FIRST ? SKIP ?" + COLUNAS_PROFISSIONAL + filtros + " ORDER BY " + ordem,
					(page_size, (page - 1) * page_size, *params))
		resultados = cur.fetchall()
		colunas = [desc[0].lower() for desc in cur.description]
		profissionais = [serializar_profissional(colunas, linha) for linha in resultados]

		cur.execute("SELECT COUNT(*) " + filtros, tuple(params))
		total_res = cur.fetchone()
		if not total_res:
			raise RuntimeError("Banco nao retornou a contagem")
		total = total_res[0]

	return {
		'page': page,
		'page_size': page_size,
		'total': total,
		'total_pages': (total + page_size - 1) // page_size,
		'profissionais': profissionais,
	}


def detalhe(id_profissional):
	with transacao() as cur:
		cur.execute("SELECT" + COLUNAS_PROFISSIONAL + ORIGEM_PROFISSIONAL
					+ " WHERE p.USUARIO_ID = ? AND u.ATIVO = TRUE", (id_profissional,))
		resultado = cur.fetchone()
		if resultado is None:
			raise ErroNegocio('Profissional não encontrado', 404)
		profissional = serializar_profissional([desc[0].lower() for desc in cur.description], resultado)
	_contar_visita(id_profissional)
	return profissional


def _contar_visita(id_profissional):
	"""Visualizacoes do perfil (plano Profissional). Melhor esforco, em transacao propria: duas visitas ao mesmo
	tempo podem dar conflito de update no Firebird, e perder uma contagem nao pode derrubar o perfil.

	ponytail: conta tambem a visita do proprio profissional e de quem recarrega a pagina
	"""
	try:
		with transacao() as cur:
			cur.execute('''MERGE INTO VISUALIZACAO_PERFIL v
				USING (SELECT CAST(? AS INTEGER) AS PROFISSIONAL_ID FROM RDB$DATABASE) n
				ON v.PROFISSIONAL_ID = n.PROFISSIONAL_ID AND v.DIA = CURRENT_DATE
				WHEN MATCHED THEN UPDATE SET TOTAL = v.TOTAL + 1
				WHEN NOT MATCHED THEN INSERT (PROFISSIONAL_ID, DIA, TOTAL) VALUES (n.PROFISSIONAL_ID, CURRENT_DATE, 1)''',
						(id_profissional,))
	except Exception:
		logger.warning('Visita ao perfil %s nao contada', id_profissional, exc_info=True)


PERIODOS_DESEMPENHO = ('7', '30', '90')


def _indicadores(cur, profissional_id, inicio, fim):
	"""Numeros do periodo [inicio, fim], datas inclusivas."""
	de = datetime.combine(inicio, datetime.min.time())
	ate = datetime.combine(fim + timedelta(days=1), datetime.min.time())
	cur.execute('''SELECT DIA, TOTAL FROM VISUALIZACAO_PERFIL WHERE PROFISSIONAL_ID = ? AND DIA BETWEEN ? AND ?
		ORDER BY DIA''', (profissional_id, inicio, fim))
	por_dia = [{'dia': dia.isoformat(), 'total': total} for dia, total in cur.fetchall()]
	# Sessoes pela data em que acontecem, como nas metricas do admin
	cur.execute('''SELECT STATUS, COUNT(*) FROM SESSAO WHERE PROFISSIONAL_ID = ?
		AND DATA_HORA_INICIO >= ? AND DATA_HORA_INICIO < ? GROUP BY STATUS''', (profissional_id, de, ate))
	consultas = {**dict.fromkeys(('AGENDADO', 'CONFIRMADO', 'REALIZADO', 'CANCELADO'), 0), **dict(cur.fetchall())}
	cur.execute('SELECT COUNT(*) FROM SESSAO WHERE PROFISSIONAL_ID = ? AND CRIADO_EM >= ? AND CRIADO_EM < ?',
				(profissional_id, de, ate))
	agendamentos = cur.fetchone()[0]
	cur.execute('SELECT COUNT(*) FROM FAVORITO WHERE PROFISSIONAL_ID = ? AND CRIADO_EM >= ? AND CRIADO_EM < ?',
				(profissional_id, de, ate))
	return {'visualizacoes': sum(d['total'] for d in por_dia), 'visualizacoes_por_dia': por_dia,
			'consultas': consultas, 'agendamentos_feitos': agendamentos, 'novos_favoritos': cur.fetchone()[0]}


def desempenho(profissional_id, dias):
	"""Relatorio do plano Profissional; o Premium soma a conversao e o periodo anterior para comparar."""
	if dias not in PERIODOS_DESEMPENHO:
		raise ErroNegocio('dias deve ser 7, 30 ou 90')
	fim = date.today()
	inicio = fim - timedelta(days=int(dias) - 1)
	with transacao() as cur:
		assinaturas_service.exigir(cur, profissional_id, 'PROFISSIONAL')
		relatorio = {'dias': int(dias), 'data_inicio': inicio.isoformat(), **_indicadores(cur, profissional_id, inicio, fim)}
		if not assinaturas_service.libera(assinaturas_service.plano_ativo(cur, profissional_id), 'PREMIUM'):
			return relatorio
		anterior = _indicadores(cur, profissional_id, inicio - timedelta(days=int(dias)), inicio - timedelta(days=1))
	del anterior['visualizacoes_por_dia']
	visualizacoes = relatorio['visualizacoes']
	# Agendamentos feitos por visualizacao do perfil no periodo
	relatorio['conversao'] = round(relatorio['agendamentos_feitos'] / visualizacoes, 4) if visualizacoes else None
	relatorio['periodo_anterior'] = anterior
	return relatorio


def _vazio(valor):
	return valor is None or valor == [] or (isinstance(valor, str) and not valor.strip())


def _valor(formulario, campo):
	# Multipart pode repetir a chave das listas (idiomas=Ingles&idiomas=Libras)
	if campo in LISTAS and hasattr(formulario, 'getlist'):
		valores = formulario.getlist(campo)
		return valores if len(valores) > 1 else valores[0]
	return formulario[campo]


def _opcionais(formulario, limpar_vazios):
	"""{COLUNA: valor normalizado} dos campos opcionais enviados.

	Vazio ou null vira None (limpa a coluna) quando limpar_vazios; senao e ignorado.
	"""
	resultado = {}
	for campo, (normalizar, mensagem) in CAMPOS_OPCIONAIS.items():
		if campo not in formulario:
			continue
		valor = _valor(formulario, campo)
		if _vazio(valor):
			if limpar_vazios:
				resultado[campo.upper()] = None
			continue
		normalizado = normalizar(valor)
		if normalizado is None:
			raise ErroNegocio(mensagem)
		resultado[campo.upper()] = normalizado
	return resultado


def cadastrar(formulario, imagem):
	especialidade = _especialidade(obter_campo(formulario, 'especialidade'))
	if not especialidade:
		raise ErroNegocio("Especialidade deve ser Psicologia ou Psiquiatria")

	configuracao = ESPECIALIDADES_PROFISSIONAIS[especialidade]
	conselho_tipo = configuracao["conselho_tipo"]
	conselho_recebido = obter_campo(formulario, 'crp_crm', 'crp', 'crm', 'conselho', 'conselho_numero')
	if not conselho_recebido:
		raise ErroNegocio("CRP/CRM e obrigatorio")
	conselho_numero = normalizar_conselho(conselho_recebido, conselho_tipo)
	if not conselho_numero:
		raise ErroNegocio(f"{conselho_tipo} invalido")

	descricao = obter_campo(formulario, 'descricao')
	if len(descricao) < 10 or len(descricao) > 1000:
		raise ErroNegocio(MENSAGEM_DESCRICAO)

	valor_recebido = obter_campo(formulario, 'valor', 'valor_sessao', 'valorPorSessao', 'preco', 'preco_hora')
	if not valor_recebido:
		raise ErroNegocio("Valor por sessao e obrigatorio")
	preco_hora = normalizar_valor_sessao(valor_recebido)
	if preco_hora is None:
		raise ErroNegocio(MENSAGEM_VALOR)

	dias_atendimento, erro_dias = obter_dias_atendimento(formulario)
	if erro_dias:
		raise ErroNegocio(erro_dias)

	opcionais = _opcionais(formulario, limpar_vazios=False)

	with transacao() as cur:
		usuario = criar_usuario_base(cur, formulario, imagem, configuracao["usuario_role"], exigir_confirmacao_senha=True)
		colunas = {
			'USUARIO_ID': usuario["id_usuario"],
			'CONSELHO_TIPO': conselho_tipo,
			'CONSELHO_NUMERO': conselho_numero,
			'ESPECIALIDADE': especialidade,
			'PRECO_HORA': preco_hora,
			'DESCRICAO': descricao,
			**opcionais,
		}
		cur.execute(f"INSERT INTO PROFISSIONAL ({', '.join(colunas)}) VALUES ({', '.join('?' * len(colunas))})",
					tuple(colunas.values()))
		disponibilidade_service.gravar(cur, usuario["id_usuario"], disponibilidade_service.janelas_do_cadastro(dias_atendimento))
		assinaturas_service.iniciar_gratis(cur, usuario["id_usuario"])
	enviar_ativacao(usuario)

	return {
		"usuario": {
			"id_usuario": usuario["id_usuario"],
			"tipo_usuario": configuracao["usuario_role"]
		},
		"profissional": _apresentar_perfil({
			"conselho_tipo": conselho_tipo,
			"conselho_numero": conselho_numero,
			"especialidade": especialidade,
			"preco_hora": preco_hora,
			"dias_atendimento": dias_atendimento,
			**{campo: opcionais.get(campo.upper()) for campo in CAMPOS_OPCIONAIS},
		})
	}


def atualizar(id_profissional, formulario):
	permitidos = {'descricao', 'preco_hora', *CAMPOS_OPCIONAIS}
	if not isinstance(formulario, dict) or not formulario or not set(formulario).issubset(permitidos):
		raise ErroNegocio("Campos permitidos: " + ", ".join(sorted(permitidos)))

	atualizacoes = {}
	if 'descricao' in formulario:
		descricao = obter_campo(formulario, 'descricao')
		if len(descricao) < 10 or len(descricao) > 1000:
			raise ErroNegocio(MENSAGEM_DESCRICAO)
		atualizacoes['DESCRICAO'] = descricao
	if 'preco_hora' in formulario:
		preco_hora = normalizar_valor_sessao(obter_campo(formulario, 'preco_hora'))
		if preco_hora is None:
			raise ErroNegocio(MENSAGEM_VALOR)
		atualizacoes['PRECO_HORA'] = preco_hora
	atualizacoes.update(_opcionais(formulario, limpar_vazios=True))

	with transacao() as cur:
		definicoes = ", ".join(f"{coluna} = ?" for coluna in atualizacoes)
		cur.execute(f"UPDATE PROFISSIONAL SET {definicoes} WHERE USUARIO_ID = ?",
					(*atualizacoes.values(), id_profissional))


# Feature 2: pontos por resposta do questionario que o perfil atende; a avaliacao (0 a 5) desempata.
PESOS_RECOMENDACAO = {'motivo': 3, 'abordagem': 3, 'preco': 2, 'modalidade': 2, 'idioma': 1}
MAXIMO_RECOMENDADOS = 10


def _pontuar(profissional, preferencias):
	motivos = [m for m in preferencias.get('motivos', []) if m in profissional['areas_atuacao']]
	pontos = PESOS_RECOMENDACAO['motivo'] * len(motivos)
	if preferencias.get('abordagem') and preferencias['abordagem'] == profissional['abordagem']:
		pontos += PESOS_RECOMENDACAO['abordagem']
	if preferencias.get('preco_max') and profissional['preco_centavos'] <= preferencias['preco_max']:
		pontos += PESOS_RECOMENDACAO['preco']
	modalidade = preferencias.get('modalidade')
	# Como na busca: quem atende nos dois formatos serve para qualquer preferencia
	if modalidade and (modalidade == profissional['modalidade'] or 'AMBAS' in (modalidade, profissional['modalidade'])):
		pontos += PESOS_RECOMENDACAO['modalidade']
	if preferencias.get('idioma') in profissional['idiomas']:
		pontos += PESOS_RECOMENDACAO['idioma']
	return pontos + (profissional['avaliacao'] or 0) / 5


def recomendados(paciente_id, papel):
	preferencias = preferencias_service.obter(paciente_id, papel)['preferencias']
	with transacao() as cur:
		# ponytail: pontua todos os ativos em Python; filtro SQL antes se o cadastro passar de alguns milhares
		cur.execute("SELECT" + COLUNAS_PROFISSIONAL + ORIGEM_PROFISSIONAL + " WHERE u.ATIVO = TRUE")
		colunas = [desc[0].lower() for desc in cur.description]
		profissionais = [serializar_profissional(colunas, linha) for linha in cur.fetchall()]
	for profissional in profissionais:
		profissional['pontuacao'] = round(_pontuar(profissional, preferencias or {}), 2)
	profissionais.sort(key=lambda p: (-p['pontuacao'], p['id_usuario']))
	return {'questionario_respondido': preferencias is not None, 'profissionais': profissionais[:MAXIMO_RECOMENDADOS]}
