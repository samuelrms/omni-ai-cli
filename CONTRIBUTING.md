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

## Versões

A publicação no npm é automática (semantic-release) ao mergear na `main`, por OIDC /
trusted publishing. Ninguém edita `version` no `package.json` à mão.
