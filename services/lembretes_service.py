"""Tarefas automáticas que rodam a cada minuto numa thread do servidor.

1. Lembretes de consulta, 24h e 1h antes, para paciente e profissional.
2. Reservas não pagas a tempo: cancela e libera o horário (consultas_service.expirar_reservas).
3. Assinaturas: confirma cobranças pagas e manda o Pix de renovação (assinaturas_service.processar).

Por que uma thread e não um cron separado: o servidor precisa ser um processo só (salas em memória),
e a thread reaproveita a conexão e as funções da própria API. JOBS=0 desliga a thread (os testes usam isso).
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from database.db import transacao
from funcao import formatar_data_hora
from services import assinaturas_service, consultas_service, notificacoes_service

logger = logging.getLogger(__name__)

INTERVALO_SEGUNDOS = 60
# (coluna que marca o aviso, de quanto antes, até quanto antes): a janela de 24h para em 1h, para não avisar duas vezes
LEMBRETES = (
    ('LEMBRETE_24H_EM', timedelta(hours=24), timedelta(hours=1)),
    ('LEMBRETE_1H_EM', timedelta(hours=1), timedelta(0)),
)


def enviar_lembretes(agora=None):
    """Avisa paciente e profissional das sessoes que entraram em cada janela. Devolve quantas sessoes avisou."""
    agora = agora or datetime.now()
    emails, avisadas = [], 0
    with transacao() as cur:
        for coluna, de, ate in LEMBRETES:
            # Consultas pagas que começam dentro da janela e ainda não receberam este lembrete
            cur.execute(f'''SELECT s.SESSAO_ID, s.PACIENTE_ID, s.PROFISSIONAL_ID, s.DATA_HORA_INICIO, u.NOME
                FROM SESSAO s JOIN USUARIO u ON u.ID_USUARIO = s.PROFISSIONAL_ID
                WHERE s.STATUS = 'CONFIRMADO' AND s.{coluna} IS NULL
                  AND s.DATA_HORA_INICIO > ? AND s.DATA_HORA_INICIO <= ?''', (agora + ate, agora + de))
            for sessao_id, paciente_id, profissional_id, inicio, profissional in cur.fetchall():
                # Marca o lembrete como enviado. Se outra execução marcou antes, o RETURNING volta NULL e pula
                cur.execute(f'''UPDATE SESSAO SET {coluna} = CURRENT_TIMESTAMP
                    WHERE SESSAO_ID = ? AND {coluna} IS NULL RETURNING SESSAO_ID''', (sessao_id,))
                if cur.fetchone()[0] is None:
                    continue
                quando = formatar_data_hora(inicio)
                sala = consultas_service.url_sala(sessao_id)
                dados = {'sessao_id': sessao_id}
                emails += [
                    notificacoes_service.notificar(cur, paciente_id, 'LEMBRETE_CONSULTA', 'Lembrete de consulta',
                                                   f'Sua consulta com {profissional} é em {quando}. Entre pela sala: {sala}',
                                                   dados),
                    notificacoes_service.notificar(cur, profissional_id, 'LEMBRETE_CONSULTA', 'Lembrete de consulta',
                                                   f'Você tem uma consulta em {quando}. Sala: {sala}', dados),
                ]
                avisadas += 1
    notificacoes_service.enviar_emails(emails)
    return avisadas


def _rodar():
    # Cada tarefa tem o próprio try: uma falha (ex.: Arkhé fora do ar) não impede as outras nem para a thread
    while True:
        try:
            enviar_lembretes()
        except Exception:
            logger.exception('Falha ao enviar lembretes')
        try:
            consultas_service.expirar_reservas()
        except Exception:
            logger.exception('Falha ao expirar as reservas sem pagamento')
        try:
            assinaturas_service.processar()
        except Exception:
            logger.exception('Falha ao processar as assinaturas')
        time.sleep(INTERVALO_SEGUNDOS)


def iniciar():
    """Liga a thread das tarefas automáticas. O servidor roda num processo só, então existe uma thread só."""
    threading.Thread(target=_rodar, name='lembretes', daemon=True).start()
