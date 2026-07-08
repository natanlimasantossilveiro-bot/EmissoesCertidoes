# Certidões Platform

Plataforma de emissão automatizada de certidões, com um worker independente
por portal (Receita Federal, prefeituras via Atende.Net, TJs, etc).

## Arquitetura

```
Front (front/index.html) ──▶ Gateway (FastAPI) ──▶ RabbitMQ ──▶ Worker do portal X
                              │                                    │
                              ▼                                    ▼
                    Banco central (MySQL)  ◀──── status/evidência ─┘
```

- **Gateway**: recebe pedidos (unitário ou planilha), grava no banco com
  status `pendente`, publica na fila do portal. Não processa nada — só roteia.
- **Fila (RabbitMQ)**: uma fila por portal. Se um worker cair ou travar,
  não afeta os outros. Falhas repetidas vão pra fila `<portal>.dlq` pra
  inspeção manual.
- **Workers**: um serviço por portal, cada um com seu Dockerfile e suas
  dependências específicas (ex: worker do Atende.Net vai precisar do
  2captcha, o da Receita Federal não). Escalam independente:
  `docker compose up --scale worker-receita-federal=3`.
- **Banco central**: única fonte de verdade sobre o status de cada pedido.
  Gateway e front consultam daqui, nunca perguntam direto pro worker.
- **`libs/certidoes_core`**: tudo que é comum entre os workers — conexão
  com fila, banco, captura/upload de evidência (screenshot), config via
  variáveis de ambiente.

## Rodando localmente

```bash
cp .env.example .env
docker compose up --build
```

- Gateway: http://localhost:8000/docs (Swagger automático do FastAPI)
- RabbitMQ management: http://localhost:15672 (guest/guest)
- Front: abra `front/index.html` direto no navegador (não precisa de
  servidor nem build — é uma página só, HTML+JS puro). Pede login
  (e-mail/senha) antes de mostrar qualquer coisa — ver "Autenticação"
  abaixo pra saber como logar na primeira vez. Se o Gateway estiver em
  outro host/porta, ajuste em "Gateway em outro endereço?" (dentro do
  details da tela de login). Cria pedidos, envia planilha, e acompanha o
  status em tempo real (atualiza sozinho a cada 5s, com contadores de
  total/andamento/sucesso/erro). Cada portal mostra um selo de
  confiabilidade (✅ validado / 🟡 parcial / 🧪 sem status catalogado
  ainda) — mantido manualmente em `STATUS_PORTAL` no próprio HTML, a
  partir do que já foi validado em `docs/CATALOGO_PORTAIS.md`.
- `front/admin.html`: painel de administração (só acessível a quem logou
  como admin) — criar/desativar colaboradores, resetar senha, e ver
  atividade (último acesso e pedidos de cada um).

### Autenticação (login multiusuário)

Cada colaborador tem sua própria conta (e-mail + senha), com papel
`admin` ou `colaborador`. Login via `POST /auth/login` devolve um token
JWT que o front manda em `Authorization: Bearer <token>` em toda chamada
— configure `JWT_SECRET_KEY` no `.env` (gere com `openssl rand -hex 32`,
**obrigatória**, sem ela ninguém consegue logar).

**Primeiro acesso**: com o banco vazio, configure `ADMIN_EMAIL` e
`ADMIN_SENHA_INICIAL` no `.env` — o Gateway cria essa conta admin
automaticamente no primeiro boot. Depois de logar a primeira vez, troque
essa senha pelo link "Trocar senha" no topo do painel, e crie os demais
colaboradores pelo `admin.html`.

Só o admin cria contas (`POST /admin/usuarios`) — não tem cadastro
aberto nem "esqueci minha senha" self-service (admin reseta a senha de
qualquer colaborador direto pelo painel).

## Testando o fluxo unitário

```bash
# 1. Login — pega o token
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "SEU_ADMIN_EMAIL", "senha": "SUA_SENHA"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

# 2. Cria o pedido usando o token
curl -X POST http://localhost:8000/pedidos \
  -H "Authorization: Bearer $TOKEN" \
  -F "portal=receita_federal" \
  -F "nome=Fulano de Tal" \
  -F "tipo=pf" \
  -F "documento=00000000000" \
  -F "data_nascimento=01/01/1990"
```

Isso retorna um `pedido_id`. Consulte o andamento com:

