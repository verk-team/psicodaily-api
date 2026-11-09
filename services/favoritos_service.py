"""Profissionais favoritos do paciente, cada um com o próximo horário livre."""
from database.db import transacao
from services import disponibilidade_service
from services.erros import ErroNegocio
from services.upload_service import upload_service


def _exigir_paciente(papel):
    if papel != 'PACIENTE':
        raise ErroNegocio('Favoritos sao exclusivos do paciente', 403)


def listar(paciente_id, papel):
    _exigir_paciente(papel)
    with transacao() as cur:
        cur.execute('''SELECT f.PROFISSIONAL_ID, u.NOME, p.ESPECIALIDADE, p.PRECO_HORA, f.CRIADO_EM
            FROM FAVORITO f
            JOIN PROFISSIONAL p ON p.USUARIO_ID = f.PROFISSIONAL_ID
            JOIN USUARIO u ON u.ID_USUARIO = p.USUARIO_ID
            WHERE f.PACIENTE_ID = ? AND u.ATIVO = TRUE
            ORDER BY f.CRIADO_EM DESC''', (paciente_id,))
        linhas = cur.fetchall()
    favoritos = []
    for profissional_id, nome, especialidade, preco_hora, criado_em in linhas:
        # ponytail: uma consulta de agenda por favorito; juntar numa so se a lista crescer muito
        livres = disponibilidade_service.horarios_livres(profissional_id)['horarios']
        favoritos.append({
            'id_profissional': profissional_id,
            'nome': nome,
            'especialidade': especialidade,
            'preco_hora': preco_hora,
            'foto_url': upload_service.url_foto_usuario(profissional_id),
            'favoritado_em': criado_em.isoformat(),
            'proximo_horario': livres[0] if livres else None,
        })
    return favoritos


def favoritar(paciente_id, papel, profissional_id):
    """Devolve True se criou, False se ja era favorito."""
    _exigir_paciente(papel)
    with transacao() as cur:
        cur.execute('''SELECT 1 FROM PROFISSIONAL p JOIN USUARIO u ON u.ID_USUARIO = p.USUARIO_ID
            WHERE p.USUARIO_ID = ? AND u.ATIVO = TRUE''', (profissional_id,))
        if cur.fetchone() is None:
            raise ErroNegocio('Profissional não encontrado', 404)
        cur.execute('SELECT 1 FROM FAVORITO WHERE PACIENTE_ID = ? AND PROFISSIONAL_ID = ?', (paciente_id, profissional_id))
        if cur.fetchone():
            return False
        cur.execute('INSERT INTO FAVORITO (PACIENTE_ID, PROFISSIONAL_ID) VALUES (?, ?)', (paciente_id, profissional_id))
        return True


def desfavoritar(paciente_id, papel, profissional_id):
    _exigir_paciente(papel)
    with transacao() as cur:
        cur.execute('DELETE FROM FAVORITO WHERE PACIENTE_ID = ? AND PROFISSIONAL_ID = ?', (paciente_id, profissional_id))
        if cur.rowcount == 0:
            raise ErroNegocio('Profissional nao esta nos favoritos', 404)
