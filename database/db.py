"""Conexão com o Firebird: pool de conexões e o `with transacao() as cur:` que os serviços usam.

Termos:
- conexão: o "canal" aberto entre a API e o banco. Abrir uma custa caro (~50 ms).
- cursor (`cur`): o objeto que executa SQL numa conexão e traz as linhas do resultado.
- transação: um grupo de comandos que vale junto. Commit grava tudo; rollback desfaz tudo.
- pool: uma fila de conexões já abertas, reaproveitadas entre as requisições em vez de abrir uma nova a cada vez.
"""
import os
import queue
from contextlib import contextmanager

import fdb
from dotenv import load_dotenv

load_dotenv()

# Abrir conexão no Firebird custa ~50 ms e, com várias ao mesmo tempo (uma tela dispara consultas + financeiro
# + um avatar por paciente), algumas esperavam 1 s para conectar. Por isso as conexões ficam guardadas aqui.
# LIFO: a última conexão devolvida é a primeira a sair de novo. No máximo 10 ficam guardadas.
_livres = queue.LifoQueue(maxsize=10)


class ConexaoDoPool:
    """Embrulho de uma conexão do fdb: funciona igual a ela, mas o close() devolve a conexão ao pool.

    Assim quem usa get_connection() continua chamando con.close() normalmente, sem saber que existe um pool.
    """

    def __init__(self, con):
        self._con = con

    def __getattr__(self, nome):
        # Qualquer outro atributo (cursor, commit, rollback...) vai direto para a conexão de verdade
        return getattr(self._con, nome)

    def close(self):
        # Tira a conexão do embrulho, para um segundo close() não devolver a mesma conexão duas vezes
        con, self._con = self._con, None
        if con is None:
            return
        try:
            # Desfaz o que não foi commitado e fecha os cursores abertos antes de guardar
            con.rollback()
            _livres.put_nowait(con)
        except Exception:
            # Pool cheio ou conexão quebrada: fecha de vez
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
    """Uma conexão do pool ou, se o pool estiver vazio, uma nova. Quem chama precisa dar commit e close()."""
    while True:
        try:
            con = _livres.get_nowait()
        except queue.Empty:
            return ConexaoDoPool(_conectar())
        try:
            # Se o Firebird reiniciou, a conexão guardada morreu: descarta e tenta a próxima
            cur = con.cursor()
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            cur.close()
            return ConexaoDoPool(con)
        except Exception:
            _fechar(con)


@contextmanager
def transacao():
    """Abre uma transação e entrega o cursor:

        with transacao() as cur:
            cur.execute(...)

    Se o bloco termina sem exceção, faz commit. Se der exceção (inclusive ErroNegocio), nada é gravado:
    o close() faz rollback e devolve a conexão ao pool.
    """
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
            # RDB$DATABASE é uma tabela de sistema com uma linha só: serve para testar a conexão
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            return cur.fetchone() is not None
    except Exception:
        return False
