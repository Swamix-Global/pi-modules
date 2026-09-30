import { test, expect, afterAll } from 'bun:test';
import { Database } from 'bun:sqlite';
import { gzipSync, zstdCompressSync } from 'node:zlib';
import { hash, json, tokens, turns, transcript, textRefs } from '../utils';
let calls=0, mode='ok', recorded:Uint8Array|undefined, recordedUrl='', prompt='', streamController:ReadableStreamDefaultController<Uint8Array>|undefined;
const upstream=Bun.serve({hostname:'127.0.0.1',port:0,async fetch(request) {
  if (new URL(request.url).pathname==='/condense') {
    calls++; const payload=await request.json() as any; prompt=payload.messages[1].content;
    if (mode==='fail') return new Response('unavailable',{status:503});
    return Response.json({choices:[{message:{content:'Concise useful result with exact identifiers.'}}]});
  }
  if (new URL(request.url).pathname==='/stream') return new Response(new ReadableStream({start(c){streamController=c;c.enqueue(Buffer.from('data: first\n\n'));}}),{headers:{'Content-Type':'text/event-stream'}});
  recordedUrl=request.url; recorded=new Uint8Array(await request.arrayBuffer()); return new Response(recorded as Uint8Array<ArrayBuffer>,{headers:{'Content-Type':'application/octet-stream','X-Upstream-Auth':request.headers.get('authorization')??''}});
}});
Object.assign(process.env,{PI_CONTEXT_ROUTER_DB:':memory:',PI_CONTEXT_ROUTER_AUTH:'',PI_CONTEXT_ROUTER_API_KEY:'test',PI_CONTEXT_ROUTER_TOKENS:'600',PI_CONTEXT_ROUTER_TOOL_OUTPUT_TOKENS:'80',PI_CONTEXT_ROUTER_KEEP_TURNS:'2',PI_CONTEXT_ROUTER_TRIM_FLOOR:'40',PI_CONTEXT_ROUTER_FALLBACK_CHARS:'180',PI_CONTEXT_ROUTER_WAIT_MS:'300',PI_CONTEXT_ROUTER_WORKERS:'2',PI_CONTEXT_ROUTER_CONDENSER_URL:`${upstream.url}condense`,PI_CONTEXT_ROUTER_ROUTES:json({test:upstream.url.toString()}),PI_CONTEXT_ROUTER_UPSTREAM:upstream.url.toString(),PI_CONTEXT_ROUTER_MAX_BODY_BYTES:'16384'});
const { app, db, compressBody, initCache }=await import('../main');
app.listen({hostname:'127.0.0.1',port:0,idleTimeout:0});
const base=`http://127.0.0.1:${app.server!.port}`;
afterAll(async()=>{await app.stop(true); await upstream.stop(true); db.close();});
const longBody=()=>({model:'test',messages:[{role:'system',content:'Preserve instructions.'},{role:'user',content:'old query '+ 'x'.repeat(1300)},{role:'assistant',content:'old answer '+ 'y'.repeat(1300)},{role:'user',content:'previous query'},{role:'assistant',content:'previous answer'},{role:'user',content:'live query'},{role:'assistant',content:null,tool_calls:[{id:'c1',type:'function',function:{name:'read',arguments:'{"path":"exact"}'}}]}]});
test('group turns before rendering all five section types',()=>{
  const items=[{role:'system',content:'keep'},{role:'user',content:'question'},{role:'assistant',content:'answer',tool_calls:[{function:{name:'read',arguments:'{}'}}]},{role:'tool',tool_call_id:'c',content:'result'},{type:'reasoning',summary:[{text:'thought'}]},{role:'user',content:'next'}];
  const [prefix,groups]=turns(items); expect(prefix).toEqual([items[0]]); expect(groups.map(g=>g.length)).toEqual([4,1]);
  expect(transcript(groups[0])).toBe('user:\nquestion\n---\nassistant:\nanswer\n---\ntool_call:\nread({})\n---\ntool_output:\nresult\n---\nthinking:\nthought\n---\n---------------------------');
  expect(transcript([groups[0][0],groups[0][3],groups[0][1],groups[0][2]])).toBe(transcript(groups[0]));
  expect(hash({b:1,a:2})).toBe(hash({a:2,b:1}));
});
test('blocks compress to plain content; cache and rendered hashes prevent repeat calls',async()=>{
  const original=longBody(), frozen=json(original), before=calls;
  const first=await compressBody(original); expect(first.changed).toBe(true); expect(calls-before).toBe(1); expect(json(original)).toBe(frozen);
  expect(first.body.messages[1]).toEqual({role:'user',content:'Concise useful result with exact identifiers.'});
  expect(json(first.body)).not.toContain('<compacted>'); expect(first.body.messages.slice(-4)).toEqual(original.messages.slice(-4)); expect(tokens(first.body)).toBeLessThanOrEqual(600);
  expect(prompt).toContain('user:\nold query'); expect(prompt).toContain('\n---\nassistant:');
  await compressBody(original); expect(calls-before).toBe(1);
  const repeatCalls=calls;
  await compressBody({...first.body,messages:[...first.body.messages,{role:'user',content:'new turn'}, {role:'assistant',content:'z'.repeat(2000)}]});
  expect(calls-repeatCalls).toBe(1); // Only the newly settled turn, never the plain cached note.
  expect(db.query("SELECT count(*) n FROM compressed WHERE kind='block'").get()).toBeTruthy();
});
test('concurrent identical blocks deduplicate by internal hash',async()=>{
  const body=longBody(); body.messages[1].content='different old query '+ 'q'.repeat(1300);
  const before=calls; const [a,b]=await Promise.all([compressBody(body),compressBody(body)]); expect(a.body).toEqual(b.body); expect(calls-before).toBe(1);
});
test('tool output only: preserve tool-call ids, arguments and images',async()=>{
  const image={type:'image_url',image_url:{url:'data:image/png;base64,'+'X'.repeat(3000)}};
  const body={messages:[{role:'user',content:'read file'},{role:'assistant',content:null,tool_calls:[{id:'keep-id',function:{name:'read',arguments:'{"path":"exact"}'}}]},{role:'tool',tool_call_id:'keep-id',content:[{type:'text',text:'large tool output '+ 'A'.repeat(500)},image]}]};
  const before=calls, result=await compressBody(body);
  expect(calls-before).toBe(1); expect(result.body.messages[1]).toEqual(body.messages[1]); expect(result.body.messages[2].tool_call_id).toBe('keep-id'); expect(result.body.messages[2].content[1]).toEqual(image);
  expect(result.body.messages[2].content[0].text).toBe('Concise useful result with exact identifiers.');
  await compressBody(body); await compressBody(result.body); expect(calls-before).toBe(1);
  expect(textRefs(body.messages[1],true)).toEqual([]);
});
test('Responses and Anthropic tool results retain their native wrappers',async()=>{
  const body={input:[{role:'user',content:[{type:'input_text',text:'query'}]},{type:'function_call',name:'read',call_id:'keep',arguments:'{}'},{type:'function_call_output',call_id:'keep',output:'B'.repeat(500)}]};
  const result=await compressBody(body); expect(result.body.input[1]).toEqual(body.input[1]); expect(result.body.input[2].call_id).toBe('keep'); expect(result.body.input[2].output.length).toBeLessThan(500);
  const anthropic={messages:[{role:'user',content:'question'},{role:'assistant',content:[{type:'tool_use',id:'same',name:'read',input:{}}]},{role:'user',content:[{type:'tool_result',tool_use_id:'same',content:'C'.repeat(500)}]}]};
  expect(turns(anthropic.messages)[1].length).toBe(1);
  const compressed=await compressBody(anthropic); expect(compressed.body.messages[2].content[0].tool_use_id).toBe('same'); expect(compressed.changed).toBe(true);
});
test('failed condensation uses an uncached excerpt; retry can recover',async()=>{
  const body=longBody(); body.messages[1].content='outage '+ 'Q'.repeat(1300); mode='fail';
  const failed=await compressBody(body); expect(failed.changed).toBe(true); expect(tokens(failed.body)).toBeLessThanOrEqual(600);
  const count=(db.query('SELECT count(*) n FROM compressed').get() as any).n; mode='ok';
  const recovered=await compressBody(body); expect(recovered.body.messages[1].content).toBe('Concise useful result with exact identifiers.'); expect((db.query('SELECT count(*) n FROM compressed').get() as any).n).toBe(count+1);
});
test('immutable fields over budget fail instead of forwarding oversized requests',async()=>{
  await expect(compressBody({instructions:'x'.repeat(4000),input:[{role:'user',content:'small'}]})).rejects.toThrow('fixed request fields exceed token budget');
});
test('proxy passes original bytes and credentials, including gzip and zstd',async()=>{
  const raw=Buffer.from('{ "model": "test", "messages": [{"role":"user","content":"small"}] }');
  for (const [encoding,body] of [['identity',raw],['gzip',gzipSync(raw)],['zstd',zstdCompressSync(raw)]] as const) {
    const result=await fetch(`${base}/r/test/echo?key=value`,{method:'POST',headers:{'Content-Encoding':encoding,Authorization:'Bearer caller'},body});
    expect(result.status).toBe(200); expect(result.headers.get('X-Context-Router')).toBe('pass'); expect(result.headers.get('X-Upstream-Auth')).toBe('Bearer caller'); expect(Buffer.from(await result.arrayBuffer())).toEqual(Buffer.from(body));
  }
  expect((await fetch(`${base}/r/unknown/models`)).status).toBe(404);
  expect((await fetch(`${base}/r/test/encoded%2Fsegment?key=%2F`)).status).toBe(200);
  expect(new URL(recordedUrl).pathname).toBe('/encoded%2Fsegment'); expect(new URL(recordedUrl).search).toBe('?key=%2F');
});
test('proxy streams the first chunk without waiting for the end',async()=>{
  const result=await fetch(`${base}/r/test/stream`), reader=result.body!.getReader();
  expect(new TextDecoder().decode((await reader.read()).value)).toBe('data: first\n\n');
  streamController!.enqueue(Buffer.from('data: last\n\n')); streamController!.close();
  expect(new TextDecoder().decode((await reader.read()).value)).toBe('data: last\n\n'); expect((await reader.read()).done).toBe(true);
});
test('explicit Elysia routes reject unrelated paths and methods',async()=>{
  expect(app.routes.map(({method,path})=>`${method} ${path}`)).toEqual(['GET /health','GET /stats','POST /compress','GET /r/:provider/*','POST /r/:provider/*','GET /codex/responses','POST /codex/responses']);
  const before=calls;
  for (const [method,path] of [['GET','/unknown'],['POST','/health'],['GET','/compress'],['DELETE','/r/test/echo'],['POST','/r/unknown/echo']]) {
    expect((await fetch(`${base}${path}`,{method})).status).toBe(404);
  }
  expect(calls).toBe(before);
  for (const method of ['GET','POST']) expect((await fetch(`${base}/codex/responses`,{method,...(method==='POST'?{body:json({messages:[{role:'user',content:'small'}]})}:{})})).status).toBe(200);
});
test('HTTP health, validation, compression and native cache schema',async()=>{
  expect(await (await fetch(`${base}/health`)).text()).toBe('{"status":"ok"}');
  const post=(body:string)=>fetch(`${base}/compress`,{method:'POST',headers:{'Content-Type':'application/json'},body});
  expect((await post('{bad')).status).toBe(400); expect((await post('null')).status).toBe(400); expect((await post('{"messages":[null]}')).status).toBe(400);
  expect((await post(json(longBody()))).status).toBe(200);
  expect((await fetch(`${base}/r/test/echo`,{method:'POST',headers:{'Content-Encoding':'gzip'},body:'not gzip'})).status).toBe(400);
  expect((db.query('PRAGMA table_info(compressed)').all() as any[]).filter(col=>col.pk).sort((a,b)=>a.pk-b.pk).map(col=>col.name)).toEqual(['source_hash','kind','settings_hash']);
  const legacy=new Database(':memory:'); legacy.exec('CREATE TABLE summaries(digest TEXT,summary TEXT); INSERT INTO summaries VALUES("old","keep")'); initCache(legacy); initCache(legacy); expect(legacy.query('SELECT summary FROM summaries').get()).toEqual({summary:'keep'}); expect(legacy.query('SELECT count(*) n FROM compressed').get()).toEqual({n:0}); legacy.close();
});
