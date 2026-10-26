# Omni AI OpenRouter simulation

Local mock derived from official OpenRouter docs and Orbit contracts; no owner mock imported.

## Purpose

Exercise HTTP masking, native fallback request format and quota response without real credentials.

## Execution

From repository root, start `postman simulation run postman/simulations/openrouter.sim.yaml --port 4901 --no-history --output ndjson`.
Wait for `listening`; run `SMART_AI_SIM_URL=http://127.0.0.1:4901/openrouter python3 -m unittest discover -s tests -v`.
Stop using Ctrl-C or `pkill -INT -f 'simulation run postman/simulations/openrouter.sim.yaml'`.

## Assumptions

- 2026-10-26: tests patch only the public base URL and use a synthetic API key. The mock simulates server-side model selection; it cannot prove OpenRouter availability or live quotas (observed in tests/test_openrouter.py).

## Observations

<!-- begin:observations -->
2026-10-26 terminal run: 16 tests passed. The sole member `openrouter` served GET /models and POST /chat/completions with 200 and 429; none silent. The masked request event contained neither synthetic email in clear text.
<!-- end:observations -->

## What didn't work

- 2026-10-26: sandbox prevented opening/connecting to localhost (EPERM). Run local HTTP checks with appropriate sandbox approval.
