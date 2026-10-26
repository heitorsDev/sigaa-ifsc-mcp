# sigaa-ifsc-mcp

Servidor [MCP](https://modelcontextprotocol.io) para consultar o portal do
discente do [SIGAA do IFSC](https://sig.ifsc.edu.br/sigaa): perfil, turmas,
notas, frequência, tarefas, notícias e avaliações.

A navegação e a extração de dados foram portadas da biblioteca
[sigaa-api](https://github.com/GeovaneSchmitz/sigaa-api), de Geovane Schmitz.

> Projeto não oficial, sem vínculo com o IFSC.

## Autenticação

O login do SIGAA tem um CAPTCHA (Cloudflare Turnstile), então este servidor
**não digita a sua senha nem resolve o CAPTCHA**. Quem loga é você, numa janela
do navegador que o próprio servidor abre:

1. Você pede algo ao Claude (por exemplo, "quais são minhas notas?").
2. Se não houver sessão válida, o servidor abre uma janela do Edge (no Windows)
   ou do Chromium na tela de login do SIGAA.
3. Você entra normalmente. Quando o portal do discente abre, o servidor captura
   o cookie `JSESSIONID`, fecha a janela e responde ao que você pediu.

O perfil desse navegador fica salvo em `~/.sigaa-mcp/browser-profile`. Se você
deixar o navegador lembrar a senha, os próximos logins são só clicar em "Entrar".

Para logar sem esperar uma consulta, use a ferramenta `login_navegador` ou o
comando `sigaa-mcp login`. Para desligar a janela automática, use
`SIGAA_AUTO_LOGIN=0`.

**Alternativa: colar o cookie.** Entre em <https://sig.ifsc.edu.br/sigaa> no seu
navegador, abra as ferramentas de desenvolvedor (F12) > **Application** >
**Cookies** > `https://sig.ifsc.edu.br`, copie o valor de `JSESSIONID` e chame
`definir_cookie`, ou rode:

```bash
uvx --from git+https://github.com/heitorsDev/sigaa-ifsc-mcp sigaa-mcp cookie SEU_JSESSIONID
```

> O cliente MCP precisa esperar enquanto você loga. No Claude Code, se a
> ferramenta der timeout antes de você terminar, aumente `MCP_TOOL_TIMEOUT`
> (em milissegundos).

### Manter a sessão viva

A sessão do SIGAA expira por inatividade. Enquanto o servidor estiver rodando,
ele acessa o portal a cada 10 minutos (`SIGAA_KEEPALIVE_MIN`) para a sessão não
cair. Para manter a sessão ativa também com o cliente MCP fechado, deixe isto
rodando num terminal:

```bash
uvx --from git+https://github.com/heitorsDev/sigaa-ifsc-mcp sigaa-mcp keepalive
```

O keepalive não evita que a sessão termine se o SIGAA reiniciar ou impuser um
tempo máximo de sessão. Quando ela cair, as ferramentas avisam e explicam como
reconectar.

O cookie fica salvo em `~/.sigaa-mcp/session.json`. **Ele dá acesso à sua conta
enquanto a sessão estiver ativa: não compartilhe esse arquivo.**

## Instalação

Requer o [uv](https://docs.astral.sh/uv/):

```bash
winget install --id=astral-sh.uv -e
```

### Claude Code

```bash
claude mcp add sigaa -- uvx --from git+https://github.com/heitorsDev/sigaa-ifsc-mcp sigaa-mcp
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "sigaa": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/heitorsDev/sigaa-ifsc-mcp", "sigaa-mcp"]
    }
  }
}
```

Fora do Windows, o login por janela usa o Chromium do Playwright:
`uvx --from playwright playwright install chromium`.

## Ferramentas

| Ferramenta | O que faz |
|---|---|
| `definir_cookie(cookie)` | Valida e salva o `JSESSIONID` |
| `login_navegador()` | Abre o navegador para logar e captura o cookie (acontece sozinho quando a sessão expira) |
| `status()` | Diz se a sessão é válida |
| `esquecer_sessao()` | Apaga o cookie salvo |
| `perfil()` | Nome, matrícula, curso, status, período atual |
| `atividades()` | Tarefas, questionários e avaliações próximas do portal |
| `turmas(todos_periodos)` | Turmas do semestre (ou de todos os períodos) |
| `notas(turma)` | Notas por unidade e avaliação |
| `frequencia(turma)` | Faltas por aula, total e máximo permitido |
| `tarefas(turma)` | Tarefas, prazos e se já foram enviadas |
| `avaliacoes(turma)` | Calendário de provas da turma |
| `noticias(turma, com_conteudo, limite)` | Notícias da turma, com o texto |
| `secao_turma(turma, secao)` | Texto de qualquer seção da turma (Arquivos, Plano de Ensino, Participantes...) |
| `abrir_pagina(caminho)` | Texto de uma página do SIGAA com URL fixa |

`turma` aceita o código ou um trecho do nome da disciplina, sem acentos.

## Variáveis de ambiente

| Variável | Padrão | |
|---|---|---|
| `SIGAA_AUTO_LOGIN` | `1` | Abre a janela de login quando a sessão expira; `0` desativa |
| `SIGAA_JSESSIONID` | | Cookie de sessão (tem prioridade sobre o arquivo salvo) |
| `SIGAA_KEEPALIVE_MIN` | `10` | Intervalo do keepalive; `0` desativa |
| `SIGAA_STATE_DIR` | `~/.sigaa-mcp` | Onde ficam o cookie e o perfil do navegador |
| `SIGAA_MAX_CHARS` | `20000` | Limite de texto por página devolvida |
| `SIGAA_LOGIN_TIMEOUT` | `300` | Segundos esperando o login por janela |
| `SIGAA_BROWSER_CHANNEL` | `msedge` no Windows | Navegador do login por janela; vazio usa o Chromium do Playwright |

## Limitações

- Se você tiver mais de um vínculo (por exemplo, dois cursos), o servidor usa o
  vínculo escolhido na sessão do navegador.
- O HTML do SIGAA muda com o tempo; se uma ferramenta parar de funcionar,
  `secao_turma` e `abrir_pagina` ainda devolvem o texto cru da página.