```bash
curl http://localhost:8000/pedidos/<pedido_id> -H "Authorization: Bearer $TOKEN"
```

## Como adicionar um novo portal (ex: Prefeitura de Pinhais / Atende.Net)

Isso é o ponto principal do desenho: adicionar portal novo **não deve
tocar em nada que já existe**. Passo a passo:

1. Adicionar o portal em `PORTAIS_DISPONIVEIS` no `services/gateway/app/main.py`.
2. Criar `services/worker-atendenet-pinhais/` com:
   - `worker.py` (a lógica que já está em `robots/atendenet_v2/robot.py`
     no `SsaMonitorProcessos`, adaptada pro modelo `processar_pedido(pedido_id)`
     igual ao `worker-receita-federal/worker.py`)
   - `requirements.txt`, `Dockerfile` (nesse caso, vai precisar do Playwright
     ou nodriver + dependências do 2captcha)
3. Adicionar o serviço no `docker-compose.yml`, seguindo o mesmo padrão do
   `worker-receita-federal`.
4. Pronto — o Gateway já sabe rotear pedidos `portal=atendenet_pinhais`
   pra fila certa, sem precisar saber como aquele portal funciona por dentro.

## Pendências conhecidas / próximos passos

- [x] Front provisório pra teste (`front/index.html`) — página única sem
      build, cria pedidos/planilha e acompanha status em tempo real
- [x] Visual do front melhorado pra apresentação (header com marca,
      contadores de resumo, selo de confiabilidade por portal, nomes
      amigáveis na tabela em vez do slug interno) — testado num
      navegador real (Chromium via nodriver), não só revisado por
      leitura de código
- [x] **Login multiusuário + painel de administração + deploy em VPS**
      (substitui a autenticação por chave única): tabela `Usuario`
      (nome, e-mail, senha com hash bcrypt, papel admin/colaborador,
      `ultimo_acesso_em`), login via JWT (`POST /auth/login`), bootstrap
      automático do primeiro admin a partir do `.env` no primeiro boot
      com banco vazio, endpoint pra trocar a própria senha
      (`PATCH /auth/me/senha`), e `/admin/usuarios` + `/admin/atividade`
      (pedidos por colaborador, protegidos por papel). `PedidoCertidao`
      e `LotePlanilha` trocaram o campo livre `solicitado_por` por
      `usuario_id` (FK), preenchido automaticamente por quem está
      logado. Front ganhou tela de login (`front/index.html`) e um
      painel de admin novo (`front/admin.html`) — tudo testado de ponta
      a ponta num navegador real: login certo/errado, criar colaborador,
      colaborador recebendo 403 em rota de admin, desativar usuário e
      confirmar que ele não consegue mais logar. `docker-compose.yml`
      ganhou `restart: unless-stopped` em todos os serviços e volume
      persistente pro RabbitMQ (antes perdia filas/DLQ a cada reinício).
      Guia completo de deploy em VPS (Hostinger, Nginx, HTTPS via
      Certbot/sslip.io) em `docs/DEPLOY_VPS.md`
- [x] Endpoint de relatório consolidado por lote (`GET /lotes/{lote_id}/relatorio`,
      `.xlsx` com uma linha por pedido: nome, documento, tipo, status
      traduzido, mensagem, nomes dos arquivos de certidão/evidência,
      solicitante, datas). Pode ser baixado a qualquer momento, mesmo com
      pedidos ainda pendente/processando. Front ganhou um card "Baixar
      relatório" por lote enviado, persistido no localStorage. No caminho,
      corrigido um bug pré-existente no `POST /pedidos/planilha`
      (`DetachedInstanceError` ao acessar `lote.id` depois que a sessão já
      tinha expirado o objeto por causa dos commits do loop) — o endpoint
      de planilha nunca tinha sido testado de ponta a ponta antes disso
- [x] Visibilidade de DLQ: endpoint `GET /dlq/status` (consulta a API de
      management do RabbitMQ, sem dependência nova — usa `urllib` da
      biblioteca padrão) devolve a contagem de mensagens presas em cada
      fila `<portal>.dlq`. Front mostra um banner vermelho no topo quando
      qualquer portal tem mensagem parada, checando a cada 20s (mais
      espaçado que os 5s da tabela de pedidos, já que DLQ muda bem mais
      devagar). Não é alerta "empurrado" (sem Slack/e-mail configurado
      nesse ambiente) — é visibilidade direta no painel. Testado num
      navegador real com mensagens reais na DLQ (deixadas por falhas de
      teste anteriores nesta mesma sessão)
