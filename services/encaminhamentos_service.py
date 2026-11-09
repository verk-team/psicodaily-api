import re

from database.db import nomes_das_colunas, transacao
from services.auditoria_service import auditar
from services.erros import ErroNegocio

CAMPOS = {'id_usuario', 'motivo', 'id_cid'}
MAXIMO_MOTIVO = 2000
# CID-10: letra, dois digitos e subcategoria opcional (F32, F32.1, F321)
CID = re.compile(r'^[A-Z]\d{2}(\.?\d{1,2})?$')


def _validar(dados):
    if not isinstance(dados, dict):
        raise ErroNegocio('Formato inválido')
    extras = set(dados) - CAMPOS
    if extras:
        raise ErroNegocio(f"Campos não permitidos: {', '.join(sorted(extras))}")
    paciente_id, motivo, cid = dados.get('id_usuario'), dados.get('motivo'), dados.get('id_cid')
    if paciente_id is None or motivo is None:
        raise ErroNegocio('id_usuario e motivo são obrigatórios')
    if not isinstance(paciente_id, int) or isinstance(paciente_id, bool):
        raise ErroNegocio('id_usuario deve ser um número')
    if not isinstance(motivo, str) or not 1 <= len(motivo.strip()) <= MAXIMO_MOTIVO:
        raise ErroNegocio(f'motivo deve ter de 1 a {MAXIMO_MOTIVO} caracteres')
    if cid is not None and cid != '':
        if not isinstance(cid, str) or not CID.match(cid.strip().upper()):
            raise ErroNegocio('id_cid deve ser um código CID-10, ex.: F32.1')
        cid = cid.strip().upper()
    return paciente_id, motivo.strip(), cid or None


def criar(profissional_id, dados):
    paciente_id, motivo, cid = _validar(dados)

    with transacao() as cur:
        # Só encaminha paciente com quem tem vínculo (sessão não cancelada); 404 também esconde se o ID existe
        cur.execute("""SELECT FIRST 1 1 FROM SESSAO S JOIN USUARIO U ON U.ID_USUARIO = S.PACIENTE_ID
            WHERE S.PACIENTE_ID = ? AND S.PROFISSIONAL_ID = ? AND S.STATUS <> 'CANCELADO'
            AND U.USUARIO_ROLE = 'PACIENTE'""", (paciente_id, profissional_id))
        if cur.fetchone() is None:
            raise ErroNegocio('Paciente não encontrado entre os seus atendimentos', 404)
        cur.execute("""
        INSERT INTO ENCAMINHAMENTO(PACIENTE_ID, PSICOLOGO_ID, CID, MOTIVO, STATUS)
        VALUES(?, ?, ?, ?, ?) RETURNING *
        """, (paciente_id, profissional_id, cid, motivo, 'ATIVO'))
        linha = cur.fetchone()
        encaminhamento = dict(zip(nomes_das_colunas(cur), linha))
        auditar(cur, profissional_id, 'ENCAMINHAMENTO_CRIADO', 'ENCAMINHAMENTO', encaminhamento['encaminhamento_id'])
        return encaminhamento


def listar(profissional_id):
    """Só os encaminhamentos feitos por este profissional (S5)."""
    with transacao() as cur:
        cur.execute('SELECT * FROM ENCAMINHAMENTO WHERE PSICOLOGO_ID = ? ORDER BY CRIADO_EM DESC', (profissional_id,))
        colunas = nomes_das_colunas(cur)
        return [dict(zip(colunas, linha)) for linha in cur.fetchall()]
