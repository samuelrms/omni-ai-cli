const http = require('node:http');
http.createServer(async (req, res) => {
  const send = (status, body) => { res.writeHead(status, {'content-type': 'application/json'}); res.end(JSON.stringify(body)); };
  if (req.method === 'GET' && req.url === '/models') return send(200, {data: ['test/a:free', 'test/b:free'].map(id => ({id, pricing: {prompt: '0', completion: '0'}}))});
  if (req.method !== 'POST' || req.url !== '/chat/completions') return send(501, {});
  let raw = ''; for await (const chunk of req) raw += chunk;
  const body = JSON.parse(raw);
  if (body.messages.some(m => m.content === 'QUOTA')) return send(429, {error: {message: 'daily quota'}});
  if (raw.includes('@example.com') || JSON.stringify(body.models) !== JSON.stringify(['test/a:free', 'test/b:free', 'openrouter/free'])) return send(400, {error: {message: 'Invalid masking or fallback contract'}});
  if (body.provider?.max_price?.prompt !== 0 || body.provider?.max_price?.completion !== 0) return send(400, {});
  return send(200, {model: 'test/b:free', choices: [{message: {content: body.messages.map(m => m.content).join(' ')}}]});
}).listen(process.env.PORT);
