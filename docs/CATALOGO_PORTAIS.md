# Catálogo de Portais — EmissoesCertidoes

Extraído de `PlanilhaLinksCertidoes.xlsx` (links estavam embutidos como botões
clicáveis no Excel, não em texto — foram extraídos via script analisando o
XML interno do arquivo). 28 sistemas únicos identificados a partir das 53
linhas da planilha (PF, PJ e um bloco de Imóvel/Curitiba), consolidados nas
26 linhas da tabela abaixo — dois pares foram agrupados numa linha só:
"Assertiva" + "Assertiva Crédito Mix" (mesma URL) e "Consulta Processual 1º
Grau" + "2º Grau Projudi TJPR" (nenhuma tinha link na planilha pra
diferenciar; separar de novo se o escritório confirmar que são destinos
distintos).

Legenda de status:
- ✅ **Automatizado** — worker já existe
- 🟢 **Simples (confirmado)** — validado manualmente, sem login, sem captcha
  complexo, resultado instantâneo
- 🔴 **Complexo (confirmado)** — login, certificado digital, ou fluxo
  assíncrono (pedido → espera → retorna depois)
- ⚪ **A validar** — não verificado ainda, precisa do checklist abaixo
- 🟡 **Construído, mas bloqueado** — worker existe e a mecânica de preenchimento
  funciona, mas algo impede a emissão de fato (ex: captcha rejeitado pelo
  backend) e precisa de investigação adicional (às vezes infraestrutura, não
  só código)
- ⚠️ **Link suspeito** — URL da planilha pode estar expirada (token de sessão embutido)
- ❌ **Eliminado da fila (decisão do escritório)** — descartado de propósito por ser
  específico/complicado demais pro esforço de automação (login, certificado
  digital, pagamento, ou dependência de terceiros) — não será automatizado
  a menos que o escritório reabra a discussão

## Fila de prioridade por dificuldade

Consolidado depois da varredura de reconhecimento em todos os ⚪
restantes. Da mais fácil (já pronta) pra mais difícil:

**Tier 0 — Automatizados e validados de ponta a ponta (✅)**
1. CPF — Situação Cadastral (Receita Federal) — hCaptcha resolvido de verdade
2. CNDT — Débitos Trabalhistas (TST) — captcha de imagem simples
3. Certidão de Cadastro de Imóvel (Curitiba) — captcha de imagem simples,
   confirmado com dado real fornecido pelo usuário
4. Consulta de Débitos/Dívida Ativa (Curitiba) — mesma plataforma/captcha
   do item 3, validado com dado real (sem débito e, numa tentativa
   anterior, com débito — as duas mensagens diferentes foram
   confirmadas). É consulta informativa, não uma certidão formal, mas
   automatizada a pedido do usuário

**Tier 1 — Construído, falta só destravar (🟡)**
5. Certidão Conjunta (Receita Federal) — submissão funciona (preenche e
   envia certinho), mas esse serviço específico bloqueia consistentemente
   com erro genérico ("023 - tente novamente") **só quando rodado dentro
   de Docker/Linux** — confirmado com um projeto irmão que roda a mesma
   lógica nativo no Windows e funciona toda vez. Testado e descartado:
   IP/rede, CPF específico, frequência, headless vs. com tela,
   `--no-sandbox`, Chromium vs. Chrome real — nenhum resolveu. Sobra o
   ambiente Linux/Docker em si. Corrigir exigiria rodar esse worker fora
   do Docker (numa máquina Windows sempre ligada) — **despriorizado por
   enquanto** (sem máquina disponível pra isso); emissão manual continua
   sendo o caminho pra esse portal específico. Ver aviso no topo de
   `services/worker-receita-federal/worker.py`
6. CNPJ+QSA (Receita Federal) — mecânica funciona, backend rejeita o
   token do captcha (suspeita de validação de IP)
