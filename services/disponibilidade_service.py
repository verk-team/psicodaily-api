"""Agenda do profissional: janelas semanais, bloqueios de dias e o cálculo dos horários livres.

- Janela: um horário fixo da semana em que o profissional atende (ex.: segunda, das 08:00 às 12:00).
  Tabela DISPONIBILIDADE.
- Bloqueio: um período de dias em que ele não atende (férias, folga). Tabela BLOQUEIO_AGENDA.
- Horário livre: um pedaço de uma janela, fora de bloqueio, sem consulta marcada e no futuro.
"""
from datetime import date, datetime, time, timedelta

from database.db import transacao
from services.erros import ErroNegocio

# 0 = domingo ... 6 = sábado, como EXTRACT(WEEKDAY) do Firebird e getDay() do JavaScript
NOMES_DIAS = {'DOMINGO': 0, 'SEGUNDA': 1, 'TERCA': 2, 'QUARTA': 3, 'QUINTA': 4, 'SEXTA': 5, 'SABADO': 6}
# Janela padrão para os dias marcados no cadastro, até o profissional ajustar a agenda
PADRAO_CADASTRO = (time(8), time(18))
MAXIMO_JANELAS = 50
MAXIMO_DIAS_CONSULTA = 31
DURACAO_PADRAO = 60
MAXIMO_DIAS_BLOQUEIO = 365


def dia_semana(momento):
    """Dia da semana com domingo = 0. O weekday() do Python começa na segunda (segunda = 0), por isso a conta."""
    return (momento.weekday() + 1) % 7


def _hora(texto):
    """'08:30' -> time(8, 30). Exige exatamente HH:MM."""
    try:
        if not isinstance(texto, str) or len(texto) != 5:
            raise ValueError()
        return time.fromisoformat(texto)
    except ValueError:
        raise ErroNegocio('Horarios no formato HH:MM')


def _serializar(linha):
    dia, inicio, fim = linha
    return {'dia_semana': dia, 'inicio': inicio.strftime('%H:%M'), 'fim': fim.strftime('%H:%M')}


def _janelas(cur, profissional_id):
    """[(dia_semana, hora_inicio, hora_fim), ...] ordenadas por dia e hora."""
    cur.execute('''SELECT DIA_SEMANA, HORA_INICIO, HORA_FIM FROM DISPONIBILIDADE
        WHERE PROFISSIONAL_ID = ? ORDER BY DIA_SEMANA, HORA_INICIO''', (profissional_id,))
    return cur.fetchall()


def _profissional_ativo(cur, profissional_id):
    cur.execute('''SELECT 1 FROM PROFISSIONAL p JOIN USUARIO u ON u.ID_USUARIO = p.USUARIO_ID
        WHERE p.USUARIO_ID = ? AND u.ATIVO = TRUE''', (profissional_id,))
    if cur.fetchone() is None:
        raise ErroNegocio('Profissional não encontrado', 404)


def fora_de_bloqueio(coluna_profissional, inicio, fim):
    """Condicao SQL: nenhum dia do intervalo cai num bloqueio (ferias, folga) do profissional.

    Devolve (trecho de SQL, parâmetros). `coluna_profissional` é a coluna com o id do profissional na consulta
    de quem chama (ex.: 'p.USUARIO_ID').
    """
    # O fim é exclusivo: uma sessão que termina à meia-noite não encosta no dia seguinte
    ultimo_dia = (fim - timedelta(microseconds=1)).date()
    # Dois períodos se cruzam quando cada um começa antes do outro terminar
    sql = f'''NOT EXISTS (SELECT 1 FROM BLOQUEIO_AGENDA b WHERE b.PROFISSIONAL_ID = {coluna_profissional}
        AND b.DATA_INICIO <= ? AND b.DATA_FIM >= ?)'''
    return sql, [ultimo_dia, inicio.date()]


