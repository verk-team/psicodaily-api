import os
import queue
from contextlib import contextmanager
from dotenv import load_dotenv
import fdb

load_dotenv()

# Abrir conexao no Firebird custa ~50ms e, com varias ao mesmo tempo (uma tela dispara
# consultas + financeiro + um avatar por paciente), algumas esperavam 1s pelo SYN.
# As rotas continuam chamando con.close(); a conexao so volta para ca.
_livres = queue.LifoQueue(maxsize=10)


class ConexaoDoPool:
    def __init__(self, con):
        self._con = con

    def __getattr__(self, nome):
        return getattr(self._con, nome)

    def close(self):
        con, self._con = self._con, None
        if con is None:
            return
        try:
            # Descarta o que a rota nao commitou e fecha os cursores abertos
            con.rollback()
            _livres.put_nowait(con)
        except Exception:
            _fechar(con)


def _fechar(con):
    try:
        con.close()
    except Exception:
        pass


def _conectar():
    try:
        return fdb.connect(
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            database=os.getenv("DB_DATABASE"),
            charset=os.getenv("DB_CHARSET", "UTF8"),
        )
    except Exception as e:
        print(f"erro ao conectar ao banco: {str(e)}")
        raise RuntimeError("Erro ao conectar ao banco")


def get_connection():
    while True:
        try:
            con = _livres.get_nowait()
        except queue.Empty:
            return ConexaoDoPool(_conectar())
        try:
            # Se o Firebird reiniciou, a conexao guardada morreu: descarta e tenta a proxima
            cur = con.cursor()
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            cur.close()
            return ConexaoDoPool(con)
        except Exception:
            _fechar(con)


@contextmanager
def transacao():
    # Commit so se o bloco terminar sem excecao; close() desfaz o resto e devolve ao pool.
    con = get_connection()
    cur = con.cursor()
    try:
        yield cur
        con.commit()
    finally:
        cur.close()
        con.close()


def nomes_das_colunas(cur):
    """Nomes das colunas da última consulta do cursor, em minúsculas. Viram as chaves do JSON."""
    return [coluna[0].lower() for coluna in cur.description]


def banco_responde():
    """Health check: True se o Firebird responde a uma consulta."""
    try:
        with transacao() as cur:
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            return cur.fetchone() is not None
    except Exception:
        return False
