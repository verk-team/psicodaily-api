# Assinaturas do profissional

**Princípio:** plano maior dá benefício a mais; nenhum plano limita o uso da plataforma. O Essencial já permite atender sem teto de pacientes ou de sessões.

Legenda: **API** = backend pronto, falta o front · **novo** = falta backend e front.

## Planos

### Essencial: R$ 49,99/mês, para atender

| Benefício | Estado |
|---|---|
| Perfil completo na busca (formação, idiomas, áreas de atuação) | API |
| Agendamentos, horários de atendimento e carteira | pronto |
| Recebimento por Pix, boleto e cartão | API |
| Videochamada e prontuário | pronto |
| Chat com pacientes e encaminhamentos | API |
| Bloqueio de férias e folgas | novo |

### Profissional: R$ 79,99/mês, para encher a agenda

Tudo do Essencial, mais:

| Benefício | Estado |
|---|---|
| Taxa da plataforma de **8%** (em vez de 10%) | novo |
| Vídeo de apresentação no perfil | API |
| Lista de espera: paciente é avisado quando abre horário | API |
| Visualizações do perfil, consultas e novos favoritos | API |
| Sessões recorrentes: mesmo paciente, mesmo horário toda semana | novo |

### Premium: R$ 119,99/mês, para ganhar visibilidade

Tudo do Profissional, mais:

| Benefício | Estado |
|---|---|
| Taxa da plataforma de **6%** | novo |
| Selo de patrocinado e topo da busca, em rodízio com os outros Premium | API; rodízio novo |
| Publicação de artigos no perfil | API |
| Artigos em destaque no site público | novo |
| Taxa de conversão e comparação com o período anterior | API |
| Suporte prioritário: resposta em até 1 dia útil | novo |

## Taxa reduzida

- Conta: tanto o Profissional quanto o Premium empatam com o Essencial em **R$ 2.500/mês** em consultas. Acima disso, o plano maior sai mais barato para o profissional.
- Custo: a plataforma ganha menos com quem fatura acima desse valor, em troca de receita fixa maior.
- Backend: gravar a taxa em cada pagamento. Hoje `financeiro_service.py` usa uma constante, e uma troca de plano alteraria os valores de consultas antigas.

## Regras

- **Valem em todos os planos:** prontuário, encaminhamentos, chat e videochamada.
- **Sem plano válido:** o profissional sai da busca e não recebe agendamento novo. As sessões marcadas continuam, e os dados voltam com o plano.
- **Cadastro novo:** 30 dias de Essencial grátis.
- **Assistente de IA:** indica sem considerar patrocínio. Hoje a busca dele põe Premium primeiro, sem avisar o paciente.

## Correções

- `Assinaturas.jsx`: buscar os preços na API e trocar as promessas vagas pelas listas acima.
- Backend: trocar a ordem por `ID_USUARIO` entre Premium, que deixa o cadastro mais antigo sempre em primeiro, pelo rodízio.
- Backend: usar no assistente uma ordem sem patrocínio.

## A confirmar

1. **Taxas de 8% e 6%:** confirmadas em 01/11/2026; a plataforma aceita a perda de receita acima de R$ 2.500/mês. Com os preços de 02/11/2026 (R$ 49,99, R$ 79,99 e R$ 119,99), essa perda começa antes: R$ 1.500/mês no Profissional e R$ 2.000/mês no Premium. É de propósito, para a troca de plano compensar para o profissional.
2. **Suporte em 1 dia útil:** o time consegue cumprir esse prazo?
3. **Publicidade:** validar o topo pago da busca e a divulgação de preços com jurídico ou CRP/CRM (art. 20 do Código de Ética do Psicólogo e regras do CFM, citados de memória).