def filtro_sql(coluna_profissional, inicio, fim):
    """Condicao SQL: o intervalo cabe numa janela do profissional e nao cai num bloqueio. Quem ainda nao
    cadastrou nenhuma janela aceita qualquer horario (como era antes de existir a agenda), mas o bloqueio vale igual.

    Usada no agendamento e no filtro de disponibilidade da busca. Devolve (trecho de SQL, parâmetros).
    """
    # ponytail: uma sessão não atravessa a meia-noite; dia diferente nunca casa com janela
    mesmo_dia = inicio.date() == fim.date()
    sem_bloqueio, parametros_bloqueio = fora_de_bloqueio(coluna_profissional, inicio, fim)
    sql = f'''((NOT EXISTS (SELECT 1 FROM DISPONIBILIDADE d WHERE d.PROFISSIONAL_ID = {coluna_profissional})
        OR EXISTS (SELECT 1 FROM DISPONIBILIDADE d WHERE d.PROFISSIONAL_ID = {coluna_profissional}
            AND d.DIA_SEMANA = ? AND d.HORA_INICIO <= ? AND d.HORA_FIM >= ?))
        AND {sem_bloqueio})'''
    # Intervalo que vira o dia: dia_semana -1 não existe, então nenhuma janela casa
    dia = dia_semana(inicio) if mesmo_dia else -1
    hora_fim = fim.time() if mesmo_dia else inicio.time()
    return sql, [dia, inicio.time(), hora_fim, *parametros_bloqueio]


def listar(profissional_id):
    """As janelas da agenda semanal do profissional."""
    with transacao() as cur:
        _profissional_ativo(cur, profissional_id)
        return [_serializar(linha) for linha in _janelas(cur, profissional_id)]


def validar(janelas):
    """Confere a lista de janelas enviada e devolve [(dia, inicio, fim), ...] ordenada."""
    if not isinstance(janelas, list) or len(janelas) > MAXIMO_JANELAS:
        raise ErroNegocio(f'Envie janelas como lista de ate {MAXIMO_JANELAS} itens')
    limpas = []
    for janela in janelas:
        if not isinstance(janela, dict) or set(janela) != {'dia_semana', 'inicio', 'fim'}:
            raise ErroNegocio('Cada janela tem exatamente dia_semana, inicio e fim')
        dia = janela['dia_semana']
        if type(dia) is not int or not 0 <= dia <= 6:
            raise ErroNegocio('dia_semana deve ser inteiro de 0 (domingo) a 6 (sabado)')
        inicio, fim = _hora(janela['inicio']), _hora(janela['fim'])
        if fim <= inicio:
            raise ErroNegocio('fim deve ser posterior ao inicio')
        limpas.append((dia, inicio, fim))
    limpas.sort()
    # Depois de ordenar, basta comparar cada janela com a anterior: no mesmo dia, ela não pode começar
    # antes da anterior terminar
    for (dia_anterior, _, fim_anterior), (dia, inicio, _) in zip(limpas, limpas[1:]):
        if dia_anterior == dia and inicio < fim_anterior:
            raise ErroNegocio('Janelas do mesmo dia nao podem se sobrepor')
    return limpas


def gravar(cur, profissional_id, janelas):
    """Troca todas as janelas do profissional pelas novas (apaga e insere de novo)."""
    cur.execute('DELETE FROM DISPONIBILIDADE WHERE PROFISSIONAL_ID = ?', (profissional_id,))
    cur.executemany('''INSERT INTO DISPONIBILIDADE (PROFISSIONAL_ID, DIA_SEMANA, HORA_INICIO, HORA_FIM)
        VALUES (?, ?, ?, ?)''', [(profissional_id, *janela) for janela in janelas])


def substituir(profissional_id, dados):
    if not isinstance(dados, dict) or set(dados) != {'janelas'}:
        raise ErroNegocio('Envie {"janelas": [...]}')
    janelas = validar(dados['janelas'])
    with transacao() as cur:
        gravar(cur, profissional_id, janelas)
    return [_serializar(janela) for janela in janelas]


