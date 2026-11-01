# Relatório: backend dos benefícios de assinatura

As 4 tarefas estão prontas. Sessões recorrentes, suporte prioritário e front ficaram de fora, como pedido.

## Antes de publicar

1. **O deploy liga as taxas de 8% e 6%,** confirmadas em 01/11/2026 (item 1 de "A confirmar" em `assinaturas.md`). Para mudar os valores, basta editar `PLANOS` em `services/planos_service.py`.
2. **O rodízio mantém o topo pago da busca.** O item 3 (validação com jurídico ou CRP/CRM) também segue em aberto.
3. **Seu banco local está na migration 0022.** Antes de subir a API local, rode `.venv\Scripts\python database/migrar.py`, senão as rotas de pagamento e de agenda dão erro por falta da coluna e da tabela novas.

## 1. Taxa da plataforma por plano

**O que mudou**
- A migration 0023 cria `PAGAMENTO.TAXA_PLATAFORMA_PERCENTUAL`, `NUMERIC(5,2)` e anulável.
- O percentual é gravado nos dois `INSERT` de `pagamentos_service.py`.
- `liquido()` agora recebe o percentual; `ganhos` e o fechamento de repasses foram ajustados.
- `/planos` ganhou `taxa_plataforma_percentual`. Em `/ganhos`, esse campo agora mostra a taxa do plano atual.

**Por que mudou:** com uma constante única, trocar de plano alteraria o valor de consultas antigas.

**Escolhas e tradeoffs**
- **Módulo novo `services/planos_service.py`:** `PLANOS` e `plano_ativo` saíram de `assinaturas_service`, porque `pagamentos_service` precisa da taxa e `assinaturas_service` já usa `pagamentos_service`. A alternativa era um import dentro da função, que esconde o ciclo sem resolvê-lo. Os outros arquivos continuam chamando `assinaturas_service.plano_ativo`, para não aumentar o diff.
- **Duas constantes de 10%:** `TAXA_SEM_PLANO` é regra de negócio e pode mudar. `TAXA_PAGAMENTO_ANTIGO` é histórico e não pode: se usassem a mesma constante, mudar a primeira alteraria o valor de pagamentos antigos.
- **`NUMERIC(5,2)` em vez de inteiro:** aceita um valor como 7,5% sem nova migration.
- **Vale o plano do momento da cobrança, não o do pagamento (como você pediu):** quem gera um Pix no Essencial, faz upgrade e só então paga fica com 10% nesse pagamento. A pendente reaproveitada também mantém a taxa original.

## 2. Bloqueio de férias e folgas

**O que mudou**
- A migration 0024 cria a tabela `BLOQUEIO_AGENDA`.
- As 3 rotas de `/me/bloqueios` foram criadas.
- O bloqueio passou a valer em `filtro_sql` e em `horarios_livres`.

**Por que mudou:** só existiam janelas semanais, sem como fechar datas específicas.

**Escolhas e tradeoffs**
- **Fora do ramo `EXISTS`:** a condição fica fora do `OR` das janelas, então vale também para quem não tem janelas, como você alertou. Há teste para esse caso.
- **Lista de espera: avisava vaga dentro de bloqueio, corrigi.** O paciente recebia o aviso e o agendamento dava 409. Agora a entrada não é avisada e continua na fila para a próxima vaga real.
- **Fim exclusivo:** uma sessão das 23h à meia-noite, na véspera do bloqueio, é aceita, porque não encosta no dia bloqueado.
- **Trava no usuário ao criar:** o Firebird não tem restrição contra períodos sobrepostos, então duas requisições ao mesmo tempo passariam na checagem. O `WITH LOCK` faz a segunda esperar a primeira, como em `reservar_participantes`.
- **O profissional ainda pode remarcar para dentro de um bloqueio.** O `PATCH` dele não passa por `filtro_sql` e já pode cair fora das janelas; mantive esse comportamento.
- **Limite aceito:** uma sessão marcada enquanto o bloqueio está sendo criado pode ficar fora de `sessoes_no_periodo`. A sessão continua válida e só a contagem do aviso sai menor.

## 3. Ordem da busca

**O que mudou**
- `ORDEM_PADRAO` agora faz rodízio entre os Premium.
- O assistente busca por `avaliacao` quando não recebe `ordenar`.

**Por que mudou:** o cadastro Premium mais antigo ficava sempre em primeiro, e o assistente favorecia quem paga sem avisar o paciente.

