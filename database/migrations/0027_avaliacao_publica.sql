-- Comentario da avaliacao aparece no perfil do profissional so com PUBLICA = TRUE; as anteriores (NULL) ficam privadas.
ALTER TABLE AVALIACAO ADD PUBLICA BOOLEAN DEFAULT FALSE;
