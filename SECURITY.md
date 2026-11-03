# Política de segurança

## Versões suportadas

A última versão publicada no npm recebe correções de segurança.

## Como reportar

Não abra uma issue pública para vulnerabilidades. Use o canal privado do GitHub:

**Security → Report a vulnerability** em
https://github.com/samuelrms/omni-ai-cli/security/advisories/new

Descreva o problema, o impacto e um passo a passo mínimo para reproduzir. A resposta sai
assim que possível, e a correção é coordenada antes de qualquer divulgação pública.

## Escopo

Chaves de API, sessões e logs ficam só na máquina do usuário, com permissão restrita. O log
de uso nunca grava o texto das perguntas. Antes de enviar ao OpenRouter, dados pessoais são
mascarados por padrões — o mascaramento ajuda, mas não garante anonimização total.
