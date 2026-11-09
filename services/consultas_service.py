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
# 24: o paciente cancela com reembolso integral ate 15 min antes; depois disso nao cancela mais
ANTECEDENCIA_CANCELAMENTO = timedelta(minutes=15)
# A consulta so vale paga: sem pagamento, o horario fica reservado por este tempo e depois volta a ficar livre
RESERVA_SEM_PAGAMENTO = timedelta(minutes=30)
MINUTOS_RESERVA = int(RESERVA_SEM_PAGAMENTO.total_seconds() // 60)


def serializar_sessao(colunas, linha):
    # TIMESTAMP do banco representa o horario local, sem conversao para GMT.
    sessao = {coluna: valor.isoformat() if isinstance(valor, (date, datetime)) else valor
              for coluna, valor in zip(colunas, linha)}
    sessao['paciente_foto_url'] = upload_service.url_foto_usuario(sessao['paciente_id'])
    sessao['profissional_foto_url'] = upload_service.url_foto_usuario(sessao['profissional_id'])
    return sessao


def _sessao_retornada(cur):
    linha = cur.fetchone()
    return serializar_sessao(nomes_das_colunas(cur), linha)


def ler_intervalo(inicio, fim):
    try:
        if not isinstance(inicio, str) or not isinstance(fim, str) or 'T' not in inicio or 'T' not in fim:
            raise ValueError()
        # O agendamento do paciente tambem envia segundos (:00).
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
    # Serializa agendamentos/reagendamentos concorrentes das mesmas pessoas.
    # A ordem fixa evita que duas requisicoes adquiram os bloqueios ao contrario.
    cur.execute('''SELECT ID_USUARIO FROM USUARIO
        WHERE ID_USUARIO IN (?, ?) ORDER BY ID_USUARIO WITH LOCK''',
                (profissional_id, paciente_id))
    cur.fetchall()


def listar(usuario_id, papel):
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


def agendar(paciente_id, papel, data):
    if not isinstance(data, dict) or not data:
        raise ErroNegocio('Formato invalido')
    id_profissional = data.get('id_profissional')
    inicio = data.get('timestamp_inicio')
    fim = data.get('timestamp_fim')
    if not id_profissional or not inicio or not fim:
        raise ErroNegocio('Profissional, inicio e fim sao obrigatorios')
    inicio, fim = ler_intervalo(inicio, fim)

    with transacao() as cur:
        cur.execute('''
            SELECT p.PRECO_HORA
            FROM PROFISSIONAL p
            INNER JOIN USUARIO u ON u.ID_USUARIO = p.USUARIO_ID
            WHERE p.USUARIO_ID = ? AND u.ATIVO = TRUE
        ''', (id_profissional,))
        profissional = cur.fetchone()
        if profissional is None:
            raise ErroNegocio('Profissional não encontrado', 404)
        if papel != 'PACIENTE':
            raise ErroNegocio('Somente pacientes podem agendar consultas', 403)
        # Sem assinatura valida (9) o profissional sai da busca e para de receber agendamentos novos
        if assinaturas_service.plano_ativo(cur, id_profissional) is None:
            raise ErroNegocio('Este profissional não está aceitando agendamentos', 409)
        na_agenda, parametros = disponibilidade_service.filtro_sql('p.USUARIO_ID', inicio, fim)
        cur.execute(f'SELECT 1 FROM PROFISSIONAL p WHERE p.USUARIO_ID = ? AND {na_agenda}',
                    (id_profissional, *parametros))
        if cur.fetchone() is None:
            raise ErroNegocio('Horario fora da agenda do profissional', 409)

        reservar_participantes(cur, id_profissional, paciente_id)
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
        """, (paciente_id, id_profissional, None, None, inicio, fim, profissional[0], None))
        sessao = _sessao_retornada(cur)
        auditar(cur, paciente_id, 'SESSAO_AGENDADA', 'SESSAO', sessao['sessao_id'])
        quando = formatar_data_hora(inicio)
        # O profissional so fica sabendo quando o pagamento confirma (pagamentos_service._sessao_paga)
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
            # Pix ou boleto pago sem ninguem abrir a tela de pagamento: confirma em vez de cancelar
            if pagamentos_service.confirmar_pendentes(sessao_id):
                continue
            canceladas += _cancelar_reserva(sessao_id)
        except Exception:
            # Arkhé fora do ar: na duvida nao cancela; o job tenta de novo no proximo minuto
            logger.exception('Falha ao expirar a reserva %s', sessao_id)
    return canceladas


def _cancelar_reserva(sessao_id):
    with transacao() as cur:
        cur.execute('''UPDATE SESSAO SET STATUS = 'CANCELADO' WHERE SESSAO_ID = ? AND STATUS = 'AGENDADO'
            RETURNING PACIENTE_ID, PROFISSIONAL_ID, DATA_HORA_INICIO, DATA_HORA_FIM''', (sessao_id,))
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


def _ler_link(link):
    if link is not None and not isinstance(link, str):
        raise ErroNegocio('Link da reuniao invalido')
    link = (link or '').strip() or None
    try:
        partes = urlsplit(link) if link else None
        if link and (len(link) > 255 or partes.scheme not in ('http', 'https')
                     or not partes.hostname or partes.username or partes.password
                     or any(caractere.isspace() for caractere in link)):
            raise ValueError()
    except ValueError:
        raise ErroNegocio('Informe um link HTTP ou HTTPS valido, de ate 255 caracteres')
    return link


def atualizar(profissional_id, sessao_id, dados):
    if not profissional_id:
        raise ErroNegocio('Token invalido', 401)
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

    with transacao() as cur:
        cur.execute('''SELECT STATUS, DATA_HORA_INICIO, DATA_HORA_FIM, PACIENTE_ID
            FROM SESSAO WHERE SESSAO_ID = ? AND PROFISSIONAL_ID = ? WITH LOCK''',
                    (sessao_id, profissional_id))
        sessao = cur.fetchone()
        if sessao is None:
            raise ErroNegocio('Sessao nao encontrada', 404)
        if sessao[0] in ('REALIZADO', 'CANCELADO'):
            raise ErroNegocio('Esta sessao ja foi encerrada e nao pode ser alterada', 409)
        if status == 'REALIZADO' and sessao[2] > datetime.now():
            raise ErroNegocio('Aguarde o fim do horario da sessao para concluir', 409)

        campos, parametros = [], []
        if intervalo:
            inicio, fim = intervalo
            reservar_participantes(cur, profissional_id, sessao[3])
            cur.execute('''SELECT FIRST 1 1 FROM SESSAO
                WHERE SESSAO_ID <> ? AND STATUS <> 'CANCELADO'
                AND (PROFISSIONAL_ID = ? OR PACIENTE_ID = ?)
                AND DATA_HORA_INICIO < ? AND DATA_HORA_FIM > ?''',
                        (sessao_id, profissional_id, sessao[3], fim, inicio))
            if cur.fetchone() is not None:
                raise ErroNegocio(MENSAGEM_CONFLITO, 409)
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
        # 30: o paciente fica sabendo de horario novo e de link novo (tambem pelo WhatsApp)
        quando = formatar_data_hora(intervalo[0] if intervalo else sessao[1])
        avisos = ([('Consulta remarcada', f'Sua consulta passou para {quando}.')] if intervalo else []) + \
                 ([('Link da consulta', f'O link da sua consulta de {quando} é {link}')] if link else [])
        emails = [notificacoes_service.notificar(cur, sessao[3], 'SESSAO_ALTERADA', titulo, mensagem,
                                                 {'sessao_id': sessao_id}) for titulo, mensagem in avisos]
        if status == 'CANCELADO':
            _, cancelamento = _ao_cancelar(cur, sessao_id, profissional_id, sessao[3], sessao[1], sessao[2], avisar=sessao[3])
            emails += cancelamento
    notificacoes_service.enviar_emails(emails)
    return atualizada


def url_sala(sessao_id):
    return f"{os.getenv('FRONT_URL', 'https://psicodaily-front.vercel.app').rstrip('/')}/videochamada/{sessao_id}"


def cancelar(paciente_id, papel, sessao_id):
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
    quem_cancelou = paciente_id if avisar == profissional_id else profissional_id
    cur.execute('SELECT NOME FROM USUARIO WHERE ID_USUARIO = ?', (quem_cancelou,))
    mensagem = f'{cur.fetchone()[0]} cancelou a consulta de {formatar_data_hora(inicio)}.'
    if reembolso and avisar == paciente_id:
        mensagem += ' O valor pago será devolvido integralmente.'
    emails.append(notificacoes_service.notificar(cur, avisar, 'SESSAO_CANCELADA', 'Consulta cancelada', mensagem,
                                                 {'sessao_id': sessao_id}))
    return reembolso, emails


def avaliar(paciente_id, papel, sessao_id, dados):
    if papel != 'PACIENTE' or not paciente_id:
        raise ErroNegocio('Somente o paciente pode avaliar a sessao', 403)
    if not isinstance(dados, dict) or set(dados) - {'nota', 'comentario', 'publica'}:
        raise ErroNegocio('Envie apenas nota, comentario e publica')
    nota = dados.get('nota')
    if type(nota) is not int or not 1 <= nota <= 5:
        raise ErroNegocio('nota deve ser inteiro entre 1 e 5')
    # Privada por padrao: o comentario publico aparece no perfil do profissional, sem o nome do paciente
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
            # Logo ao sair da videochamada a sessao ainda e CONFIRMADO: o profissional so marca REALIZADO depois do fim
            if sessao[1] not in ('CONFIRMADO', 'REALIZADO') or sessao[2] > datetime.now():
                raise ErroNegocio('Só é possível avaliar consultas pagas que já começaram', 409)
            cur.execute('''INSERT INTO AVALIACAO (SESSAO_ID, PACIENTE_ID, PROFISSIONAL_ID, NOTA, COMENTARIO, PUBLICA)
                VALUES (?, ?, ?, ?, ?, ?)''', (sessao_id, paciente_id, sessao[0], nota, comentario, publica))
    except fdb.DatabaseError as erro:
        if 'UQ_AVALIACAO_SESSAO' in str(erro):
            raise ErroNegocio('Sessão já avaliada', 409)
        raise
    return {'sessao_id': sessao_id, 'nota': nota, 'publica': publica}


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
    status, inicio, fim = sessao[:3]
    participantes = {linha[0]: {'id_usuario': linha[0], 'nome': linha[1], 'email': linha[2]}
                     for linha in (sessao[3:6], sessao[6:9])}
    agora = datetime.now()
    # Sala so para consulta paga; REALIZADO ja passou do fim
    if (id_usuario not in participantes or status != 'CONFIRMADO'
            or inicio > agora.replace(second=0, microsecond=0) + timedelta(minutes=15) or fim < agora):
        return None
    return {'sessao_id': sessao_id, 'inicio': inicio, 'participantes': participantes}


# 23: STUN descobre o IP publico de cada lado e basta na maioria das redes. Em NAT simetrico (4G, rede de
# empresa) a conexao direta falha e o TURN retransmite a midia. Cloudflare Realtime: 1.000 GB/mes gratis.
STUN_PUBLICO = [{'urls': 'stun:stun.cloudflare.com:3478'}]
URL_TURN = 'https://rtc.live.cloudflare.com/v1/turn/keys/{}/credentials/generate-ice-servers'
# Cobre a consulta mais longa (120 min) mais os 15 min de antecedencia. A credencial vai para o navegador,
# entao quanto mais curta, menos tempo alguem consegue usar a nossa cota se ela vazar.
TURN_VALIDADE_SEGUNDOS = 3 * 60 * 60


def ice_servers(id_usuario, sessao_id):
    """Lista de servidores para o RTCPeerConnection, so para quem pode entrar na sala agora."""
    if not sala_da_sessao(id_usuario, sessao_id):
        raise ErroNegocio('Sala indisponivel', 404)
    chave = os.getenv('TURN_KEY_ID')
    # Sem chave (dev, CI) a chamada segue so com STUN
    if not chave:
        return STUN_PUBLICO
    req = Request(URL_TURN.format(chave), method='POST',
                  data=json.dumps({'ttl': TURN_VALIDADE_SEGUNDOS}).encode(),
                  headers={'Authorization': f'Bearer {os.getenv("TURN_KEY_API_TOKEN", "")}',
                           'Content-Type': 'application/json',
                           # A Cloudflare barra o User-Agent padrao do urllib (erro 1010, 403)
                           'User-Agent': 'psicodaily-api'})
    try:
        with urlopen(req, timeout=10) as resposta:
            return json.load(resposta)['iceServers']
    except OSError:
        # Cloudflare fora do ar nao pode impedir a consulta: sem TURN ela ainda conecta na maioria das redes
        logger.exception('TURN da Cloudflare indisponivel')
        return STUN_PUBLICO


# ponytail: dedupe em memoria (processo unico); reinicio do servidor pode repetir um aviso
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
