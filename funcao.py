"""Validadores e normalizadores puros, usados por vários serviços.

"Puro" quer dizer que a função só olha a entrada e devolve um resultado: não acessa banco, rede nem Flask.
Por isso dá para testar cada uma sozinha. Antes de escrever um validador novo, veja se ele já existe aqui.

Convenção dos normalizadores (`normalizar_*`): devolvem o valor no formato que vai para o banco,
ou None quando a entrada é inválida. Quem chama transforma o None em mensagem de erro.
"""
import json
import os
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------

def somente_digitos(valor):
    """'123.456-78' -> '12345678'."""
    return "".join(filter(str.isdigit, valor or ""))


def normalizar_texto(valor):
    """Tira acentos e põe em maiúsculas: 'Psicanálise' -> 'PSICANALISE'.

    Serve para comparar o que o usuário digitou com as opções fixas, sem se importar com acento ou caixa.
    NFKD separa cada letra do seu acento ('á' vira 'a' + '´'), e o filtro joga os acentos fora.
    """
    texto = unicodedata.normalize("NFKD", valor or "")
    return "".join(letra for letra in texto if not unicodedata.combining(letra)).upper()


# ---------------------------------------------------------------------------
# Cadastro de usuário: senha, documentos e contato
# ---------------------------------------------------------------------------

TIPOS_IMAGEM_PERMITIDOS = {"image/jpeg", "image/jpg"}
EXTENSOES_IMAGEM_PERMITIDAS = {".jpg", ".jpeg"}


def validar_senha(senha: str):
    """Senha de 8 a 12 caracteres com maiúscula, minúscula, número e caractere especial."""
    if not senha:
        return False

    maiuscula = minuscula = numero = especial = False
    for caractere in senha:
        if caractere.isupper():
            maiuscula = True
        elif caractere.islower():
            minuscula = True
        elif caractere.isdigit():
            numero = True
        elif not caractere.isalnum():
            especial = True

    if len(senha) < 8 or len(senha) > 12:
        return False
    return maiuscula and minuscula and numero and especial


def validar_cpf(cpf):
    """Confere os dois dígitos verificadores do CPF (aceita com ou sem pontuação)."""
    cpf = ''.join(filter(str.isdigit, cpf))

    # '111.111.111-11' passa na conta, mas não é um CPF válido
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False

    # Cálculo oficial: o 10º dígito sai dos 9 primeiros (pesos 10 a 2) e o 11º sai dos 10 primeiros (pesos 11 a 2)
    for posicao in range(9, 11):
        soma = sum(int(cpf[indice]) * (posicao + 1 - indice) for indice in range(posicao))
        digito = (soma * 10 % 11) % 10
        if digito != int(cpf[posicao]):
            return False

    return True


def validar_email(email):
    """Formato básico de e-mail: algo@dominio.ext."""
    padrao = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(padrao, email) is not None


def validar_telefone(telefone):
    """DDD + número, com ou sem parênteses, espaço e hífen: (11) 91234-5678, 1191234-5678..."""
    padrao = r'^\(?\d{2}\)?[\s-]?\d{4,5}-?\d{4}$'
    return re.match(padrao, telefone) is not None


def validar_nome(nome):
    """Nome de 3 a 150 caracteres, só com letras, espaço, ponto, apóstrofo e hífen."""
    if len(nome) < 3 or len(nome) > 150:
        return False

    caracteres_permitidos = set(" .'-")
    tem_letra = any(letra.isalpha() for letra in nome)
    return tem_letra and all(letra.isalpha() or letra in caracteres_permitidos for letra in nome)


def validar_imagem_usuario(imagem):
    """A foto de perfil é opcional; se vier, precisa ser JPEG (pela extensão e pelo tipo enviado)."""
    if not imagem or not imagem.filename:
        return True

    _, extensao = os.path.splitext(imagem.filename.lower())
    tipo = (imagem.content_type or "").lower()
    return extensao in EXTENSOES_IMAGEM_PERMITIDAS and tipo in TIPOS_IMAGEM_PERMITIDOS


# ---------------------------------------------------------------------------
# Perfil profissional
# ---------------------------------------------------------------------------

# Especialidade escolhida no cadastro -> papel do usuário e conselho de classe que ele precisa informar
ESPECIALIDADES_PROFISSIONAIS = {
    "Psicologia": {
        "usuario_role": "PSICOLOGO",
        "conselho_tipo": "CRP",
    },
    "Psiquiatria": {
        "usuario_role": "PSIQUIATRA",
        "conselho_tipo": "CRM",
    },
}

