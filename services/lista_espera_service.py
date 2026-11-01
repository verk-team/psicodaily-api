from datetime import datetime, timedelta

from database.db import transacao
from services import assinaturas_service, consultas_service, disponibilidade_service, notificacoes_service
from services.erros import ErroNegocio

JANELA_MAXIMA = timedelta(days=31)


def _exigir_paciente(papel):
    if papel != 'PACIENTE':
        raise ErroNegocio('Lista de espera e exclusiva do paciente', 403)


def _serializar(lista_espera_id, profissional_id, nome, inicio, fim, avisado_em, criado_em):
    return {'lista_espera_id': lista_espera_id, 'profissional_id': profissional_id, 'profissional_nome': nome,
            'inicio': inicio.isoformat(), 'fim': fim.isoformat(),
            'avisado_em': avisado_em.isoformat() if avisado_em else None, 'criado_em': criado_em.isoformat()}


def entrar(paciente_id, papel, dados):
    _exigir_paciente(papel)
    if not isinstance(dados, dict) or set(dados) != {'id_profissional', 'inicio', 'fim'}:
        raise ErroNegocio('Envie id_profissional, inicio e fim')
    profissional_id = dados['id_profissional']
    if type(profissional_id) is not int:
        raise ErroNegocio('id_profissional deve ser um numero inteiro')
    inicio, fim = consultas_service.ler_intervalo(dados['inicio'], dados['fim'])
    if fim - inicio > JANELA_MAXIMA:
        raise ErroNegocio('A janela de espera pode ter no maximo 31 dias')

    with transacao() as cur:
        cur.execute('''SELECT u.NOME FROM PROFISSIONAL p JOIN USUARIO u ON u.ID_USUARIO = p.USUARIO_ID
            WHERE p.USUARIO_ID = ? AND u.ATIVO = TRUE''', (profissional_id,))
        profissional = cur.fetchone()
        if profissional is None:
            raise ErroNegocio('Profissional não encontrado', 404)
        # Ferramenta de agenda do plano Profissional (9)
        if not assinaturas_service.libera(assinaturas_service.plano_ativo(cur, profissional_id), 'PROFISSIONAL'):
            raise ErroNegocio('Este profissional não tem lista de espera', 409)
        cur.execute('''INSERT INTO LISTA_ESPERA (PACIENTE_ID, PROFISSIONAL_ID, INICIO_DESEJADO, FIM_DESEJADO)
            VALUES (?, ?, ?, ?) RETURNING LISTA_ESPERA_ID, CRIADO_EM''', (paciente_id, profissional_id, inicio, fim))
        lista_espera_id, criado_em = cur.fetchone()
    return _serializar(lista_espera_id, profissional_id, profissional[0], inicio, fim, None, criado_em)


def listar(paciente_id, papel):
    """Entradas cuja janela ainda nao terminou, mais antigas primeiro."""
    _exigir_paciente(papel)
    with transacao() as cur:
        cur.execute('''SELECT l.LISTA_ESPERA_ID, l.PROFISSIONAL_ID, u.NOME, l.INICIO_DESEJADO, l.FIM_DESEJADO,
                   l.AVISADO_EM, l.CRIADO_EM
            FROM LISTA_ESPERA l JOIN USUARIO u ON u.ID_USUARIO = l.PROFISSIONAL_ID
            WHERE l.PACIENTE_ID = ? AND l.FIM_DESEJADO > ?
            ORDER BY l.LISTA_ESPERA_ID''', (paciente_id, datetime.now()))
        return [_serializar(*linha) for linha in cur.fetchall()]


def sair(paciente_id, papel, lista_espera_id):
    _exigir_paciente(papel)
    with transacao() as cur:
        cur.execute('DELETE FROM LISTA_ESPERA WHERE LISTA_ESPERA_ID = ? AND PACIENTE_ID = ?', (lista_espera_id, paciente_id))
        if cur.rowcount == 0:
            raise ErroNegocio('Entrada da lista de espera nao encontrada', 404)


def avisar_vaga(cur, profissional_id, inicio, fim, paciente_da_sessao):
    """Na transacao do cancelamento: avisa o primeiro da fila cuja janela cobre o horario liberado.

    Devolve os e-mails para enviar depois do commit. Nao reserva nada: quem agendar primeiro leva a vaga.
    """
    if inicio <= datetime.now():
        return []
    # Horario dentro de um bloqueio nao e vaga: ninguem conseguiria agendar, e a entrada segue na fila
    sem_bloqueio, parametros_bloqueio = disponibilidade_service.fora_de_bloqueio('l.PROFISSIONAL_ID', inicio, fim)
    # WITH LOCK: dois cancelamentos ao mesmo tempo nao avisam a mesma entrada
    cur.execute(f'''SELECT FIRST 1 l.LISTA_ESPERA_ID, l.PACIENTE_ID FROM LISTA_ESPERA l
        WHERE l.PROFISSIONAL_ID = ? AND l.AVISADO_EM IS NULL AND l.PACIENTE_ID <> ?
          AND l.INICIO_DESEJADO <= ? AND l.FIM_DESEJADO >= ? AND {sem_bloqueio}
        ORDER BY l.LISTA_ESPERA_ID WITH LOCK''', (profissional_id, paciente_da_sessao, inicio, fim, *parametros_bloqueio))
    primeiro = cur.fetchone()
    if primeiro is None:
        return []
    lista_espera_id, paciente_id = primeiro
    cur.execute('UPDATE LISTA_ESPERA SET AVISADO_EM = CURRENT_TIMESTAMP WHERE LISTA_ESPERA_ID = ?', (lista_espera_id,))
    cur.execute('SELECT NOME FROM USUARIO WHERE ID_USUARIO = ?', (profissional_id,))
    nome_profissional = cur.fetchone()[0]
    horario = inicio.strftime('%d/%m/%Y às %H:%M')
    return [notificacoes_service.notificar(
        cur, paciente_id, 'VAGA_LISTA_ESPERA', f'Abriu um horário com {nome_profissional}',
        f'O horário de {horario} com {nome_profissional} ficou livre. Quem agendar primeiro fica com a vaga.',
        {'lista_espera_id': lista_espera_id, 'profissional_id': profissional_id,
         'inicio': inicio.isoformat(), 'fim': fim.isoformat()})]
