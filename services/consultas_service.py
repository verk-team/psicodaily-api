"""Consultas (tabela SESSAO): agendar, remarcar, cancelar, avaliar e a sala da videochamada.

Ciclo de vida de uma consulta:
    AGENDADO   o paciente reservou o horário e tem 30 minutos para pagar
    CONFIRMADO o pagamento entrou (pagamentos_service._sessao_paga)
    REALIZADO  o profissional marcou como feita, depois do horário
    CANCELADO  cancelada por alguém, ou reserva que não foi paga a tempo

Lock (`WITH LOCK`): trava as linhas lidas até o fim da transação. Outra requisição que tente travar as mesmas
linhas espera a primeira terminar. É assim que dois agendamentos ao mesmo tempo não pegam o mesmo horário.
"""
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import fdb

from database.db import nomes_das_colunas, transacao
from funcao import formatar_data_hora
from services import (assinaturas_service, disponibilidade_service, email_service, lista_espera_service,
                      notificacoes_service, pagamentos_service)
from services.auditoria_service import auditar
from services.erros import ErroNegocio
from services.upload_service import upload_service

logger = logging.getLogger(__name__)

MENSAGEM_CONFLITO = 'Profissional ou paciente ja possui consulta nesse horario'
# O paciente cancela com reembolso integral até 15 min antes; depois disso não cancela mais
ANTECEDENCIA_CANCELAMENTO = timedelta(minutes=15)
# A consulta só vale paga: sem pagamento, o horário fica reservado por este tempo e depois volta a ficar livre
RESERVA_SEM_PAGAMENTO = timedelta(minutes=30)
MINUTOS_RESERVA = int(RESERVA_SEM_PAGAMENTO.total_seconds() // 60)


def serializar_sessao(colunas, linha):
    """Linha da SESSAO -> dicionário para o JSON, com datas em texto ISO e as fotos dos dois participantes."""
    # O TIMESTAMP do banco é o horário local, sem conversão para GMT
    sessao = {coluna: valor.isoformat() if isinstance(valor, (date, datetime)) else valor
              for coluna, valor in zip(colunas, linha)}
    sessao['paciente_foto_url'] = upload_service.url_foto_usuario(sessao['paciente_id'])
    sessao['profissional_foto_url'] = upload_service.url_foto_usuario(sessao['profissional_id'])
    return sessao


def _sessao_retornada(cur):
    """A linha que o último comando com RETURNING * devolveu, já serializada."""
    linha = cur.fetchone()
    return serializar_sessao(nomes_das_colunas(cur), linha)


def ler_intervalo(inicio, fim):
    """Textos 'AAAA-MM-DDTHH:MM' -> (datetime, datetime). Exige horário futuro, fim depois do início e sem fuso."""
    try:
        if not isinstance(inicio, str) or not isinstance(fim, str) or 'T' not in inicio or 'T' not in fim:
            raise ValueError()
        # O agendamento do paciente também envia segundos (:00)
        inicio = datetime.fromisoformat(inicio)
        fim = datetime.fromisoformat(fim)
        if any(data.tzinfo is not None or data.second or data.microsecond for data in (inicio, fim)):
            raise ValueError()
    except (TypeError, ValueError):
        raise ErroNegocio('Informe inicio e fim no formato AAAA-MM-DDTHH:MM, sem fuso horario')
    if inicio < datetime.now().replace(second=0, microsecond=0) or fim <= inicio:
        raise ErroNegocio('Informe um horario futuro e um fim posterior ao inicio')
    return inicio, fim


def reservar_participantes(cur, profissional_id, paciente_id):
    """Trava as linhas do profissional e do paciente até o fim da transação.

    Assim, agendamentos e remarcações das mesmas pessoas ao mesmo tempo rodam um de cada vez, e a checagem
    de conflito de horário que vem depois não deixa passar dois no mesmo horário.
    """
    # A ordem fixa (por ID) evita que duas requisições travem as duas linhas em ordem trocada e fiquem
    # esperando uma pela outra para sempre (deadlock)
    cur.execute('''SELECT ID_USUARIO FROM USUARIO
        WHERE ID_USUARIO IN (?, ?) ORDER BY ID_USUARIO WITH LOCK''',
                (profissional_id, paciente_id))
    cur.fetchall()


def listar(usuario_id, papel):
    """Consultas do paciente ou do profissional (o admin vê todas), das mais novas para as mais antigas.

    Cada consulta vem com o último registro de humor do paciente e a avaliação, se houver.
    """
    if not usuario_id:
        raise ErroNegocio('Token invalido', 401)
    if papel not in ('PACIENTE', 'PSICOLOGO', 'PSIQUIATRA', 'ADMIN'):
        raise ErroNegocio('Usuario sem permissao para consultar sessoes', 403)

    where, parametros = '', ()
    if papel == 'PACIENTE':
        where, parametros = 'WHERE s.PACIENTE_ID = ?', (usuario_id,)
    elif papel in ('PSICOLOGO', 'PSIQUIATRA'):
        where, parametros = 'WHERE s.PROFISSIONAL_ID = ?', (usuario_id,)

    with transacao() as cur:
        # O LEFT JOIN com REGISTRO pega só o registro mais recente do paciente (a subconsulta com FIRST 1)
        cur.execute(f'''
                 SELECT s.*, u.NOME AS PROFISSIONAL_NOME,
                     paciente.NOME AS PACIENTE_NOME,
                   r.HUMOR AS ULTIMO_HUMOR,
                   r.QUALIDADE_SONO AS ULTIMA_QUALIDADE_SONO,
                   r.CRIADO_EM AS ULTIMO_REGISTRO_EM,
                   av.NOTA AS NOTA_AVALIACAO,
                   av.COMENTARIO AS COMENTARIO_AVALIACAO,
                   COALESCE(av.PUBLICA, FALSE) AS AVALIACAO_PUBLICA
            FROM SESSAO s
            INNER JOIN PROFISSIONAL p ON p.USUARIO_ID = s.PROFISSIONAL_ID
            INNER JOIN USUARIO u ON u.ID_USUARIO = p.USUARIO_ID
            INNER JOIN USUARIO paciente ON paciente.ID_USUARIO = s.PACIENTE_ID
            LEFT JOIN REGISTRO r ON r.REGISTRO_ID = (
                SELECT FIRST 1 r2.REGISTRO_ID
                FROM REGISTRO r2
                WHERE r2.USUARIO_ID = s.PACIENTE_ID
                ORDER BY r2.CRIADO_EM DESC, r2.REGISTRO_ID DESC
            )
            LEFT JOIN AVALIACAO av ON av.SESSAO_ID = s.SESSAO_ID
            {where}
            ORDER BY s.DATA_HORA_INICIO DESC
        ''', parametros)
        colunas = nomes_das_colunas(cur)
        return [serializar_sessao(colunas, linha) for linha in cur.fetchall()]


# ---------------------------------------------------------------------------
# Agendar
# ---------------------------------------------------------------------------

def _preco_do_profissional(cur, id_profissional):
    """Preço da consulta em centavos; 404 se o profissional não existe ou está inativo."""
    cur.execute('''
        SELECT p.PRECO_HORA
        FROM PROFISSIONAL p
        INNER JOIN USUARIO u ON u.ID_USUARIO = p.USUARIO_ID
        WHERE p.USUARIO_ID = ? AND u.ATIVO = TRUE
    ''', (id_profissional,))
    profissional = cur.fetchone()
    if profissional is None:
        raise ErroNegocio('Profissional não encontrado', 404)
    return profissional[0]


def _exigir_na_agenda(cur, id_profissional, inicio, fim):
    """409 se o horário não cabe numa janela da agenda do profissional ou cai num bloqueio."""
    na_agenda, parametros = disponibilidade_service.filtro_sql('p.USUARIO_ID', inicio, fim)
    cur.execute(f'SELECT 1 FROM PROFISSIONAL p WHERE p.USUARIO_ID = ? AND {na_agenda}',
                (id_profissional, *parametros))
    if cur.fetchone() is None:
        raise ErroNegocio('Horario fora da agenda do profissional', 409)


def _exigir_sem_conflito(cur, id_profissional, paciente_id, inicio, fim):
    """409 se o profissional ou o paciente já tem consulta (não cancelada) que se cruza com o horário."""
    # Dois horários se cruzam quando cada um começa antes do outro terminar
    cur.execute('''
        SELECT FIRST 1 1
        FROM SESSAO
        WHERE (PROFISSIONAL_ID = ? OR PACIENTE_ID = ?)
          AND STATUS <> 'CANCELADO'
          AND DATA_HORA_INICIO < ?
          AND DATA_HORA_FIM > ?
    ''', (id_profissional, paciente_id, fim, inicio))
    if cur.fetchone() is not None:
        raise ErroNegocio(MENSAGEM_CONFLITO, 409)


def agendar(paciente_id, papel, data):
    """O paciente reserva um horário. A consulta nasce AGENDADO e só vale depois de paga (30 minutos de prazo)."""
    if not isinstance(data, dict) or not data:
        raise ErroNegocio('Formato invalido')
    id_profissional = data.get('id_profissional')
    inicio = data.get('timestamp_inicio')
    fim = data.get('timestamp_fim')
    if not id_profissional or not inicio or not fim:
        raise ErroNegocio('Profissional, inicio e fim sao obrigatorios')
    inicio, fim = ler_intervalo(inicio, fim)

    with transacao() as cur:
        preco = _preco_do_profissional(cur, id_profissional)
        if papel != 'PACIENTE':
            raise ErroNegocio('Somente pacientes podem agendar consultas', 403)
        # Sem assinatura válida o profissional sai da busca e para de receber agendamentos novos
        if assinaturas_service.plano_ativo(cur, id_profissional) is None:
            raise ErroNegocio('Este profissional não está aceitando agendamentos', 409)
        _exigir_na_agenda(cur, id_profissional, inicio, fim)

        reservar_participantes(cur, id_profissional, paciente_id)
        _exigir_sem_conflito(cur, id_profissional, paciente_id, inicio, fim)

        cur.execute("""
        INSERT INTO SESSAO (
            PACIENTE_ID
          , PROFISSIONAL_ID
          , ENCAMINHAMENTO_ID
          , CONVERSA_ID
          , DATA_HORA_INICIO
          , DATA_HORA_FIM
          , STATUS
          , VALOR_CENTAVOS
          , LINK_REUNIAO)
            VALUES(?, ?, ?, ?, ?, ?, 'AGENDADO', ?, ?) RETURNING *
        """, (paciente_id, id_profissional, None, None, inicio, fim, preco, None))
        sessao = _sessao_retornada(cur)
        auditar(cur, paciente_id, 'SESSAO_AGENDADA', 'SESSAO', sessao['sessao_id'])
        quando = formatar_data_hora(inicio)
        # O profissional só fica sabendo quando o pagamento confirma (pagamentos_service._sessao_paga)
        emails = [notificacoes_service.notificar(
            cur, paciente_id, 'SESSAO_AGENDADA', 'Horário reservado',
            f'Reservamos o horário de {quando} por {MINUTOS_RESERVA} minutos. A consulta só é confirmada com o pagamento; '
            f'sem ele, a reserva é cancelada.', {'sessao_id': sessao['sessao_id']})]
    notificacoes_service.enviar_emails(emails)
    return sessao


def expirar_reservas(agora=None):
    """Job: cancela as consultas futuras que nao foram pagas a tempo e libera o horario. Devolve quantas cancelou."""
    agora = agora or datetime.now()
    with transacao() as cur:
        cur.execute('''SELECT SESSAO_ID FROM SESSAO
            WHERE STATUS = 'AGENDADO' AND CRIADO_EM <= ? AND DATA_HORA_INICIO > ?''',
                    (agora - RESERVA_SEM_PAGAMENTO, agora))
        reservas = [linha[0] for linha in cur.fetchall()]
    canceladas = 0
    for sessao_id in reservas:
        try:
            # Pix ou boleto pago sem ninguém abrir a tela de pagamento: confirma em vez de cancelar
            if pagamentos_service.confirmar_pendentes(sessao_id):
                continue
            canceladas += _cancelar_reserva(sessao_id)
        except Exception:
            # Arkhé fora do ar: na dúvida não cancela; o job tenta de novo no próximo minuto
            logger.exception('Falha ao expirar a reserva %s', sessao_id)
    return canceladas


def _cancelar_reserva(sessao_id):
    """Cancela uma reserva não paga, avisa a lista de espera e o paciente. Devolve 1 se cancelou, 0 se não."""
    with transacao() as cur:
        cur.execute('''UPDATE SESSAO SET STATUS = 'CANCELADO' WHERE SESSAO_ID = ? AND STATUS = 'AGENDADO'
            RETURNING PACIENTE_ID, PROFISSIONAL_ID, DATA_HORA_INICIO, DATA_HORA_FIM''', (sessao_id,))
        # No Firebird, um UPDATE ... RETURNING que não acha a linha devolve uma linha de NULLs (não None)
        paciente_id, profissional_id, inicio, fim = cur.fetchone()
        if paciente_id is None:
            return 0  # pago ou cancelado entre a busca e aqui
        emails = lista_espera_service.avisar_vaga(cur, profissional_id, inicio, fim, paciente_id)
        emails.append(notificacoes_service.notificar(
            cur, paciente_id, 'SESSAO_CANCELADA', 'Reserva cancelada',
            f'A reserva da consulta de {formatar_data_hora(inicio)} foi cancelada porque o pagamento '
            f'não foi concluído em {MINUTOS_RESERVA} minutos. Se ainda quiser o horário, agende de novo.', {'sessao_id': sessao_id}))
    notificacoes_service.enviar_emails(emails)
    return 1


# ---------------------------------------------------------------------------
# Alterar (profissional)
# ---------------------------------------------------------------------------

def _ler_link(link):
    """Link da reunião: None (apaga o link) ou um endereço http/https de até 255 caracteres."""
    if link is not None and not isinstance(link, str):
        raise ErroNegocio('Link da reuniao invalido')
    link = (link or '').strip() or None
    if not link:
        return None
    try:
        partes = urlsplit(link)
        invalido = (len(link) > 255 or partes.scheme not in ('http', 'https') or not partes.hostname
                    # Usuário e senha dentro do link (http://usuario:senha@site) não são aceitos
                    or partes.username or partes.password
                    or any(caractere.isspace() for caractere in link))
    except ValueError:
        invalido = True
    if invalido:
        raise ErroNegocio('Informe um link HTTP ou HTTPS valido, de ate 255 caracteres')
    return link


def _ler_alteracao(dados):
    """Valida o corpo do PATCH e devolve (status, (inicio, fim) ou None, link)."""
    permitidos = {'timestamp_inicio', 'timestamp_fim', 'status', 'link_reuniao'}
    if not isinstance(dados, dict) or not dados or set(dados) - permitidos:
        raise ErroNegocio('Informe apenas horario, status ou link da reuniao')
    if ('timestamp_inicio' in dados) != ('timestamp_fim' in dados):
        raise ErroNegocio('Informe inicio e fim juntos')
    status = dados.get('status')
    if 'status' in dados and status not in ('REALIZADO', 'CANCELADO'):
        raise ErroNegocio('Status deve ser REALIZADO ou CANCELADO')
    if status and len(dados) != 1:
        raise ErroNegocio('Altere o status separadamente do horario e do link')

    intervalo = ler_intervalo(dados['timestamp_inicio'], dados['timestamp_fim']) if 'timestamp_inicio' in dados else None
    link = _ler_link(dados.get('link_reuniao')) if 'link_reuniao' in dados else None
    return status, intervalo, link


def _exigir_horario_livre_para_remarcar(cur, sessao_id, profissional_id, paciente_id, inicio, fim):
    """Como _exigir_sem_conflito, mas ignorando a própria consulta que está sendo remarcada."""
    cur.execute('''SELECT FIRST 1 1 FROM SESSAO
        WHERE SESSAO_ID <> ? AND STATUS <> 'CANCELADO'
        AND (PROFISSIONAL_ID = ? OR PACIENTE_ID = ?)
        AND DATA_HORA_INICIO < ? AND DATA_HORA_FIM > ?''',
                (sessao_id, profissional_id, paciente_id, fim, inicio))
    if cur.fetchone() is not None:
        raise ErroNegocio(MENSAGEM_CONFLITO, 409)


def _avisar_alteracao(cur, sessao_id, paciente_id, quando, remarcou, link):
    """O paciente fica sabendo do horário novo e do link novo (também pelo WhatsApp)."""
    avisos = []
    if remarcou:
        avisos.append(('Consulta remarcada', f'Sua consulta passou para {quando}.'))
    if link:
        avisos.append(('Link da consulta', f'O link da sua consulta de {quando} é {link}'))
    return [notificacoes_service.notificar(cur, paciente_id, 'SESSAO_ALTERADA', titulo, mensagem,
                                           {'sessao_id': sessao_id}) for titulo, mensagem in avisos]


def atualizar(profissional_id, sessao_id, dados):
    """O profissional remarca, troca o link da reunião ou encerra a consulta (REALIZADO ou CANCELADO).

    O status muda sozinho: não vem junto com horário nem link na mesma chamada.
    """
    if not profissional_id:
        raise ErroNegocio('Token invalido', 401)
    status, intervalo, link = _ler_alteracao(dados)

    with transacao() as cur:
        cur.execute('''SELECT STATUS, DATA_HORA_INICIO, DATA_HORA_FIM, PACIENTE_ID
            FROM SESSAO WHERE SESSAO_ID = ? AND PROFISSIONAL_ID = ? WITH LOCK''',
                    (sessao_id, profissional_id))
        sessao = cur.fetchone()
        if sessao is None:
            raise ErroNegocio('Sessao nao encontrada', 404)
        status_atual, inicio_atual, fim_atual, paciente_id = sessao
        # Consulta encerrada (feita ou cancelada) não volta atrás
        if status_atual in ('REALIZADO', 'CANCELADO'):
            raise ErroNegocio('Esta sessao ja foi encerrada e nao pode ser alterada', 409)
        if status == 'REALIZADO' and fim_atual > datetime.now():
            raise ErroNegocio('Aguarde o fim do horario da sessao para concluir', 409)

        # Monta o UPDATE só com o que mudou: campos e parâmetros andam juntos, na mesma ordem
        campos, parametros = [], []
        if intervalo:
            inicio, fim = intervalo
            reservar_participantes(cur, profissional_id, paciente_id)
            _exigir_horario_livre_para_remarcar(cur, sessao_id, profissional_id, paciente_id, inicio, fim)
            campos.extend(['DATA_HORA_INICIO = ?', 'DATA_HORA_FIM = ?'])
            parametros.extend([inicio, fim])
        if status:
            campos.append('STATUS = ?')
            parametros.append(status)
        if 'link_reuniao' in dados:
            campos.append('LINK_REUNIAO = ?')
            parametros.append(link)
        cur.execute(f'''UPDATE SESSAO SET {', '.join(campos)}
            WHERE SESSAO_ID = ? AND PROFISSIONAL_ID = ? RETURNING *''',
                    (*parametros, sessao_id, profissional_id))
        atualizada = _sessao_retornada(cur)
        auditar(cur, profissional_id, 'SESSAO_ALTERADA', 'SESSAO', sessao_id)

        quando = formatar_data_hora(intervalo[0] if intervalo else inicio_atual)
        emails = _avisar_alteracao(cur, sessao_id, paciente_id, quando, remarcou=bool(intervalo), link=link)
        if status == 'CANCELADO':
            _, cancelamento = _ao_cancelar(cur, sessao_id, profissional_id, paciente_id, inicio_atual, fim_atual,
                                           avisar=paciente_id)
            emails += cancelamento
    notificacoes_service.enviar_emails(emails)
    return atualizada


def url_sala(sessao_id):
    """Link da tela da videochamada no front."""
    return f"{os.getenv('FRONT_URL', 'https://psicodaily-front.vercel.app').rstrip('/')}/videochamada/{sessao_id}"


# ---------------------------------------------------------------------------
# Cancelar (paciente)
# ---------------------------------------------------------------------------

def cancelar(paciente_id, papel, sessao_id):
    """O paciente cancela a própria consulta, até 15 minutos antes. Devolve a sessão e quanto será reembolsado."""
    if papel != 'PACIENTE':
        raise ErroNegocio('Somente o paciente cancela por esta rota', 403)
    with transacao() as cur:
        cur.execute('''SELECT STATUS, DATA_HORA_INICIO, DATA_HORA_FIM, PROFISSIONAL_ID
            FROM SESSAO WHERE SESSAO_ID = ? AND PACIENTE_ID = ? WITH LOCK''', (sessao_id, paciente_id))
        sessao = cur.fetchone()
        if sessao is None:
            raise ErroNegocio('Sessao nao encontrada', 404)
        status, inicio, fim, profissional_id = sessao
        _exigir_cancelavel(status, inicio)
        cur.execute("UPDATE SESSAO SET STATUS = 'CANCELADO' WHERE SESSAO_ID = ? RETURNING *", (sessao_id,))
        cancelada = _sessao_retornada(cur)
        auditar(cur, paciente_id, 'SESSAO_CANCELADA', 'SESSAO', sessao_id)
        reembolso, emails = _ao_cancelar(cur, sessao_id, profissional_id, paciente_id, inicio, fim, avisar=profissional_id)
    notificacoes_service.enviar_emails(emails)
    return {'sessao': cancelada, 'reembolso_centavos': reembolso}


def _exigir_cancelavel(status, inicio):
    if status in ('REALIZADO', 'CANCELADO'):
        raise ErroNegocio('Esta sessao ja foi encerrada e nao pode ser alterada', 409)
    if datetime.now() > inicio - ANTECEDENCIA_CANCELAMENTO:
        minutos = int(ANTECEDENCIA_CANCELAMENTO.total_seconds() // 60)
        raise ErroNegocio(f'Cancelamento so ate {minutos} minutos antes do horario', 409)


def checar_cancelamento(paciente_id, sessao_id):
    """Sem alterar nada: a sessao do paciente pode ser cancelada agora? Para o assistente propor o cancelamento."""
    with transacao() as cur:
        cur.execute('''SELECT s.STATUS, s.DATA_HORA_INICIO, u.NOME,
                (SELECT COUNT(*) FROM PAGAMENTO p WHERE p.SESSAO_ID = s.SESSAO_ID AND p.STATUS = 'PAGO')
            FROM SESSAO s JOIN USUARIO u ON u.ID_USUARIO = s.PROFISSIONAL_ID
            WHERE s.SESSAO_ID = ? AND s.PACIENTE_ID = ?''', (sessao_id, paciente_id))
        sessao = cur.fetchone()
    if sessao is None:
        raise ErroNegocio('Sessao nao encontrada', 404)
    status, inicio, profissional, pagos = sessao
    _exigir_cancelavel(status, inicio)
    return {'sessao_id': sessao_id, 'profissional_nome': profissional,
            'inicio': inicio.isoformat(timespec='minutes'), 'reembolso_integral': pagos > 0}


def _ao_cancelar(cur, sessao_id, profissional_id, paciente_id, inicio, fim, avisar):
    """Na transacao do cancelamento: reembolso integral do que foi pago, vaga para a lista de espera e aviso
    a outra parte. Devolve (centavos reembolsados, e-mails para enviar depois do commit)."""
    reembolso = pagamentos_service.registrar_reembolso(cur, sessao_id)
    emails = lista_espera_service.avisar_vaga(cur, profissional_id, inicio, fim, paciente_id)
    # `avisar` é quem recebe o aviso; quem cancelou é a outra pessoa da consulta
    quem_cancelou = paciente_id if avisar == profissional_id else profissional_id
    cur.execute('SELECT NOME FROM USUARIO WHERE ID_USUARIO = ?', (quem_cancelou,))
    mensagem = f'{cur.fetchone()[0]} cancelou a consulta de {formatar_data_hora(inicio)}.'
    if reembolso and avisar == paciente_id:
        mensagem += ' O valor pago será devolvido integralmente.'
    emails.append(notificacoes_service.notificar(cur, avisar, 'SESSAO_CANCELADA', 'Consulta cancelada', mensagem,
                                                 {'sessao_id': sessao_id}))
    return reembolso, emails


# ---------------------------------------------------------------------------
# Avaliar
# ---------------------------------------------------------------------------

def avaliar(paciente_id, papel, sessao_id, dados):
    """O paciente dá nota de 1 a 5 e, se quiser, um comentário. Uma avaliação por consulta."""
    if papel != 'PACIENTE' or not paciente_id:
        raise ErroNegocio('Somente o paciente pode avaliar a sessao', 403)
    if not isinstance(dados, dict) or set(dados) - {'nota', 'comentario', 'publica'}:
        raise ErroNegocio('Envie apenas nota, comentario e publica')
    nota = dados.get('nota')
    # type(...) is int, e não isinstance: True e False também são int em Python e passariam como 1 e 0
    if type(nota) is not int or not 1 <= nota <= 5:
        raise ErroNegocio('nota deve ser inteiro entre 1 e 5')
    # Privada por padrão: o comentário público aparece no perfil do profissional, sem o nome do paciente
    publica = dados.get('publica', False)
    if not isinstance(publica, bool):
        raise ErroNegocio('publica deve ser true ou false')
    comentario = dados.get('comentario')
    if comentario is not None:
        if not isinstance(comentario, str) or len(comentario.strip()) > 500:
            raise ErroNegocio('comentario deve ter no maximo 500 caracteres')
        comentario = comentario.strip() or None

    try:
        with transacao() as cur:
            cur.execute('''SELECT PROFISSIONAL_ID, STATUS, DATA_HORA_INICIO FROM SESSAO
                WHERE SESSAO_ID = ? AND PACIENTE_ID = ?''', (sessao_id, paciente_id))
            sessao = cur.fetchone()
            if sessao is None:
                raise ErroNegocio('Sessao nao encontrada', 404)
            profissional_id, status, inicio = sessao
            # Logo ao sair da videochamada a sessão ainda é CONFIRMADO: o profissional só marca REALIZADO depois do fim
            if status not in ('CONFIRMADO', 'REALIZADO') or inicio > datetime.now():
                raise ErroNegocio('Só é possível avaliar consultas pagas que já começaram', 409)
            cur.execute('''INSERT INTO AVALIACAO (SESSAO_ID, PACIENTE_ID, PROFISSIONAL_ID, NOTA, COMENTARIO, PUBLICA)
                VALUES (?, ?, ?, ?, ?, ?)''', (sessao_id, paciente_id, profissional_id, nota, comentario, publica))
    except fdb.DatabaseError as erro:
        # A restrição UQ_AVALIACAO_SESSAO do banco recusa a segunda avaliação da mesma consulta
        if 'UQ_AVALIACAO_SESSAO' in str(erro):
            raise ErroNegocio('Sessão já avaliada', 409)
        raise
    return {'sessao_id': sessao_id, 'nota': nota, 'publica': publica}


# ---------------------------------------------------------------------------
# Videochamada
# ---------------------------------------------------------------------------

def sala_da_sessao(id_usuario, sessao_id):
    """Dados da sala se o usuario pode entrar agora, senao None.

    A sala abre 15 min antes do inicio e fecha no fim, so para os participantes.
    """
    with transacao() as cur:
        cur.execute('''SELECT s.STATUS, s.DATA_HORA_INICIO, s.DATA_HORA_FIM,
                   pac.ID_USUARIO, pac.NOME, pac.EMAIL, prof.ID_USUARIO, prof.NOME, prof.EMAIL
            FROM SESSAO s
            INNER JOIN USUARIO pac ON pac.ID_USUARIO = s.PACIENTE_ID
            INNER JOIN USUARIO prof ON prof.ID_USUARIO = s.PROFISSIONAL_ID
            WHERE s.SESSAO_ID = ?''', (sessao_id,))
        sessao = cur.fetchone()
    if not sessao:
        return None
    (status, inicio, fim,
     paciente_id, paciente_nome, paciente_email,
     profissional_id, profissional_nome, profissional_email) = sessao
    participantes = {
        paciente_id: {'id_usuario': paciente_id, 'nome': paciente_nome, 'email': paciente_email},
        profissional_id: {'id_usuario': profissional_id, 'nome': profissional_nome, 'email': profissional_email},
    }
    agora = datetime.now()
    ainda_nao_abriu = inicio > agora.replace(second=0, microsecond=0) + timedelta(minutes=15)
    ja_acabou = fim < agora
    # Sala só para consulta paga; REALIZADO já passou do fim
    if id_usuario not in participantes or status != 'CONFIRMADO' or ainda_nao_abriu or ja_acabou:
        return None
    return {'sessao_id': sessao_id, 'inicio': inicio, 'participantes': participantes}


# STUN descobre o IP público de cada lado e basta na maioria das redes. Em NAT simétrico (4G, rede de
# empresa) a conexão direta falha e o TURN retransmite a mídia. Cloudflare Realtime: 1.000 GB/mês grátis.
STUN_PUBLICO = [{'urls': 'stun:stun.cloudflare.com:3478'}]
URL_TURN = 'https://rtc.live.cloudflare.com/v1/turn/keys/{}/credentials/generate-ice-servers'
# Cobre a consulta mais longa (120 min) mais os 15 min de antecedência. A credencial vai para o navegador,
# então quanto mais curta, menos tempo alguém consegue usar a nossa cota se ela vazar.
TURN_VALIDADE_SEGUNDOS = 3 * 60 * 60


def ice_servers(id_usuario, sessao_id):
    """Lista de servidores para o RTCPeerConnection, so para quem pode entrar na sala agora."""
    if not sala_da_sessao(id_usuario, sessao_id):
        raise ErroNegocio('Sala indisponivel', 404)
    chave = os.getenv('TURN_KEY_ID')
    # Sem chave (dev, CI) a chamada segue só com STUN
    if not chave:
        return STUN_PUBLICO
    req = Request(URL_TURN.format(chave), method='POST',
                  data=json.dumps({'ttl': TURN_VALIDADE_SEGUNDOS}).encode(),
                  headers={'Authorization': f'Bearer {os.getenv("TURN_KEY_API_TOKEN", "")}',
                           'Content-Type': 'application/json',
                           # A Cloudflare barra o User-Agent padrão do urllib (erro 1010, 403)
                           'User-Agent': 'psicodaily-api'})
    try:
        with urlopen(req, timeout=10) as resposta:
            return json.load(resposta)['iceServers']
    except OSError:
        # Cloudflare fora do ar não pode impedir a consulta: sem TURN ela ainda conecta na maioria das redes
        logger.exception('TURN da Cloudflare indisponivel')
        return STUN_PUBLICO


# ponytail: dedupe em memória (processo único); reinício do servidor pode repetir um aviso
_avisos_enviados = set()
_avisos_lock = threading.Lock()


def avisar_entrada_na_sala(sala, id_usuario, presentes):
    """Avisa por e-mail quem ainda nao entrou; uma vez por sessao e por quem entrou."""
    link = url_sala(sala['sessao_id'])
    quem_entrou = sala['participantes'][id_usuario]
    for destino in sala['participantes'].values():
        if destino['id_usuario'] == id_usuario or destino['id_usuario'] in presentes:
            continue
        chave = (sala['sessao_id'], id_usuario, destino['id_usuario'])
        # O lock impede duas threads de mandarem o mesmo aviso ao mesmo tempo
        with _avisos_lock:
            if chave in _avisos_enviados:
                continue
            _avisos_enviados.add(chave)
        email_service.enviar_email_async(
            destino['email'],
            f"{quem_entrou['nome']} entrou na sala da sua consulta",
            f"Olá, {destino['nome']}.\n\n"
            f"{quem_entrou['nome']} já está na sala da consulta de "
            f"{formatar_data_hora(sala['inicio'])} e aguarda você.\n\n"
            f"Entre pelo link: {link}",
        )