ABORDAGENS = ["TCC", "Psicanálise", "Humanista", "Gestalt", "Comportamental"]
MODALIDADES = {"ONLINE", "PRESENCIAL", "AMBAS"}
IDIOMAS = ["Português", "Inglês", "Espanhol", "Francês", "Italiano", "Alemão", "Libras"]
AREAS_ATUACAO = [
    "Ansiedade", "Depressão", "Estresse", "Burnout", "Autoestima", "Relacionamentos", "Família",
    "Luto", "Trauma", "TDAH", "Autismo", "Transtornos alimentares", "Dependência química",
    "Sexualidade", "Orientação profissional", "Infância e adolescência",
]
MAXIMO_AREAS_ATUACAO = 8


def _opcao(valor, opcoes):
    """Acha a opção oficial que bate com o valor, sem olhar acento nem caixa: 'psicanalise' -> 'Psicanálise'."""
    recebida = normalizar_texto(str(valor).strip())
    return next((nome for nome in opcoes if normalizar_texto(nome) == recebida), None)


def normalizar_abordagem(valor):
    return _opcao(valor, ABORDAGENS)


def normalizar_modalidade(valor):
    """'online' -> 'ONLINE'. Aceita ONLINE, PRESENCIAL ou AMBAS."""
    modalidade = normalizar_texto(str(valor).strip())
    return modalidade if modalidade in MODALIDADES else None


def normalizar_lista(valor, opcoes, maximo):
    """Lista, texto JSON ('["a","b"]') ou texto separado por vírgula -> 'Opcao1,Opcao2'.

    Cada item precisa ser uma das `opcoes`; repetidos são ignorados. None se algum item for inválido,
    se a lista ficar vazia ou se passar de `maximo` itens. No banco a lista fica como texto com vírgulas.
    """
    if isinstance(valor, str):
        texto = valor.strip()
        if texto.startswith("["):
            try:
                valor = json.loads(texto)
            except json.JSONDecodeError:
                return None
        else:
            valor = texto.split(",")
    if not isinstance(valor, list):
        return None

    itens = []
    for item in valor:
        if not isinstance(item, str):
            return None
        if not item.strip():
            continue
        nome = _opcao(item, opcoes)
        if nome is None:
            return None
        if nome not in itens:
            itens.append(nome)

    if not itens or len(itens) > maximo:
        return None
    return ",".join(itens)


def normalizar_ano_inicio(valor):
    """Ano em que o profissional começou a atuar, entre 1950 e o ano atual."""
    # True e False são int em Python (True == 1): sem isso, true no JSON viraria o ano 1
    if isinstance(valor, bool):
        return None
    try:
        ano = int(str(valor).strip())
    except ValueError:
        return None
    return ano if 1950 <= ano <= date.today().year else None


def normalizar_formacao(valor):
    """Texto livre da formação, de 3 a 1000 caracteres."""
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    return texto if 3 <= len(texto) <= 1000 else None


def normalizar_video_url(valor):
    """Link do YouTube ou do Vimeo -> link de embed; None se não for um deles.

    Embed é o endereço próprio para tocar o vídeo dentro de um <iframe> no perfil. Guardar sempre
    o embed impede que o perfil mostre um site qualquer dentro da página.
    """
    if not isinstance(valor, str) or len(valor) > 500:
        return None
    try:
        partes = urlsplit(valor.strip())
    except ValueError:
        return None
    if partes.scheme not in ("http", "https"):
        return None
    host = (partes.hostname or "").removeprefix("www.").removeprefix("m.")
    caminho = [parte for parte in partes.path.split("/") if parte]

    if host in ("youtube.com", "youtube-nocookie.com", "youtu.be"):
        # O id do vídeo aparece em lugares diferentes conforme o formato do link
        video = None
        if host == "youtu.be" and caminho:
            video = caminho[0]                                    # youtu.be/<id>
        elif caminho == ["watch"]:
            video = parse_qs(partes.query).get("v", [""])[0]      # youtube.com/watch?v=<id>
        elif len(caminho) == 2 and caminho[0] in ("embed", "shorts", "live"):
            video = caminho[1]                                    # youtube.com/embed/<id>
        if video and re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            return f"https://www.youtube-nocookie.com/embed/{video}"
        return None

    if host == "player.vimeo.com" and len(caminho) == 2 and caminho[0] == "video":
        numero, hash_privado = caminho[1], parse_qs(partes.query).get("h", [""])[0]
    elif host == "vimeo.com" and caminho:
        # Vídeo não listado: vimeo.com/<numero>/<hash>
        numero, hash_privado = caminho[0], caminho[1] if len(caminho) == 2 else ""
    else:
        return None
    if not numero.isdigit() or (hash_privado and not re.fullmatch(r"[0-9a-f]{6,20}", hash_privado)):
        return None
    return f"https://player.vimeo.com/video/{numero}" + (f"?h={hash_privado}" if hash_privado else "")