7. Certidão Cível/Criminal JFPR (TRF4) — submissão 100% validada com
   captcha real (5 emissões confirmadas). Passo final bloqueado por um
   **bug do próprio site do TRF4** (link do botão "Visualizar Certidão
   Gerada" está quebrado, 404 confirmado de 3 formas diferentes) — não
   depende mais de nós, só do TRF4 corrigir
8. SEFAZ PR (Certidão de Débitos Tributários e Dívida Ativa) — landing
   page passou a carregar sem bloqueio de borda (mudança desde a
   varredura original), formulário construído (`worker-sefaz-pr`), mas
   bloqueado ainda mais cedo que o CNPJ+QSA: o **2captcha** devolveu
   `ERROR_CAPTCHA_UNSOLVABLE` nas 3 tentativas — reCAPTCHA Enterprise
   invisível parece ser difícil demais pro serviço de resolução atual.
   Não depende de código nosso; precisaria trocar de provedor de captcha
   pra ter alguma chance
9. Certidão Negativa de Débitos (Atende.Net — Prefeitura de Pinhais) —
   primeiro portal na plataforma Atende.Net. Achado num reconhecimento
   novo (serviço "Certidão Negativa de Débitos", separado da "Consulta
   de Processo Digital" que já tinha código de referência pronto).
   **Sem captcha em nenhum ponto do fluxo** — mais simples que os outros
   portais construídos até agora. Caminho de "CPF/CNPJ não é contribuinte
   de Pinhais" validado de ponta a ponta, inclusive rodando em Docker.
   Caminho de sucesso (contribuinte de verdade) ainda não validado —
   nenhum documento de teste disponível é contribuinte de Pinhais — e
   esbarrou no **mesmo bloqueio de ambiente Linux/Docker** já visto na
   Receita Federal (alerta antifraude genérico só rodando em container;
   fluxo idêntico nativo no Windows passa limpo). Mesma decisão: worker
   construído e documentado, despriorizado por falta de máquina Windows
   sempre ligada. Ver aviso no topo de
   `services/worker-atendenet-pinhais/worker.py`

**Tier 2 — vago.** Nenhum portal 🟢 "pronto pra construir agora" sobrou na
varredura atual.

**Tier 3 — Carregam sem bloqueio, mas precisam de mais um passo de
reconhecimento antes de classificar**
8. Guia Amarela (Curitiba) — inacessível na última tentativa
   (`ERR_CONNECTION_REFUSED`, do Chromium real e de `curl` puro) — pode ser
   instabilidade temporária, revalidar antes de descartar

**Tier 6 — Bloqueados por proteção de borda pesada (🔴)**

9. TRT9 — WAF CloudFront ("403 Request blocked")
10. MPF — WAF genérico ("Web Page Blocked", Attack ID)
11. MPT — mesmo padrão de WAF do MPF (Attack ID idêntico)
12. FGTS (Caixa) — ShieldSquare/Radware Bot Manager (confirmado de novo
    numa revalidação — título da página de bloqueio é literalmente
    "ShieldSquare Captcha")
13. Prefeitura de Curitiba (CND + Imóvel/IPTU, mesmo domínio) — Akamai
    Bot Manager, domínio inteiro bloqueado

⚠️ **Nota importante**: esses 5 bloquearam vindos do mesmo ambiente/IP de
teste (datacenter/cloud) — revalidados novamente e continuam bloqueados
(confirmado com navegador real, não só `curl`). Isso pode ser
específico dessa rede de desenvolvimento — vale re-testar a partir da
rede real de produção do escritório antes de descartar de vez, já que
bloqueio por reputação de IP de datacenter nem sempre se repete numa
rede residencial/corporativa comum.

## Eliminados da fila (decisão do escritório, 2026-07-03)

Descartados de propósito por serem específicos/complicados demais pro
esforço de automação — não serão automatizados a menos que o escritório
reabra a discussão:

- IBAMA (provável login)
- Distribuidor Justiça Estadual 1º, 2º, 3º e 4º (pagos/assíncronos, ou sem
  link confirmável)
- Consulta Processual 1º/2º Grau Projudi TJPR, Consulta Processual JFPR,
  Consulta Processual TRF4 (sem link na planilha)
- Ministério da Economia — e-processo (login GOV.BR)
- Pesquisa Protesto/CENPROT (login/certificado, entrega em até 60 dias)
- Assertiva / Assertiva Crédito Mix (plataforma paga com login)

## Catálogo completo

| # | Portal | Usado em | URL | Status |
|---|---|---|---|---|
| 1 | Receita Federal — Certidão Conjunta | PF/PJ | `servicos.receitafederal.gov.br` | 🟡 Construído e validado manualmente (fora do Docker), mas bloqueado rodando dentro do container — erro genérico da Receita só nesse ambiente. Despriorizado, emissão manual por enquanto. Ver `services/worker-receita-federal/worker.py` |
| 2 | Receita Federal — CPF (situação cadastral) | PF | `servicos.receita.fazenda.gov.br/servicos/cpf/consultasituacao/consultapublica.asp` | ✅ Automatizado e **validado de ponta a ponta** com captcha real (hCaptcha, não reCAPTCHA como o catálogo original supunha) — comprovante emitido e PDF gerado corretamente |
| 3 | Receita Federal — CNPJ+QSA (cnpjreva) | PJ | `solucoes.receita.fazenda.gov.br/servicos/cnpjreva/cnpjreva_solicitacao.asp` | 🟡 Construído, mas **bloqueado** — o backend rejeita o token do hCaptcha mesmo com a submissão funcionando corretamente (hipótese de reCAPTCHA descartada). Provável causa: validação de IP entre quem resolve o captcha e quem submete, exigindo proxy. Ver aviso no topo de `services/worker-cnpj-qsa/worker.py` |
| 4 | Pesquisa Protesto (CENPROT) | PF/PJ | `pesquisaprotesto.com.br` | ❌ **Eliminado da fila** (decisão do escritório) — login/certificado digital, entrega assíncrona (até 60 dias) |
| 5 | SEFAZ PR | PF/PJ | Link da planilha morto (404) — atual: `cdwfazenda.paas.pr.gov.br/cdwportal/certidao/automatica` | 🟡 **Reconhecimento atualizado**: a landing page passou a carregar sem bloqueio de borda (antes rejeitava a sessão automatizada de cara). Worker construído (`worker-sefaz-pr`, reCAPTCHA Enterprise com hook de callback), mas testado com captcha real e bloqueado num ponto anterior ao do CNPJ+QSA: o próprio **2captcha** devolveu `ERROR_CAPTCHA_UNSOLVABLE` (não conseguiu nem gerar um token pra tentar). Não depende de código nosso — precisaria de outro provedor de captcha |
| 6 | Prefeitura de Curitiba (CND) | PF/PJ | `cnd-cidadao.curitiba.pr.gov.br/Certidao/Solicitar[Cpf]` | 🔴 Complexo — domínio inteiro (inclusive `www.curitiba.pr.gov.br`) bloqueado por Akamai Bot Manager (Access Denied), provável bloqueio de IP de datacenter, não específico da página. Despriorizado |
| 7 | Distribuidor Justiça Estadual 1º | PF/PJ | `1distribuidorcuritiba.com.br/default/` | ❌ **Eliminado da fila** (decisão do escritório) — serviço pago e assíncrono (mesmo operador do item 8) |
| 8 | Distribuidor Justiça Estadual 2º | PF/PJ | `2distribuidorcuritiba.com.br/default/` | ❌ **Eliminado da fila** (decisão do escritório) — serviço pago e assíncrono (o próprio site avisa que a elaboração só ocorre no dia seguinte à confirmação do pagamento bancário), fluxo multi-etapa |
| 9 | Distribuidor Justiça Estadual 3º | PF/PJ | `3distrib.com.br` | ❌ **Eliminado da fila** (decisão do escritório) — serviço pago, entrega em até 24h após pagamento |
| 10 | Distribuidor Justiça Estadual 4º | PF/PJ | **sem link na planilha** | ❌ **Eliminado da fila** (decisão do escritório) |
| 11 | Consulta Processual 1º/2º Grau Projudi TJPR | PF/PJ | **sem link na planilha** | ❌ **Eliminado da fila** (decisão do escritório) |
| 12 | Certidão Cível/Criminal JFPR (TRF4) | PF/PJ | `www2.trf4.jus.br/trf4/processos/certidao/index.php` | 🟡 **Submissão 100% validada** com captcha real (5 emissões confirmadas — nome/CPF batendo com a Receita Federal, certidão gerada no sistema do TRF4, sem erro). O último passo (baixar o PDF assinado) esbarra num **bug do próprio site do TRF4**: o botão "Visualizar Certidão Gerada" aponta pra um caminho quebrado — testado de 3 formas independentes (clique no navegador, `curl` isolado, navegação direta com URL resolvida corretamente via `urljoin`), sempre 404/Bad Request. Não é limitação do nosso código; só falta o TRF4 corrigir o link deles. Ver aviso no topo de `services/worker-trf4-certidao/worker.py` |
| 13 | Consulta Processual JFPR | PF/PJ | **sem link na planilha** | ❌ **Eliminado da fila** (decisão do escritório) |
| 14 | Consulta Processual TRF4 | PF/PJ | **sem link na planilha** | ❌ **Eliminado da fila** (decisão do escritório) |
| 15 | Débitos Trabalhistas (TST) | PF/PJ | `tst.jus.br/certidao` (formulário real: `cndt-certidao.tst.jus.br`) | ✅ Automatizado e **validado de ponta a ponta** com captcha real e certidão real conferida (nome/CPF/número da certidão batendo) — captcha de imagem simples (não reCAPTCHA/hCaptcha), sistema JSF/RichFaces antigo. ⚠️ Já existiu um bug real aqui: a tela de "sucesso" não é a certidão, e o download automático (disparado pelo próprio site) só é salvo se `set_download_path` for chamado ANTES do clique em emitir — corrigido, ver aviso no topo de `services/worker-tst-cndt/worker.py` |
| 16 | Ações Trabalhistas (PJe TRT9) | PF/PJ | `pje.trt9.jus.br/certidoes/inicio` | 🔴 Bloqueado por WAF (CloudFront, "403 Request blocked") — mesmo padrão de bloqueio por IP visto em outros portais nesta rede |
| 17 | IBAMA | PF/PJ | `servicos.ibama.gov.br/sicafiext/` | ❌ **Eliminado da fila** (decisão do escritório) — landing page é 100% casca (SPA), provável login |
| 18 | Ministério Público Federal | PF/PJ | `aplicativos.mpf.mp.br/ouvidoria/app/cidadao/certidao` | 🔴 Bloqueado por WAF ("Web Page Blocked", Attack ID) |
| 19 | Ministério Público do Trabalho | PF/PJ | `prt9.mpt.mp.br/servicos/certidao-positiva-negativa` | 🔴 Bloqueado por WAF (mesmo padrão do MPF — "Attack ID: 20000051" idêntico, pode ser o mesmo produto/config) |
| 20 | Ministério da Economia (e-processo) | PF/PJ | `eprocesso.sit.trabalho.gov.br/Certidao/Emitir` | ❌ **Eliminado da fila** (decisão do escritório) — exige login via GOV.BR (SSO) |
| 21 | FGTS (Caixa) | PJ | `consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf` | 🔴 Bloqueado por ShieldSquare/Radware Bot Manager ("comportamento malicioso detectado") |
| 22 | Assertiva / Assertiva Crédito Mix | PF/PJ | `painel.assertivasolucoes.com.br/login` | ❌ **Eliminado da fila** (decisão do escritório) — plataforma paga com login |
| 23 | Guia Amarela (Curitiba) | Imóvel | `www5.curitiba.pr.gov.br/gtm/gam/Default.aspx` | ⚪ **Inacessível na tentativa mais recente** (`ERR_CONNECTION_REFUSED`, tanto do Chromium real quanto de `curl` direto do host — não é bloqueio de WAF, o servidor não respondeu) — pode ser instabilidade temporária do site; revalidar depois. Ainda não confirmado se emite certidão de verdade ou é só consulta informativa de zoneamento |
| 24 | Certidão de Débitos do Imóvel/IPTU | Imóvel | `cnd-cidadao.curitiba.pr.gov.br/Certidao/Solicitar` | 🔴 Mesmo domínio do item 6 — bloqueado por Akamai. Despriorizado |
| 25 | Certidão de Cadastro (Curitiba) | Imóvel | `declaracaounificadaimovel.curitiba.pr.gov.br/` (link antigo da planilha, com token de sessão, estava morto — abrir direto na raiz funciona) | ✅ Automatizado e **validado de ponta a ponta** com captcha real e dado real (Indicação Fiscal fornecida pelo usuário) — captcha de imagem simples, PDF final baixado corresponde exatamente à declaração de referência (mesmo endereço/bairro/histórico). Ver `services/worker-curitiba-cadastro-imovel/worker.py` |
| 26 | Consulta de Débitos (parcelamento) | Imóvel | `parcelamentoexecutado.curitiba.pr.gov.br/` (idem — abrir na raiz gera sessão nova automaticamente) | ✅ Automatizado e **validado de ponta a ponta** com captcha real e dado real — mesma plataforma/captcha do item 25 (`worker-curitiba-debitos-divida-ativa`). É consulta informativa de débitos em dívida ativa, não uma certidão formal, mas automatizada a pedido do usuário. Confirmadas as duas mensagens de resultado (com e sem débito) |
| 27 | Certidão Negativa de Débitos (Prefeitura de Pinhais) | PF/PJ | `pinhais.atende.net/autoatendimento/servicos/certidao-negativa-de-debitos` | 🟡 Construído (`worker-atendenet-pinhais`), sem captcha em todo o fluxo. Caminho de "CPF/CNPJ não é contribuinte" validado de ponta a ponta, inclusive em Docker. Caminho de sucesso não confirmado (sem documento de teste que seja contribuinte de Pinhais) e esbarrou no mesmo bloqueio de ambiente Linux/Docker já visto no item 1 — despriorizado pelo mesmo motivo |

## Checklist de reconhecimento (2 min por portal)

Pra transformar um ⚪ em 🟢 ou 🔴, abra o portal manualmente e responda:

1. **Login/cadastro necessário?** (sim = 🔴, aumenta bastante a complexidade)
2. **Tem captcha? Qual tipo?** (checkbox simples = fácil; reCAPTCHA v2/v3 com
   imagens ou hCaptcha = precisa do módulo `certidoes_core.captcha`)
3. **Resultado é instantâneo ou assíncrono?** (assíncrono = precisa de
   worker com reconsulta periódica, não só "abrir → preencher → emitir")
4. **Quantos campos o formulário pede?** (só documento = mais simples;
   pede endereço, inscrição municipal, etc. = mais complexo)
5. **O link da planilha ainda funciona, sem erro de sessão expirada?**

Preenchendo isso pros 18 portais em ⚪, dá pra montar a fila de prioridade
real (mais simples primeiro), sem eu ficar advinhando.
