# Transcrição das consultas (tarefa 11)

> Escopo: este repositório, mais o contrato que o `psicodaily-front` precisa seguir (seção "Contrato para o front").
> Decidido com o time em 02/11/2026. **Adiado no mesmo dia**: custo alto para a pontuação da sprint. Nada foi implementado.

## Decisões

- **Compliance fora do escopo.** O projeto é de curso técnico e só usa dados fictícios: sem parecer de CFP/LGPD, política de retenção nem registro formal de consentimento. Se algum dia entrar paciente real, este plano volta para a mesa.
- **O que fica:** (1) controle de acesso: só o profissional da sessão lê a transcrição; (2) aviso na tela para os dois enquanto grava; (3) a segurança que já existe: bucket privado (F6), auditoria (F5), chave só no `.env`.
- **Quem grava é o navegador do profissional.** A chamada é P2P (o áudio vai direto de um navegador ao outro), então o servidor nunca recebe o som. O front mistura o microfone do profissional com o áudio do paciente e grava com o [`MediaRecorder`](https://developer.mozilla.org/docs/Web/API/MediaRecorder) (API do navegador que grava um stream em arquivo). É uma gravação por sessão.
- **Quem transcreve é o Gemini**, que o assistente já usa (mesma `GEMINI_API_KEY`, `urllib`, sem dependência nova). Usamos o endpoint nativo `generateContent`, não o compatível com OpenAI do assistente, porque o nativo aceita `audio/webm` (o formato que o Chrome grava) e o compatível só documenta WAV. O limite é de 20 MB por requisição, com o áudio em base64 (texto que cresce ~33%), então o upload aceita até **14 MB**. Gravando a 16 kbps, isso dá ~1h55 de consulta.
- **A transcrição roda na thread de lembretes, não na requisição.** Transcrever 50 minutos leva minutos. A rota só guarda o áudio e responde **202** (pedido aceito, processado depois). A thread que já roda a cada 60 s pega as pendentes, e o que estava pendente num restart continua na volta.
- **O áudio é apagado do bucket quando a transcrição termina**, pronta ou com erro. Só o texto fica, e não sobra gravação de voz sem dono.
- **Rótulos de quem fala:** a gravação é uma mistura das duas vozes. O Gemini separa as falas por contexto (diarização: identificar quem fala em cada trecho) e usa "Profissional"/"Paciente". Quando não dá para saber, usa "Falante 1"/"Falante 2".
- **Plano gratuito do Gemini:** o Google pode usar o conteúdo enviado para melhorar os produtos dele. Com dados fictícios não tem problema; com pacientes reais, só no plano pago.

## Fluxo

```
profissional clica "Transcrever"  ──WS {"type":"gravacao","ativa":true}──►  paciente vê o aviso
        │ MediaRecorder grava (mic do profissional + áudio do paciente)
encerrar ──► POST /api/consultas/<id>/transcricao (multipart "audio") ──► R2 privado + TRANSCRICAO PENDENTE (202)
                                                                              │
thread de lembretes (60 s) ──► baixa o áudio ──► Gemini ──► TEXTO, STATUS PRONTA ──► apaga o áudio + notifica
                                                     └─ falhou 5 vezes ──► STATUS ERRO ──► apaga o áudio + notifica
profissional ──► GET /api/consultas/<id>/transcricao ──► texto (auditado)
```

## Passos

### 1. Migration `database/migrations/0025_transcricao.sql`

```sql
-- Tarefa 11: transcricao da consulta, uma por sessao. O audio so fica no bucket privado ate ser transcrito.
CREATE TABLE TRANSCRICAO (SESSAO_ID INTEGER NOT NULL,
        STATUS VARCHAR(10) DEFAULT 'PENDENTE' NOT NULL,
        AUDIO_CHAVE VARCHAR(200),
        AUDIO_TIPO VARCHAR(20) NOT NULL,
        TEXTO BLOB SUB_TYPE TEXT CHARACTER SET UTF8,
        TENTATIVAS SMALLINT DEFAULT 0 NOT NULL,
        CRIADO_EM TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
        CONCLUIDA_EM TIMESTAMP,
CONSTRAINT PK_TRANSCRICAO PRIMARY KEY (SESSAO_ID),
CONSTRAINT CK_TRANSCRICAO_STATUS CHECK (STATUS IN ('PENDENTE', 'PRONTA', 'ERRO')));
ALTER TABLE TRANSCRICAO ADD CONSTRAINT FK_TRANSCRICAO_SESSAO FOREIGN KEY (SESSAO_ID) REFERENCES SESSAO (SESSAO_ID) ON DELETE CASCADE;
```

- A chave primária em `SESSAO_ID` garante uma transcrição por sessão.
- `AUDIO_CHAVE` vira `NULL` quando o áudio é apagado.
- Não há `PROFISSIONAL_ID`: ele vem de `SESSAO`.
- O `ON DELETE CASCADE` faz o fixture `sessoes` dos testes limpar a transcrição junto.

### 2. `services/upload_service.py`: ler e apagar do bucket privado

```python
def ler_privado(self, chave):
    return self.s3.get_object(Bucket=self.bucket_privado, Key=chave)['Body'].read()

def apagar_privado(self, chave):
    self.s3.delete_object(Bucket=self.bucket_privado, Key=chave)
```

### 3. `services/transcricao_service.py` (novo)

Constantes:

```python
MAXIMO_AUDIO = 14 * 1024 * 1024  # em base64 vira ~18,7 MB; o Gemini aceita ate 20 MB por requisicao
MAXIMO_TENTATIVAS = 5            # o plano gratuito devolve 429/503 com frequencia; a thread tenta de novo a cada 60 s
POR_RODADA = 3                   # cada audio leva minutos e segura a thread dos lembretes
URL_GEMINI = f'https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:generateContent'  # MODELO do assistente
```

**Tipo pelos primeiros bytes**, como no chat (`ASSINATURAS` em `chat_service.py`). Nunca usar o nome do arquivo nem o Content-Type que o cliente manda:

| Início do arquivo | Tipo enviado ao Gemini | Quem grava assim |
|---|---|---|
| `b'\x1aE\xdf\xa3'` | `audio/webm` | Chrome, Edge, Firefox |
| `b'OggS'` | `audio/ogg` | Firefox (opcional) |
| `conteudo[4:8] == b'ftyp'` | `audio/m4a` | Safari, que grava MP4 (o M4A usa o mesmo contêiner) |

Qualquer outro início dá 400 `'Envie o audio gravado pela chamada (WebM, OGG ou MP4)'`.

**`enviar(profissional_id, sessao_id, arquivo)`**, mesmo desenho de `chat_service.enviar_arquivo`:

1. Sem arquivo dá 400. Ler `MAXIMO_AUDIO + 1` bytes: se passar do limite, 413. Depois detectar o tipo.
2. Na transação 1:
   - `SELECT STATUS, DATA_HORA_INICIO FROM SESSAO WHERE SESSAO_ID = ? AND PROFISSIONAL_ID = ?`, com o profissional **do token**. Sem linha, 404 `'Sessao nao encontrada'`.
   - `CANCELADO` dá 409.
   - Se o início ainda está a mais de 15 min, 409 `'A consulta ainda nao comecou'` (a sala abre 15 min antes, igual a `sala_da_sessao`).
   - Se já existe linha em `TRANSCRICAO`, 409 `'Esta consulta ja tem transcricao'`.
3. Fora de transação (para não prender uma conexão do pool durante o upload): `chave = f'transcricoes/{sessao_id}/{uuid4().hex}{extensao}'` e `upload_service.enviar_privado(conteudo, chave, tipo)`.
4. Na transação 2: `INSERT INTO TRANSCRICAO (SESSAO_ID, AUDIO_CHAVE, AUDIO_TIPO)` e `auditar(cur, profissional_id, 'TRANSCRICAO_ENVIADA', 'SESSAO', sessao_id)`.
   - Se der `fdb.DatabaseError` com `PK_TRANSCRICAO` (dois envios ao mesmo tempo), `apagar_privado(chave)` e 409. É o mesmo padrão de `consultas_service.avaliar`.
5. Devolve `{'sessao_id': sessao_id, 'status': 'PENDENTE'}`.

**`consultar(profissional_id, sessao_id)`**:

- Busca a transcrição com `JOIN SESSAO s ... WHERE t.SESSAO_ID = ? AND s.PROFISSIONAL_ID = ?`. Sem linha, 404 (de propósito, não diz se a transcrição existe).
- **Cuidado com o `fdb`:** um BLOB acima de 64 KB não volta como `str`, volta como `BlobReader`, um objeto que só pode ser lido enquanto a transação está aberta. Uma consulta de 2 h passa disso. Dentro do `with transacao()`, fazer `texto = texto.read() if hasattr(texto, 'read') else texto`.
- Se o status for `PRONTA`, `auditar(cur, profissional_id, 'TRANSCRICAO_ACESSO', 'SESSAO', sessao_id)`, na mesma transação da leitura.
- Devolve `{'sessao_id', 'status', 'texto', 'criado_em', 'concluida_em'}`. `texto` só vem preenchido em `PRONTA`. As datas saem em `isoformat()`.

**`processar_pendentes()`** (chamada pela thread):

1. Transação curta: `SELECT FIRST 3` das `PENDENTE`, por `CRIADO_EM`, com `AUDIO_CHAVE`, `AUDIO_TIPO`, `TENTATIVAS`, o `PROFISSIONAL_ID`, `DATA_HORA_INICIO` da sessão e o nome do paciente (para o aviso).
2. Para cada uma, **fora de transação**: `audio = upload_service.ler_privado(chave)` e depois `texto = _transcrever(audio, tipo)`.
   - Usar `try/except Exception` com `logger.exception` por item, para que um áudio ruim não trave os outros.
3. **Deu certo:**
   - Fazer `UPDATE ... SET STATUS = 'PRONTA', TEXTO = ?, AUDIO_CHAVE = NULL, CONCLUIDA_EM = CURRENT_TIMESTAMP` e `notificar(cur, profissional_id, 'TRANSCRICAO_PRONTA', 'Transcrição pronta', 'A transcrição da consulta com {paciente} de {quando} está pronta na tela da consulta.', {'sessao_id': ...})`.
   - Depois do commit: `apagar_privado(chave)` e `enviar_emails(...)`.
4. **Falhou:**
   - Se `TENTATIVAS + 1 < MAXIMO_TENTATIVAS`: `UPDATE ... SET TENTATIVAS = TENTATIVAS + 1`. A próxima rodada tenta de novo.
   - Senão: `STATUS = 'ERRO'`, `AUDIO_CHAVE = NULL`, `CONCLUIDA_EM` e `notificar(..., 'TRANSCRICAO_FALHOU', 'Transcrição indisponível', ...)`. Depois do commit, `apagar_privado` e `enviar_emails`.
   - A mensagem de notificação **nunca leva trecho do texto**: ela também sai por e-mail e WhatsApp.
5. Devolve quantas processou (para os testes).

**`_transcrever(audio, tipo)`**, uma ida ao Gemini, no estilo de `assistente_service._chamar` (os testes trocam essa função por uma falsa):

```python
corpo = {
    'contents': [{'parts': [
        {'text': PEDIDO},
        {'inline_data': {'mime_type': tipo, 'data': base64.b64encode(audio).decode()}},
    ]}],
    # Consulta de saude mental fala de automutilacao e suicidio; com o filtro padrao o Gemini pode cortar o trecho
    'safetySettings': [{'category': c, 'threshold': 'BLOCK_NONE'} for c in (
        'HARM_CATEGORY_HARASSMENT', 'HARM_CATEGORY_HATE_SPEECH',
        'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'HARM_CATEGORY_DANGEROUS_CONTENT')],
}
```

- Cabeçalhos: `x-goog-api-key: GEMINI_API_KEY`, `Content-Type: application/json` e `User-Agent: psicodaily-api`. Usar `urlopen(..., timeout=300)`.
- Resposta:
  - Juntar o `text` de `candidates[0].content.parts`.
  - `finishReason == 'MAX_TOKENS'`: grava o que veio, mais a linha `[transcrição cortada: limite de tamanho do modelo]`.
  - Qualquer outro motivo que não seja `STOP`, ou texto vazio, conta como falha.
- `PEDIDO`:

  > Transcreva este áudio de uma consulta de psicologia em português do Brasil. Uma linha por fala, no formato `[MM:SS] Falante: texto`. Use Profissional e Paciente quando der para saber pelo contexto (o profissional conduz a consulta); se não der, Falante 1 e Falante 2. Escreva só o que foi dito, sem resumir, comentar ou corrigir. Trechos que não dá para entender: `[inaudível]`.

### 4. Ações de auditoria e tipos de notificação

- `auditoria_service.ACOES`: `'TRANSCRICAO_ENVIADA'` e `'TRANSCRICAO_ACESSO'`.
- `notificacoes_service.TIPOS`: `'TRANSCRICAO_PRONTA'` e `'TRANSCRICAO_FALHOU'`.

### 5. Thread de lembretes (`services/lembretes_service.py`)

- Em `_rodar`, um terceiro bloco `try: transcricao_service.processar_pendentes() except Exception: logger.exception(...)`, igual aos dois que já existem.
- Atualizar a docstring de `iniciar()` (lembretes, assinaturas e transcrições) e o comentário do `app.py` sobre `JOBS`.

### 6. Rotas (`view/consultas.py`)

```python
@consultas_bp.route('/<int:sessao_id>/transcricao', methods=['POST'])
@profissional_autenticado
def enviar_transcricao(dados_usuario, sessao_id):
    """multipart: `audio` (WebM, OGG ou MP4 de ate 14 MB) gravado pelo front do profissional."""
    transcricao = transcricao_service.enviar(dados_usuario['id_usuario'], sessao_id, request.files.get('audio'))
    return jsonify({'transcricao': transcricao}), 202


@consultas_bp.route('/<int:sessao_id>/transcricao', methods=['GET'])
@profissional_autenticado
def transcricao(dados_usuario, sessao_id):
    resposta = jsonify({'transcricao': transcricao_service.consultar(dados_usuario['id_usuario'], sessao_id)})
    # Texto de consulta nao fica em cache de navegador nem de proxy, como as mensagens do chat
    resposta.headers['Cache-Control'] = 'private, no-store'
    return resposta
```

Paciente e anônimo recebem 403 e 401 do próprio decorator. O assistente de IA **não** ganha ferramenta para ler transcrições.

### 7. Aviso de gravação na sala (`services/signaling_service.py`)

`RELAYED = {"offer", "answer", "ice-candidate", "gravacao"}`, com um comentário dizendo que `gravacao` é o aviso da tarefa 11.

O servidor só repassa o evento ao outro participante. Se um paciente mandar um `gravacao` falso, o efeito é só mostrar um aviso na tela do profissional, então não vale checar o papel de quem envia.

### 8. Documentação

- `.claude/architecture.md`:
  - Rotas: as duas de `/api/consultas/<id>/transcricao`.
  - Modelo de dados: tabela `TRANSCRICAO`.
  - Fluxos: o diagrama acima, resumido.
  - Pontos de atenção: a thread de lembretes fica presa enquanto transcreve; plano gratuito do Gemini e uso dos dados.
- `.claude/plans/ultima-sprint.md`: na linha da tarefa 11, trocar "Decisão ética/legal antes de tudo" por "Feito no backend (data)", com link para este plano.

## Contrato para o front (`psicodaily-front`, tela da chamada)

1. **Só o profissional** vê o botão "Transcrever consulta". Antes de gravar, uma confirmação: "O paciente verá um aviso de que a consulta está sendo gravada."
2. **Gravar as duas vozes juntas:**
   ```js
   const ctx = new AudioContext()
   const destino = ctx.createMediaStreamDestination()
   ctx.createMediaStreamSource(streamLocal).connect(destino)
   ctx.createMediaStreamSource(streamRemoto).connect(destino) // repetir a cada ontrack (reconexão)
   const tipo = ['audio/webm;codecs=opus', 'audio/mp4'].find(t => MediaRecorder.isTypeSupported(t))
   const gravador = new MediaRecorder(destino.stream, { mimeType: tipo, audioBitsPerSecond: 16000 })
   const partes = []
   gravador.ondataavailable = e => partes.push(e.data)
   gravador.start(10000) // entrega pedaços a cada 10 s
   ```
   No Chrome, o áudio remoto só chega ao `AudioContext` se também estiver tocando num `<video>`/`<audio>`, o que a tela da chamada já faz.
3. **Aviso:**
   - O profissional manda `{"type": "gravacao", "ativa": true}` pelo WS do signaling ao começar e de novo a cada `peer-joined`, para quem entrar depois também ver.
   - Ao parar, manda `{"type": "gravacao", "ativa": false}`.
   - O paciente mostra uma faixa fixa "Esta consulta está sendo gravada para transcrição" enquanto `ativa` for `true`.
4. **Enviar ao encerrar:**
   - `new Blob(partes, { type: gravador.mimeType })` vai no campo `audio` de um `FormData`, para `POST /api/consultas/<id>/transcricao`.
   - 413 significa gravação longa demais; 409 significa que a consulta já tem transcrição.
5. **Tela da consulta (profissional):** `GET /api/consultas/<id>/transcricao`.
   - 404: sem transcrição.
   - `PENDENTE`: "processando".
   - `PRONTA`: mostrar `texto` com `white-space: pre-wrap`.
   - `ERRO`: "não foi possível transcrever".

## Testes (`tests/test_transcricao.py`)

Fixtures:
- **bucket em memória:** o padrão de `bucket` em `test_chat.py`, trocando `enviar_privado`, `ler_privado` e `apagar_privado`.
- **`_transcrever` falsa:** devolve um texto ou levanta erro, como `_chamar` em `test_assistente.py`.
- **sessão:** criada pelo fixture `sessoes`, já iniciada (início = agora − 10 min), do profissional 22 com o paciente 23.
- **`ultima_notificacao`**, para limpar os avisos.

Casos:
1. **Envio:**
   - O profissional da sessão manda WebM: 202, linha `PENDENTE`, objeto no bucket com tipo `audio/webm` e auditoria `TRANSCRICAO_ENVIADA`.
   - PDF renomeado para `.webm`: 400. Arquivo acima do limite: 413. Sem arquivo: 400.
   - Paciente: 403. Outro profissional: 404. Sessão cancelada ou daqui a 5 dias: 409. Segundo envio: 409, e o bucket fica só com o primeiro objeto.
2. **Processamento:**
   - `processar_pendentes()` com a falsa devolvendo texto: `PRONTA`, áudio apagado do bucket, `AUDIO_CHAVE` nula e notificação `TRANSCRICAO_PRONTA` para o 22.
   - Com a falsa falhando: `TENTATIVAS` sobe e o status continua `PENDENTE`. Na 5ª falha: `ERRO`, áudio apagado e notificação `TRANSCRICAO_FALHOU`.
3. **Leitura:**
   - O profissional lê o texto: auditoria `TRANSCRICAO_ACESSO` e `Cache-Control: private, no-store`. Paciente: 403. Outro profissional: 404.
   - Texto de 100 KB volta inteiro, o que cobre o `BlobReader`.
4. **Aviso:** `gravacao` é repassado pelo signaling ao outro participante. Seguir o teste de WS que já existe para o signaling, se houver; senão, chamar `signaling_service.atender` com WS falsos.

Em `tests/test_rotas.py`, perto das linhas de `avaliacao`, fixar status e chaves das duas rotas para anônimo (401), paciente (403) e profissional sem transcrição (404).

## Verificação manual

1. `.venv\Scripts\python database/migrar.py`, depois `.venv\Scripts\python -m pytest`, tudo verde.
2. Com `GEMINI_API_KEY` e as chaves do R2 no `.env`:
   - Gravar 1–2 min no Chrome com o front (ou usar um `.webm` qualquer), fazer `POST` com o cookie do profissional e esperar até 60 s.
   - O `GET` mostra o texto com `[MM:SS] Profissional: ...`, e o objeto some do bucket `psicodaily-objects`.
3. Testar no Safari: o envio em MP4 também transcreve. Se o Gemini recusar `audio/m4a`, trocar por `audio/mp4` e anotar aqui.

## Limites conhecidos (aceitos)

- **Fechar a aba antes de encerrar perde a gravação.** O áudio só existe no navegador até o envio.
- **Acima de ~1h55 (16 kbps) dá 413.** Se acontecer, enviar pela Files API do Gemini, que aceita arquivos maiores.
- **Enquanto transcreve, a thread não manda lembretes**, por alguns minutos. As janelas dos lembretes são de horas, então não se perde aviso. Se incomodar, usar uma thread própria.
- **Se o `apagar_privado` falhar depois do commit, o áudio fica órfão no bucket.** É o mesmo `ponytail:` do chat.
- **O Gemini pode trocar quem fala.** O profissional tem o contexto para corrigir mentalmente; editar o texto fica para quando pedirem.
- **Não há rota para apagar a transcrição.** Entra quando alguém pedir.

## Commits sugeridos

- `feat: transcrição das consultas pelo Gemini, lida só pelo profissional da sessão`
- `feat: aviso de gravação repassado na sala de vídeo`