def normalizar_valor_sessao(valor):
    """Valor em reais digitado pelo usuário -> centavos inteiros, como fica no banco.

    Aceita 'R$ 1.500,50', '1500,50' e '1500.50'. None se não for um valor positivo de até R$ 100.000
    com no máximo duas casas decimais. Usa Decimal, e não float, porque float erra conta de dinheiro
    (0.1 + 0.2 dá 0.30000000000000004).
    """
    texto = str(valor).replace("R$", "").replace(" ", "")

    if "," in texto and "." in texto:
        # Formato brasileiro com milhar: o ponto separa milhar e a vírgula separa centavos
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")

    try:
        valor_decimal = Decimal(texto)
    except InvalidOperation:
        return None

    if not valor_decimal.is_finite() or valor_decimal <= 0 or valor_decimal > Decimal("100000"):
        return None

    centavos = valor_decimal * 100
    # Sobrou fração de centavo (ex.: 10,555): mais de duas casas decimais
    if centavos != centavos.to_integral_value():
        return None

    return int(centavos)


def normalizar_conselho(valor, conselho_tipo):
    """Número do conselho de classe no formato padrão.

    CRP (psicólogo): região + número, '06/123456'. CRM (psiquiatra): 'SP/123456' ou só '123456'.
    """
    texto = valor.upper().strip()
    texto = texto.replace(" ", "")

    if conselho_tipo == "CRP":
        texto = re.sub(r"^CRP", "", texto)
        if not re.fullmatch(r"\d{2}/?\d{4,6}", texto):
            return None
        digitos = somente_digitos(texto)
        return f"{digitos[:2]}/{digitos[2:]}"

    if conselho_tipo == "CRM":
        texto = re.sub(r"^CRM", "", texto)
        texto = texto.lstrip("/")
        com_uf = re.fullmatch(r"([A-Z]{2})/?(\d{4,6})", texto)
        if com_uf:
            uf, numero = com_uf.groups()
            return f"{uf}/{numero}"
        if re.fullmatch(r"\d{4,6}", texto):
            return texto

    return None


# ---------------------------------------------------------------------------
# Formulário de cadastro (multipart, enviado pelo front)
# ---------------------------------------------------------------------------

# Dia digitado (já sem acento e em maiúsculas) -> nome gravado no banco
DIAS_ATENDIMENTO = {
    "SEGUNDA": "Segunda",
    "TERCA": "Terca",
    "QUARTA": "Quarta",
    "QUINTA": "Quinta",
    "SEXTA": "Sexta",
    "SABADO": "Sabado",
    "DOMINGO": "Domingo",
}


def obter_campo(formulario, *nomes):
    """Primeiro campo preenchido entre os `nomes` (sem espaços nas pontas), ou '' se nenhum veio.

    O front já mandou o mesmo dado com nomes diferentes (ex.: 'crp', 'crm', 'conselho'), por isso aceita vários.
    """
    for nome in nomes:
        valor = formulario.get(nome)
        if valor is None:
            continue
        if isinstance(valor, str):
            valor = valor.strip()
        if valor != "":
            return valor
    return ""


def obter_dias_atendimento(formulario):
    """Lê os dias de atendimento do cadastro. Devolve (lista de dias, None) ou (None, mensagem de erro).

    Aceita os dias repetindo a chave (dias=Segunda&dias=Terca), num texto JSON ('["Segunda"]')
    ou separados por vírgula ('Segunda,Terca').
    """
    valores = []
    for campo in ("dias", "dias[]", "dias_atendimento", "diasAtendimento"):
        valores.extend(formulario.getlist(campo))

    dias_recebidos = []
    for valor in valores:
        if not valor:
            continue
        texto = valor.strip()
        if not texto:
            continue

        if texto.startswith("["):
            try:
                lista = json.loads(texto)
            except json.JSONDecodeError:
                return None, "Dias de atendimento devem ser uma lista valida"
            if not isinstance(lista, list):
                return None, "Dias de atendimento devem ser uma lista"
            dias_recebidos.extend(str(item) for item in lista)
            continue

        dias_recebidos.extend(parte.strip() for parte in texto.split(","))

    if not dias_recebidos:
        return None, "Selecione ao menos um dia de atendimento"

    dias_normalizados = []
    chaves_usadas = set()
    for dia in dias_recebidos:
        chave = normalizar_texto(dia)
        if chave not in DIAS_ATENDIMENTO:
            return None, "Dia de atendimento invalido"
        if chave in chaves_usadas:
            return None, "Dias de atendimento nao podem se repetir"
        chaves_usadas.add(chave)
        dias_normalizados.append(DIAS_ATENDIMENTO[chave])

    return dias_normalizados, None


# ---------------------------------------------------------------------------
# Formatação para mensagens (notificações, e-mails e WhatsApp)
# ---------------------------------------------------------------------------

def formatar_reais(centavos):
    """15000 -> 'R$ 150,00'. No banco o dinheiro fica sempre em centavos inteiros."""
    return f'R$ {centavos / 100:.2f}'.replace('.', ',')


def formatar_data_hora(momento):
    """datetime -> '09/11/2026 às 14:00'."""
    return momento.strftime('%d/%m/%Y às %H:%M')
