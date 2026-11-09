"""Prontuário: o profissional vê o diário de humor e sono de um paciente que ele atende.

Todo acesso é auditado (PRONTUARIO_ACESSO), porque é dado de saúde mental.
"""
from datetime import date

from database.db import transacao
from services.auditoria_service import auditar
from services.erros import ErroNegocio
from services.registros_service import COLUNAS, evolucao, inicio_periodo, serializar
from services.upload_service import upload_service


def prontuario(profissional_id, paciente_id, dias, antes):
    """Dados do paciente, última consulta realizada, resumo do período e uma página de registros."""
    with transacao() as cur:
        # O vínculo (uma consulta não cancelada entre os dois) precisa existir antes de ler qualquer dado
        cur.execute('''SELECT FIRST 1 1 FROM SESSAO
            WHERE PACIENTE_ID = ? AND PROFISSIONAL_ID = ?
            AND STATUS <> 'CANCELADO' ''', (paciente_id, profissional_id))
        if cur.fetchone() is None:
            raise ErroNegocio('Paciente sem vinculo de atendimento com este profissional', 403)

        cur.execute('SELECT NOME FROM USUARIO WHERE ID_USUARIO = ?', (paciente_id,))
        paciente = cur.fetchone()
        if paciente is None:
            raise ErroNegocio('Paciente nao encontrado', 404)
        auditar(cur, profissional_id, 'PRONTUARIO_ACESSO', 'USUARIO', paciente_id)

        cur.execute('''SELECT FIRST 1 DATA_HORA_INICIO, STATUS FROM SESSAO
            WHERE PACIENTE_ID = ? AND PROFISSIONAL_ID = ? AND STATUS = 'REALIZADO'
            ORDER BY DATA_HORA_INICIO DESC''', (paciente_id, profissional_id))
        ultima = cur.fetchone()

        periodo = evolucao(cur, paciente_id, dias)

        # 21 = os 20 da página + 1 só para saber se existe uma próxima página
        cur.execute(f'''SELECT FIRST 21 {', '.join(COLUNAS)} FROM REGISTRO
            WHERE USUARIO_ID = ? AND CRIADO_EM BETWEEN ? AND ? AND REGISTRO_ID < ?
            ORDER BY REGISTRO_ID DESC''', (paciente_id, inicio_periodo(dias), date.today(), antes))
        pagina = cur.fetchall()
        registros = [serializar(linha) for linha in pagina[:20]]

    nome_paciente = paciente[0]
    return {
        'paciente': {'id_usuario': paciente_id, 'nome': nome_paciente,
                     'foto_url': upload_service.url_foto_usuario(paciente_id)},
        'ultimaConsulta': {'data': ultima[0].isoformat(), 'status': ultima[1]} if ultima else None,
        **periodo,
        'registros': registros,
        'proximo_cursor': registros[-1]['registro_id'] if len(pagina) > 20 else None,
    }