**Como funciona o rodízio:** cada Premium recebe um número, o SQL soma o número do dia e tira o resto pelo total de Premium. A fila anda uma posição por dia. Isso usa window functions, cálculos do SQL sobre o conjunto das linhas ([documentação do Firebird](https://firebirdsql.org/file/documentation/html/en/refdocs/fblangref40/firebird-40-language-reference.html#fblangref40-windowfuncs)).

**Escolhas e tradeoffs**
- **Rotação em vez de hash por dia:** um hash embaralharia melhor, mas com 2 Premium a ordem repetiria em cerca de metade dos dias, e o pedido é que ela mude todo dia.
- **O rodízio considera só quem passou nos filtros:** cada busca filtrada também gira de forma justa, sem consulta extra.
- **Limite aceito:** se alguém vira ou deixa de ser Premium no meio do dia, a fila se reorganiza uma vez. Quem estiver paginando nesse momento pode ver alguém repetido ou deixar de ver alguém. É raro, e corrigir exigiria guardar a ordem de cada dia.
- **`ORDENACOES` e `recomendados` não consideravam o plano,** então não precisaram de correção. Há teste de que a pontuação não muda com o plano.
- **O assistente ainda desempata por ID:** sem avaliações, o cadastro mais antigo vem primeiro. Não tem relação com patrocínio, mas favorece quem chegou antes.

## 4. Artigos em destaque

**O que mudou:** foi criada a rota pública `GET /api/conteudos/destaques`, com `conteudos_service.destaques`.

**Por que mudou:** é o benefício do Premium de aparecer no site público.

**Tradeoff:** cada item traz o `corpo` inteiro, como pede o contrato ("campos atuais"). Com 12 artigos de até 20 mil caracteres, a resposta pode passar de 240 KB. Se a página de destaques só mostrar cartões, vale um campo de resumo depois.

## Testes

- **Suíte inteira: 260 passaram, 0 falharam.** O banco foi criado do zero pelas migrations e pelo seed, como no CI. Uma segunda rodada no mesmo banco também passou, o que mostra que os testes desfazem o que criam.
- **Mutações:** quebrei de propósito 12 regras novas (ordem do assistente, lista de espera, bloqueio sem janelas, rodízio, taxas, filtros dos destaques etc.), uma de cada vez. Os testes falharam em todas e os arquivos foram restaurados. As mutações não fazem parte do repositório.
- **Cobertura pedida:** todos os itens têm teste. `tests/test_rotas.py` foi atualizado, e a fixture `assinatura` foi para o `conftest.py`, porque agora vários arquivos a usam.
- **Não testado:** a concorrência real da trava na criação de bloqueios e o assistente com o Gemini de verdade. O teste chama a ferramenta dele direto, sem o modelo.
- **Problema anterior, não corrigido:** alguns testes, como os de `test_rotas.py`, agendam consultas sem desligar o e-mail e tentam enviar por SMTP de verdade (apareceu um aviso `SMTPDataError` em uma rodada). Corrigi só em `test_disponibilidade.py`. Vale levar o `sem_email` para o `conftest.py`.

## Migration em produção

**Pode ir.** As duas só adicionam: uma coluna anulável e uma tabela nova, sem `UPDATE`.

- **Testes feitos:** rodaram do zero e numa cópia do seu banco local (2 pagamentos antigos ficaram com taxa nula, como esperado). Não testei no banco de produção.
- **No deploy:** o `docker-entrypoint.sh` faz o `gbak` antes do `migrar.py`, mas o backup continua só dentro do volume.
- **Se precisar voltar o código:** o banco não precisa ser revertido, porque a versão antiga ignora a coluna e a tabela. Só que ela calcula 10% em tudo, inclusive nos pagamentos gravados a 6% ou 8%, e ignora os bloqueios. Se isso acontecer, não feche repasses nesse período.

## Diferenças e interpretações do contrato

Nenhum campo ou status definido no contrato mudou. Onde ele não definia, decidi assim:

| Ponto | Decisão |
|---|---|
| `limite` de destaques fora de 1–12 | 400, como nas outras rotas com `limite`, em vez de reduzir para 12 |
| `inicio` no passado | Aceito se `fim` não passou, para registrar férias já em andamento |
| 365 dias | Conta os dois extremos: 365 dias corridos passam, 366 dão 400 |
| `sessoes_no_periodo` | Conta tudo que não está cancelado, inclusive `REALIZADO`, se o `inicio` estiver no passado |
| `taxa_plataforma_percentual` | Número com casa decimal no JSON (`8.0`), como o campo já era em `/ganhos` |
| Datas do bloqueio | Só `AAAA-MM-DD` com 10 caracteres; `20261031` e `2026-1-5` dão 400 |
| `DELETE` de bloqueio | 200 com `{"message"}`, como os outros `DELETE` |

## Documentação

`.claude/architecture.md` e `.claude/CLAUDE.md` estão atualizados. Em `.claude/docs/assinaturas.md`, só registrei a confirmação das taxas. Ele ainda marca esses benefícios como "novo", porque atualizar isso não estava no pedido.
