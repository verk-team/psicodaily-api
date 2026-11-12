# Backend dos benefícios de assinatura

Leia `.claude/docs/assinaturas.md` (os benefícios combinados) e `.claude/architecture.md`, e implemente as 4 tarefas abaixo. O front é feito em outro projeto, que lê a tabela "Rotas" do `architecture.md`: siga os contratos à risca.

**Princípio:** plano maior dá benefício a mais; nenhum plano limita o uso da plataforma.

## 1. Taxa da plataforma por plano

Essencial 10%, Profissional 8%, Premium 6%. Hoje `financeiro_service.py` usa a constante `TAXA_PLATAFORMA` para todos.

- Migration nova: coluna anulável de percentual em `PAGAMENTO`. Nulo = 10% (pagamentos antigos), sem `UPDATE`.
- Gravar o percentual ao criar a cobrança, nos dois `INSERT` de `pagamentos_service.py` (linhas 174 e 213), usando o plano ativo do profissional naquele momento. Sem plano, 10%. Trocar de plano não altera pagamentos já criados.
- `liquido()` passa a receber o percentual. Ajustar `ganhos` e o fechamento de repasses, que chamam essa função.
- Manter os percentuais em um único lugar, porque os valores ainda podem mudar.
- Cuidado com import circular: `assinaturas_service` já usa `pagamentos_service.cobrar`.

Contrato:
- `GET /api/assinaturas/planos`: cada plano ganha `taxa_plataforma_percentual`.
- `GET /api/pagamentos/ganhos`: `taxa_plataforma_percentual` continua existindo e passa a ser a taxa do plano atual (10 sem plano; a `Carteira` do front mostra esse campo). `taxa_plataforma_centavos` de cada consulta usa o percentual gravado no pagamento.

## 2. Bloqueio de férias e folgas (todos os planos)

Hoje `DISPONIBILIDADE` só tem janelas semanais.

- Tabela nova com profissional, data inicial e data final (dias inteiros, inclusive).
- Aplicar em `disponibilidade_service.filtro_sql` e em `horarios_livres`. O bloqueio vale também para quem não tem janelas: não deixe a condição só dentro do ramo `EXISTS`.
- `filtro_sql` já cobre o `POST /consultas` e a busca por disponibilidade. Confira se a lista de espera avisa vaga dentro de um bloqueio e corrija, se avisar.
- Criar um bloqueio não cancela as sessões já marcadas.

Contrato (usar `profissional_autenticado`, sem exigir plano):
- `GET /api/profissionais/me/bloqueios` → `{"bloqueios": [{bloqueio_id, inicio, fim}]}`, só os que ainda não terminaram.
- `POST /api/profissionais/me/bloqueios` com `{inicio, fim}` em `AAAA-MM-DD` → 201 com `{bloqueio, sessoes_no_periodo}`, em que `sessoes_no_periodo` é quantas sessões não canceladas já existem no período, para o front avisar. Recusar: campo desconhecido, `fim` antes de `inicio` ou no passado, e mais de 365 dias (400); sobreposição com outro bloqueio (409).
- `DELETE /api/profissionais/me/bloqueios/<id>`: só o dono, como os outros `DELETE` da API.

## 3. Ordem da busca

- **Rodízio dos Premium:** hoje `ORDEM_PADRAO` desempata por `ID_USUARIO`, e o cadastro mais antigo fica sempre em primeiro. A ordem entre os Premium deve mudar por dia e ficar estável durante o dia, para a paginação não repetir nem pular ninguém. Os demais continuam por `ID_USUARIO`.
- **Assistente de IA:** não pode favorecer quem paga. Em `assistente_service.py:168`, sem `ordenar`, usar uma ordem sem patrocínio (ex.: `avaliacao`). Confira se `ORDENACOES` e `recomendados` não consideram o plano e corrija, se considerarem. O WhatsApp usa o mesmo assistente.

## 4. Artigos em destaque

Contrato: `GET /api/conteudos/destaques?limite=` (público, padrão e máximo 12) → `{"conteudos": [...]}`, com os campos atuais de conteúdo mais `profissional_nome` e `profissional_foto_url`. Só de profissionais ativos com Premium válido, dos mais recentes para os mais antigos.

## Não fazer agora

- **Sessões recorrentes:** ficam para uma segunda entrega, porque as regras ainda não estão definidas.
- **Suporte prioritário:** o prazo ainda não foi confirmado, e não há código a fazer por enquanto.
- **Front:** nenhuma alteração.

## Ao terminar

1. Testes em `tests/`, cobrindo no mínimo: taxa gravada por plano, troca de plano sem alterar pagamento antigo, pagamento antigo a 10%, bloqueio recusando agendamento (com e sem janelas), rodízio estável no dia, assistente sem Premium na frente e destaques só de Premium. Manter `tests/test_rotas.py` em dia e rodar a suíte inteira.
2. Atualizar `.claude/architecture.md` (rotas, tabelas, benefícios) e o trecho sobre os 10% em `.claude/CLAUDE.md`.
3. Commit só se eu pedir, em português (`feat:`), **sem Claude como autor ou coautor**. `request.sh` e `ultima-sprint-pendentes.md` estão fora do git e não fazem parte desta tarefa: não os inclua.
4. Escrever `.claude/docs/assinaturas-backend-relatorio.md`, conciso e claro. Para cada mudança, dizer: o que mudou, por que mudou, quais os tradeoffs e o porquê das escolhas feitas. Incluir também o resultado dos testes, se a migration já pode ir para produção e qualquer diferença em relação aos contratos acima.
5. Responder no chat com um resumo curto e o caminho desse arquivo.
