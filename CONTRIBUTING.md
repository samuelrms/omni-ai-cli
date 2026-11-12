# Contribuindo

Obrigado por querer melhorar o Omni AI CLI.

## Desenvolvimento

```bash
git clone https://github.com/samuelrms/omni-ai-cli ~/dev/omni-ai-cli
cd ~/dev/omni-ai-cli && ./scripts/install.sh   # Windows: powershell -File scripts\install.ps1
```

O programa é Python puro (sem dependências externas; `prompt_toolkit` e `wcwidth` ficam
embutidos em `omni_ai/vendor/`).

## Testes

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/install.sh
```

Todo módulo de teste começa com `import isolation`, que troca a pasta home, remove chaves de API do ambiente e esconde os CLIs reais. Gravar na home real faz a execução falhar.

Todos os executáveis dos testes são fictícios: nada autentica nem consome cota.

## Diagramas do README

A página do npm não renderiza `mermaid`. As fontes ficam em `docs/diagrams/*.mmd` e são
renderizadas em PNG. Ao mudar um diagrama, rode e faça commit das imagens:

```bash
npm run diagrams
```

## Commits e PRs

- Título no padrão [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `chore:`, `refactor:`, `test:`. É o que gera a versão no release.
- Título em inglês, no presente, descrevendo o que muda para quem usa.
- Um PR por mudança perceptível. Use *squash merge*.
- A seção `## Release notes` do PR, em inglês, entra nas notas do release e aparece em `ai --update` e na aba de mudanças da extensão.
- A `main` é protegida: todo merge passa por PR, com o CI verde e a aprovação do mantenedor.
- Actions nos workflows vão fixadas pelo SHA do commit, com a versão em comentário (`uses: actions/checkout@<sha> # v7.0.1`). Uma tag pode passar a apontar para outro código; um commit, não. O Dependabot atualiza os dois juntos, e um teste recusa action sem SHA.
- As ferramentas que os workflows rodam (semantic-release, vsce, ovsx, coverage) ficam em `tools/release`, com versão exata e lockfile, e são instaladas sem scripts de instalação. Valores como a versão chegam aos scripts por variável de ambiente, nunca com `${{ }}` dentro de `run:`. Os testes de `tests/test_workflows.py` conferem as duas regras.

## Versões

A publicação no npm é automática (semantic-release) ao mergear na `main`, por OIDC /
trusted publishing. Ninguém edita `version` no `package.json` à mão.

## Imagem Docker

As versões das ferramentas ficam nos `ARG` do `Dockerfile`. Ao trocar a versão do Grok ou do Cursor, rode
`scripts/docker-hashes.sh` e copie as quatro linhas de hash que ele imprime. Sem isso, o build falha na conferência,
que é o comportamento esperado. Claude Code e Codex vêm do npm, que confere os pacotes sozinho.