- [ ] Migrar o worker do Atende.Net a partir do `SsaMonitorProcessos`
- [x] **Reteste dos portais bloqueados por WAF**: TRT9, MPF, MPT, FGTS e
      Prefeitura de Curitiba (CND) continuam bloqueados, confirmado com
      navegador real (não só `curl`, que mostrava HTTP 200 enganoso — os
      WAFs devolvem a própria tela de bloqueio com status 200). SEFAZ PR
      mudou: a landing page passou a carregar sem bloqueio de borda —
      levou à construção do worker abaixo
- [x] Suporte a **reCAPTCHA Enterprise** adicionado ao módulo de captcha
      (`resolver_recaptcha_enterprise`) e hook de interceptação de
      callback (`HOOK_SCRIPT_RECAPTCHA_ENTERPRISE_CALLBACK`, análogo ao
      já existente pra hCaptcha) no `AutomacaoNodriverBase`
- [ ] Worker do **SEFAZ PR** construído (`worker-sefaz-pr`), mas
      **bloqueado num ponto anterior ao do CNPJ+QSA**: testado com
      captcha real (3 tentativas via retry automático) e o próprio
      2captcha devolveu `ERROR_CAPTCHA_UNSOLVABLE` — não é rejeição do
      portal, é o serviço de resolução não conseguindo nem gerar um token
      pra esse reCAPTCHA Enterprise específico. Não depende de código
      nosso; precisaria de outro provedor de captcha especializado em
      Enterprise pra ter alguma chance
- [x] **Passada de regressão** (`docker compose down` + `up --build` do
      zero, depois retestando os 5 portais validados um por um com
      captcha/dado real): encontradas e corrigidas 3 regressões reais
      causadas por mudanças nos próprios sites (não por código nosso
      quebrado):
      1. Receita Federal (Certidão Conjunta) passou a mostrar um banner
         de cookies que bloqueava o clique inicial — resolvido fechando o
         banner antes de prosseguir.
      2. O mesmo worker também parou de ter os campos preenchidos
         reconhecidos pela validação do site (`.value = X` deixou de
         bastar) — resolvido usando o setter nativo do `HTMLInputElement`
         mais eventos input/change/blur.
      3. Certidão de Cadastro de Imóvel e Consulta de Débitos (mesmo
         padrão de código) caíam ocasionalmente em `resultado_indefinido`
         mesmo com a mensagem certa na tela, porque `innerText` quebra a
         frase com `\n` no meio — resolvido normalizando espaços em
         branco antes de comparar.
      Confirmado que os 5 portais continuam funcionando depois das
      correções (CPF, TST-CNDT, Certidão de Cadastro de Imóvel e Consulta
      de Débitos com `sucesso_confirmado` real; Certidão Conjunta com o
      mecanismo comprovadamente corrigido, mas sem `sucesso_confirmado`
      nessa rodada por um bloqueio de anti-abuso aparentemente ligado ao
      CPF de teste reutilizado à exaustão nesta sessão, não ao código)
- [x] Worker de Consulta de Débitos/Dívida Ativa (Prefeitura de Curitiba)
      validado de ponta a ponta com captcha real e dado real — mesma
      plataforma/captcha do worker de Certidão de Cadastro de Imóvel
      (`worker-curitiba-debitos-divida-ativa`). É consulta informativa,
      não uma certidão formal, mas automatizada a pedido do usuário.
      Corrigidos dois bugs reais no caminho: (1) a mensagem "sem débito"
      real ("Não foram encontrados débitos...") não batia com o texto que
      eu tinha suposto ("nenhum débito"), fazendo a consulta ser
      classificada errado como "com débito"; (2) `page.evaluate()` às
      vezes devolve um objeto de erro do CDP em vez de string quando roda
      no meio de uma navegação, derrubando o worker com
      `AttributeError` — corrigido aqui e replicado defensivamente nos
      workers de CPF, CNPJ+QSA, TRF4, TST-CNDT e Certidão de Cadastro de
      Imóvel, que tinham o mesmo padrão frágil