def janelas_do_cadastro(dias):
    """Dias marcados no cadastro ('Segunda', 'Terca'...) viram janelas no horario padrao."""
    return [(NOMES_DIAS[dia.upper()], *PADRAO_CADASTRO) for dia in dias]


def _ler_dia(texto, padrao):
    if texto is None:
        return padrao
    try:
        return date.fromisoformat(texto)
    except (TypeError, ValueError):
        raise ErroNegocio('Datas no formato AAAA-MM-DD')


def horarios_livres(profissional_id, de=None, ate=None, duracao=None):
    """Horarios de `duracao` minutos dentro das janelas, fora dos bloqueios, sem sessao marcada e no futuro.

    Sem datas, olha os próximos 14 dias (hoje + 13).
    """
    de = _ler_dia(de, date.today())
    ate = _ler_dia(ate, de + timedelta(days=13))
    if ate < de or (ate - de).days >= MAXIMO_DIAS_CONSULTA:
        raise ErroNegocio(f'Periodo de 1 a {MAXIMO_DIAS_CONSULTA} dias, com ate depois de de')
    try:
        duracao = DURACAO_PADRAO if duracao is None else int(duracao)
    except ValueError:
        duracao = 0
    if not 30 <= duracao <= 120:
        raise ErroNegocio('duracao deve ser de 30 a 120 minutos')

    with transacao() as cur:
        _profissional_ativo(cur, profissional_id)
        janelas = _janelas(cur, profissional_id)
        # Consultas não canceladas que encostam no período (da meia-noite de `de` até o fim do dia `ate`)
        cur.execute('''SELECT DATA_HORA_INICIO, DATA_HORA_FIM FROM SESSAO
            WHERE PROFISSIONAL_ID = ? AND STATUS <> 'CANCELADO'
              AND DATA_HORA_INICIO < ? AND DATA_HORA_FIM > ?''',
                    (profissional_id, datetime.combine(ate + timedelta(days=1), time()), datetime.combine(de, time())))
        ocupados = cur.fetchall()
        cur.execute('''SELECT DATA_INICIO, DATA_FIM FROM BLOQUEIO_AGENDA
            WHERE PROFISSIONAL_ID = ? AND DATA_INICIO <= ? AND DATA_FIM >= ?''', (profissional_id, ate, de))
        bloqueios = cur.fetchall()

    agora = datetime.now()
    passo = timedelta(minutes=duracao)
    livres = []
    dia = de
    while dia <= ate:
        bloqueado = any(inicio_bloqueio <= dia <= fim_bloqueio for inicio_bloqueio, fim_bloqueio in bloqueios)
        if not bloqueado:
            livres += _livres_no_dia(dia, janelas, passo, agora, ocupados)
        dia += timedelta(days=1)
    return {'duracao': duracao, 'de': de.isoformat(), 'ate': ate.isoformat(), 'horarios': livres}


def _livres_no_dia(dia, janelas, passo, agora, ocupados):
    """Corta as janelas daquele dia da semana em pedaços de `passo` e fica com os livres e futuros."""
    livres = []
    for semana, hora_inicio, hora_fim in janelas:
        if semana != dia_semana(dia):
            continue
        inicio, limite = datetime.combine(dia, hora_inicio), datetime.combine(dia, hora_fim)
        while inicio + passo <= limite:
            fim = inicio + passo
            # Livre se nenhuma consulta marcada se cruza com [inicio, fim)
            conflita = any(ocupado_inicio < fim and ocupado_fim > inicio for ocupado_inicio, ocupado_fim in ocupados)
            if inicio >= agora and not conflita:
                livres.append({'inicio': inicio.isoformat(timespec='minutes'), 'fim': fim.isoformat(timespec='minutes')})
            inicio = fim
    return livres


