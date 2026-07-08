# Prompt inicial — Projeto EmissoesCertidoes

Copie e cole este texto como primeira mensagem no Claude Code, dentro do VS
Code, ao abrir a pasta do projeto pela primeira vez.

---

Estou iniciando o projeto **EmissoesCertidoes**: uma plataforma de emissão
automatizada de certidões para um escritório de advocacia, com arquitetura
de microserviços em Python. Vou te dar todo o contexto de decisões já
tomadas antes de você mexer em qualquer coisa.

## Contexto do problema

O escritório emite manualmente diversas certidões em portais diferentes
(Receita Federal, prefeituras via Atende.Net como Pinhais e Curitiba, TJs,
órgãos federais/trabalhistas, etc). O volume é grande e crescente. O
objetivo é automatizar essas emissões, **um worker independente por
portal**, de forma que se um portal cair, mudar de layout ou travar, os
outros continuem funcionando normalmente.

Já temos dois projetos de referência que embasaram as decisões de
arquitetura:

1. **Certidão Conjunta PF/PJ** (Receita Federal) — projeto já funcional,
   usa `nodriver`, tem lógica de captura de evidência (screenshot) em caso
   de erro, dois modos de uso (unitário e planilha em massa), e geração de
   relatório CSV. Foi a base pro worker de referência que já existe no
   esqueleto do projeto.
2. **SsaMonitorProcessos** — projeto de monitoramento de processos via
   Atende.Net (prefeituras de Pinhais/Curitiba), usa Playwright + 2captcha
   + MySQL, já tem estrutura de dashboard e evidências. O worker do
   Atende.Net a ser construído aqui vai portar a lógica de lá.

## Decisões de arquitetura já tomadas (não reabrir sem motivo forte)

```
Front (a construir) ──▶ Gateway (FastAPI) ──▶ RabbitMQ ──▶ Worker do portal X
                              │                                    │
                              ▼                                    ▼
                    Banco central (MySQL)  ◀──── status/evidência ─┘
```

- **Um worker por portal**, container Docker independente, escalável
  individualmente (`docker compose up --scale worker-x=3`).
- **Navegador padronizado em `nodriver`** (mais furtivo contra detecção de
  bot do que Playwright puro) em todos os workers novos.
- **RabbitMQ** para fila de mensagens entre Gateway e Workers — cada
  portal tem sua própria fila, com dead-letter queue automática (retry
  falhou 3x → cai numa fila `<portal>.dlq` para inspeção manual).
- **Gateway em FastAPI**: único ponto de entrada. Recebe pedidos
  (unitário ou em massa via planilha .xlsx), grava no banco central com
  status `pendente`, publica na fila do portal correto. **Nunca processa
  nada diretamente** — só roteia. Isso resolve um problema real que
  existia no projeto original (mistura de `asyncio.run()` dentro de
  `asyncio.to_thread()` porque request HTTP e automação de navegador
  conviviam no mesmo processo).
- **Banco de dados central (MySQL)**: única fonte de verdade sobre status
  de cada pedido. Status possíveis: `pendente`, `processando`,
  `sucesso_confirmado`, `sucesso_provavel`, `erro_portal` (portal recusou/
  erro de negócio), `erro_tecnico` (timeout, captcha falhou, portal fora
  do ar), `falha_indefinida`.
- **Evidência obrigatória em caso de erro**: todo worker, ao cair em
  `erro_portal` ou `falha_indefinida`, tira screenshot da tela e sobe pro
  storage central (local por enquanto, abstraído para trocar por S3/MinIO
  sem mexer nos workers).
- **Captcha como módulo plugável com classe abstrata**: nem todo portal
  tem captcha, e os que têm variam (reCAPTCHA v2, hCaptcha, captcha de
  imagem simples). Existe uma interface `ResolvedorCaptcha` (métodos
  `resolver_recaptcha_v2`, `resolver_hcaptcha`, `resolver_captcha_imagem`)
  com implementação concreta via 2captcha, e uma fábrica
  `obter_resolvedor()`. Cada worker só importa e chama isso quando o
  portal exigir — sem acoplar a lógica de captcha em todo lugar.

## O que já existe no esqueleto do projeto

- `libs/certidoes_core/` — lib Python compartilhada, instalada via
  `pip install -e` em cada serviço:
  - `config.py` — configuração via variáveis de ambiente (`.env.example`
    na raiz documenta todas)
  - `banco.py` — modelos SQLAlchemy (`PedidoCertidao`, `LotePlanilha`)
  - `fila.py` — wrapper sobre RabbitMQ (publish/consume, DLQ automática)
  - `storage.py` — abstração de storage de evidências (local ou S3/MinIO)
  - `evidencia.py` — captura de screenshot padronizada
  - `captcha/` — `base.py` (classe abstrata), `twocaptcha_provider.py`
    (implementação concreta), `__init__.py` (fábrica `obter_resolvedor`)
- `services/gateway/` — FastAPI funcional:
  - `POST /pedidos` (pedido unitário)
  - `POST /pedidos/planilha` (upload de planilha, gera N pedidos)
  - `GET /pedidos/{id}` (status de um pedido)
  - `GET /lotes/{id}` (status consolidado de um lote de planilha)
  - `GET /portais` (lista portais habilitados)
  - `app/planilha.py` — parser de planilha (adaptado do projeto original,
    lê de bytes em memória em vez de disco)