- [x] Worker do CPF — Situação Cadastral validado de ponta a ponta contra
      o site real, com `TWOCAPTCHA_API_KEY` real: hCaptcha resolvido,
      comprovante emitido com sucesso ("Situação Cadastral: REGULAR") e
      PDF gerado corretamente (via `Page.printToPDF`, já que esse portal
      não dispara download nativo — só renderiza o comprovante como HTML)
- [ ] **Worker do CNPJ+QSA bloqueado**: a mecânica de preenchimento e
      submissão funciona (inclusive interceptação do callback do hCaptcha,
      necessária porque o Angular não reconhece a resposta só pela
      textarea), mas o backend rejeita o token do captcha mesmo assim.
      Hipótese de reCAPTCHA descartada (`ERROR_WRONG_GOOGLEKEY`). Suspeita
      principal: validação de IP entre quem resolve o captcha (2captcha) e
      quem submete (este worker) — precisaria de proxy, não só código. Ver
      aviso no topo de `services/worker-cnpj-qsa/worker.py`
- [x] Worker da CNDT (TST) validado de ponta a ponta contra o site real,
      com `TWOCAPTCHA_API_KEY` real: captcha de imagem simples (não
      reCAPTCHA/hCaptcha) resolvido, certidão emitida e PDF baixado com
      sucesso — sistema JSF/RichFaces antigo, sem proteção de borda
- [ ] **Worker do TRF4/JFPR (Certidão Cível/Criminal/Eleitoral) — submissão
      100% validada com captcha real** (5 emissões confirmadas contra o
      site real: nome/CPF batendo com a Receita Federal, certidão gerada
      no sistema do TRF4, sem erro). O último passo (baixar o PDF
      assinado) esbarra num **bug do próprio site do TRF4**: o botão
      "Visualizar Certidão Gerada" aponta pra um caminho quebrado —
      confirmado de 3 formas independentes (clique no navegador, `curl`
      isolado, navegação direta com a URL resolvida corretamente via
      `urljoin`), sempre 404/Bad Request. Não depende mais do nosso
      código — só falta o TRF4 corrigir o link deles. Ver aviso no topo de
      `services/worker-trf4-certidao/worker.py`
- [x] Worker da Certidão de Cadastro de Imóvel (Prefeitura de Curitiba)
      validado de ponta a ponta com captcha real e dado real (Indicação
      Fiscal fornecida pelo usuário a partir de uma declaração já emitida
      manualmente): captcha de imagem simples resolvido, PDF final baixado
      corresponde exatamente à declaração de referência. Esse portal
      estava marcado como "link suspeito" (token de sessão expirado) —
      bastou abrir direto na raiz do domínio pra funcionar normalmente.
      Duas mecânicas não óbvias documentadas no topo do worker: (1) a
      consulta bem-sucedida abre uma tela de confirmação, não o documento —
      precisa clicar "Imprimir Declaração"; (2) esse clique abre uma aba
      nova apontando pro PDF, mas o Chromium não baixa PDFs sozinho (abre
      no visualizador embutido) — foi preciso usar `page.download_file()`
      com `page.set_download_path()` explícito
- [ ] SEFAZ PR e Prefeitura de Curitiba (CND) ficaram **bloqueados** por
      proteção de borda pesada (reCAPTCHA Enterprise com pontuação de
      risco, e Akamai Bot Manager por IP de datacenter, respectivamente)
      — não é problema de código, precisaria de infraestrutura adicional
      (proxy residencial). Documentado em `docs/CATALOGO_PORTAIS.md`
- [ ] Ver `docs/CATALOGO_PORTAIS.md` para a lista completa de portais a
      automatizar, com status de reconhecimento e prioridade sugerida

## Arquitetura de classes dos workers (`certidoes_core.automacao`)

Todo worker baseado em navegador herda de `AutomacaoPortal` (contrato:
banco, tentativas, retry) → `AutomacaoNodriverBase` (ciclo de vida do
Chromium via nodriver + regra fixa de "sempre captura evidência quando o
resultado não for sucesso confirmado", inclusive `sucesso_provável`) →
classe concreta do portal, que só implementa `preencher_e_emitir()`. Ver
`services/worker-receita-federal/worker.py` e `services/worker-cpf/worker.py`
como exemplos. Nomeação de certidão/evidência é padronizada via
`certidoes_core.nomenclatura` (`NOME_PESSOA_portal_documento.pdf`).