def _ler_dia_bloqueio(texto):
    # Exige os 10 caracteres: strptime aceitaria 2026-1-5
    try:
        if len(texto) != 10:
            raise ValueError()
        return datetime.strptime(texto, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ErroNegocio('inicio e fim no formato AAAA-MM-DD')


def _serializar_bloqueio(bloqueio_id, inicio, fim):
    return {'bloqueio_id': bloqueio_id, 'inicio': inicio.isoformat(), 'fim': fim.isoformat()}


def listar_bloqueios(profissional_id):
    """Os que ainda nao terminaram, do mais proximo para o mais distante."""
    with transacao() as cur:
        cur.execute('''SELECT BLOQUEIO_ID, DATA_INICIO, DATA_FIM FROM BLOQUEIO_AGENDA
            WHERE PROFISSIONAL_ID = ? AND DATA_FIM >= CURRENT_DATE ORDER BY DATA_INICIO''', (profissional_id,))
        return [_serializar_bloqueio(*linha) for linha in cur.fetchall()]


def criar_bloqueio(profissional_id, dados):
    """Nao cancela o que ja esta marcado: devolve quantas sessoes caem no periodo para o front avisar."""
    if not isinstance(dados, dict) or set(dados) != {'inicio', 'fim'}:
        raise ErroNegocio('Envie exatamente inicio e fim')
    inicio, fim = _ler_dia_bloqueio(dados['inicio']), _ler_dia_bloqueio(dados['fim'])
    if fim < inicio or fim < date.today():
        raise ErroNegocio('fim nao pode ser anterior ao inicio nem estar no passado')
    if (fim - inicio).days + 1 > MAXIMO_DIAS_BLOQUEIO:
        raise ErroNegocio(f'O bloqueio pode ter no maximo {MAXIMO_DIAS_BLOQUEIO} dias')

    with transacao() as cur:
        # Trava a linha do profissional até o fim da transação: dois pedidos ao mesmo tempo esperam um pelo outro.
        # Sem isso, os dois passariam na checagem abaixo, porque o banco não tem regra contra períodos sobrepostos
        cur.execute('SELECT ID_USUARIO FROM USUARIO WHERE ID_USUARIO = ? WITH LOCK', (profissional_id,))
        cur.execute('''SELECT FIRST 1 1 FROM BLOQUEIO_AGENDA
            WHERE PROFISSIONAL_ID = ? AND DATA_INICIO <= ? AND DATA_FIM >= ?''', (profissional_id, fim, inicio))
        if cur.fetchone() is not None:
            raise ErroNegocio('Ja existe um bloqueio nesse periodo', 409)
        cur.execute('''INSERT INTO BLOQUEIO_AGENDA (PROFISSIONAL_ID, DATA_INICIO, DATA_FIM) VALUES (?, ?, ?)
            RETURNING BLOQUEIO_ID''', (profissional_id, inicio, fim))
        bloqueio_id = cur.fetchone()[0]
        cur.execute('''SELECT COUNT(*) FROM SESSAO WHERE PROFISSIONAL_ID = ? AND STATUS <> 'CANCELADO'
            AND DATA_HORA_INICIO < ? AND DATA_HORA_FIM > ?''',
                    (profissional_id, datetime.combine(fim + timedelta(days=1), time()), datetime.combine(inicio, time())))
        sessoes = cur.fetchone()[0]
    return {'bloqueio': _serializar_bloqueio(bloqueio_id, inicio, fim), 'sessoes_no_periodo': sessoes}


def apagar_bloqueio(profissional_id, bloqueio_id):
    with transacao() as cur:
        # PROFISSIONAL_ID no WHERE: ninguém apaga o bloqueio de outro profissional
        cur.execute('DELETE FROM BLOQUEIO_AGENDA WHERE BLOQUEIO_ID = ? AND PROFISSIONAL_ID = ?', (bloqueio_id, profissional_id))
        if cur.rowcount == 0:
            raise ErroNegocio('Bloqueio nao encontrado', 404)