- `services/worker-receita-federal/` — worker de referência, adaptado do
  projeto Certidão Conjunta, consumindo da fila `receita_federal`.
  **Importante: este worker foi escrito por adaptação de código
  funcional, mas nunca rodou de fato nesse formato de consumidor de
  fila — precisa ser testado e depurado.**
- `docker-compose.yml` com RabbitMQ, MySQL, Gateway e o worker de
  referência, já com healthchecks e volumes configurados.
- `docs/CATALOGO_PORTAIS.md` — catálogo com os 26 sistemas/portais
  extraídos da planilha oficial do escritório (`PlanilhaLinksCertidoes.xlsx`),
  com status de reconhecimento por portal (veja seção abaixo).

## Catálogo de portais e prioridade

A planilha oficial do escritório tinha 53 linhas cobrindo PF, PJ e um
bloco de certidões de Imóvel (Curitiba), com os links embutidos como
botões clicáveis (não texto simples — precisou parsear o XML interno do
xlsx pra extrair). Consolidando por sistema único, são **26 portais**
distintos. Detalhes completos em `docs/CATALOGO_PORTAIS.md`, mas o resumo:

- ✅ **Já automatizado**: Certidão Conjunta (Receita Federal)
- 🟢 **Confirmado simples, PRÓXIMO A CONSTRUIR**: CPF — Situação
  Cadastral (Receita Federal). Captcha tipo checkbox simples ("não sou
  robô"), só 2 campos (CPF + data nascimento), sem login, PDF instantâneo.
  Mesmo domínio (`receita.fazenda.gov.br`) do worker já existente —
  reaproveitamento de código deve ser alto.
- 🔴 **Confirmado complexo**: Pesquisa Protesto (exige login/certificado
  digital, entrega assíncrona em até 60 dias — arquitetura de worker
  diferente, precisa reconsulta periódica) e Assertiva (plataforma paga
  com login, fluxo de busca, não é emissão direta).
- ⚪ **A validar** (18 portais): SEFAZ PR, Prefeitura de Curitiba,
  Distribuidores da Justiça Estadual 1º-4º, Projudi TJPR, TRF4, JFPR,
  TST, TRT9, IBAMA, MPF, MPT, Ministério da Economia, FGTS/Caixa, e o
  bloco de Imóvel/Curitiba (2 desses últimos têm URLs com token de sessão
  aparentemente expirado — reabrir manualmente antes de automatizar).
- Critério de priorização definido com o escritório: **simplicidade
  técnica primeiro**, para ganhar tração rápido, não volume nem ordem da
  planilha.

## Portal Atende.Net (Pinhais) — situação separada

O worker do Atende.Net (prefeituras, incluindo Pinhais) está sendo
portado do projeto `SsaMonitorProcessos`. Contexto técnico daquele
projeto: usa Playwright (não nodriver — decisão a discutir se vale
padronizar ou manter Playwright só nesse worker por já estar validado) +
2captcha, roda em Windows com Python 3.14 num `.venv`. Último blocker
identificado: o código estava resolvendo o reCAPTCHA errado (badge
invisível em vez do modal visível); a função `capturar_sitekey` foi
reescrita pra priorizar iframes `size=normal`, mas essa correção ainda
não tinha sido validada rodando de verdade. Esse worker usa o módulo
`certidoes_core.captcha` (via 2captcha) já que o Atende.Net exige
resolução de captcha.

⚠️ **Nota de segurança**: no projeto `SsaMonitorProcessos` original, um
arquivo `.env` com credenciais reais (MySQL e chave da API 2captcha) foi
incluído acidentalmente num upload. Se essas credenciais ainda não foram
rotacionadas, isso precisa acontecer antes de qualquer deploy real deste
novo projeto.

## O que preciso que você me ajude a fazer agora

1. **Revisar o esqueleto existente** — comece por `README.md`, depois
   `libs/certidoes_core/` e `services/gateway/app/main.py` — e me aponte
   qualquer problema de arquitetura antes de eu continuar.
2. **Validar/testar o worker da Receita Federal (Certidão Conjunta)
   rodando de verdade** via `docker compose up`, depurando o que falhar.
3. **Construir o worker do CPF — Situação Cadastral** (próxima prioridade
   confirmada), reaproveitando ao máximo a estrutura do worker da
   Certidão Conjunta, já que é o mesmo domínio e um fluxo mais simples
   (captcha checkbox, sem login).
4. **Construir o worker do Atende.Net (Pinhais)**, portando a lógica do
   `robots/atendenet_v2/robot.py` do `SsaMonitorProcessos` pro mesmo
   padrão dos outros workers (`processar_pedido(pedido_id)` lendo da
   fila, processando, atualizando status no banco), usando
   `certidoes_core.captcha` para o reCAPTCHA.
5. Depois desses três workers estáveis, seguir validando os portais
   marcados como ⚪ no catálogo (checklist de reconhecimento já definido
   em `docs/CATALOGO_PORTAIS.md`) e construindo os próximos workers na
   ordem de simplicidade confirmada.
6. Eventualmente, começar o **front** (React ou similar) com seleção via
   checkbox de portais e upload de planilha, consumindo os endpoints já
   existentes do Gateway.

Pode começar revisando o `README.md` e `docs/CATALOGO_PORTAIS.md`, e me
perguntando o que precisar antes de mexer em código.
