import { Elysia } from 'elysia';
import { Database } from 'bun:sqlite';
import { readFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { request as http, type IncomingMessage } from 'node:http';
import { request as https } from 'node:https';
import { Readable } from 'node:stream';
import { gzipSync, gunzipSync, zstdCompressSync, zstdDecompressSync } from 'node:zlib';
import { S } from './settings';
import { json, hash, tokens, turns, transcript, textRefs, clip, type Item } from './utils';

if (S.db !== ':memory:') mkdirSync(dirname(S.db),{recursive:true});
export const db = new Database(S.db,{create:true});
export function initCache(database:Database) {
  database.exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
    CREATE TABLE IF NOT EXISTS compressed (source_hash TEXT NOT NULL, kind TEXT NOT NULL, settings_hash TEXT NOT NULL, output TEXT NOT NULL, output_hash TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(source_hash,kind,settings_hash));
    CREATE INDEX IF NOT EXISTS compressed_output ON compressed(kind,output_hash);`);
}
initCache(db);
const cached = db.query<{output:string},[string,string,string]>('SELECT output FROM compressed WHERE source_hash=? AND kind=? AND settings_hash=?');
const rendered = db.query('SELECT 1 FROM compressed WHERE kind=? AND output_hash=? LIMIT 1');
const save = db.query('INSERT OR IGNORE INTO compressed VALUES (?,?,?,?,?,?)');
const jobs = new Map<string,Promise<string|null>>(), waiting: (()=>void)[] = [];
let running = 0;
const stats = {requests:0,blocks:0,tools:0,hits:0,errors:0,clipped:0};
class HttpError extends Error { constructor(public status:number,message:string) {super(message);} }
const response = (value:any,status=200,headers:Record<string,string>={}) => new Response(json(value),{status,headers:{'Content-Type':'application/json',...headers}});

async function summarize(source:any, input:string, kind:'block'|'tool', outputHash:(output:string)=>string): Promise<string|null> {
  const sourceHash = hash(source), system = kind === 'block' ? S.blockPrompt : S.toolPrompt;
  const settingsHash = hash([S.url,S.model,system,S.words,S.maxTokens,S.temperature]), key = json([sourceHash,kind,settingsHash]);
  const hit = cached.get(sourceHash,kind,settingsHash);
  if (hit) {stats.hits++; return hit.output;}
  const existing=jobs.get(key);
  if (existing) return new Promise(resolve=>{const timer=setTimeout(()=>resolve(null),S.timeout); existing.then(output=>{clearTimeout(timer); resolve(output);});});
  const job = Promise.resolve().then(async () => {
    if (running >= S.workers) await new Promise<void>(resolve=>waiting.push(resolve)); else running++;
    try {
      let apiKey = S.apiKey;
      if (!apiKey && S.auth) {const auth=JSON.parse(readFileSync(S.auth,'utf8'))[S.provider]; apiKey=auth?.key ?? auth?.apiKey ?? '';}
      const result = await fetch(S.url,{method:'POST',headers:{'Content-Type':'application/json',...(apiKey?{Authorization:`Bearer ${apiKey}`}:{})},
        body:json({model:S.model,messages:[{role:'system',content:system},{role:'user',content:`${input}\n\nTarget approximately ${S.words} words; preserve important details.`}],max_tokens:S.maxTokens,temperature:S.temperature}),signal:AbortSignal.timeout(S.timeout)});
      if (!result.ok) throw new Error(`Condenser HTTP ${result.status}`);
      const payload=await result.json() as Item;
      const answer=payload.choices?.[0]?.message?.content;
      if (typeof answer!=='string') throw new Error('Invalid condenser output');
      const output=answer.replace(/<think>[\s\S]*?<\/think>/gi,'').trim();
      if (!output) throw new Error('Empty condenser output');
      save.run(sourceHash,kind,settingsHash,output,outputHash(output),Date.now()/1000);
      stats[kind==='block'?'blocks':'tools']++; return output;
    } catch {stats.errors++; return null;}
    finally {const next=waiting.shift(); if (next) next(); else running--; jobs.delete(key);}
  });
  jobs.set(key,job);
  return new Promise(resolve=>{const timer=setTimeout(()=>resolve(null),S.timeout); job.then(output=>{clearTimeout(timer); resolve(output);});});
}
export async function compressBody(original: Item): Promise<{body:Item;changed:boolean}> {
  const key=Array.isArray(original.messages)?'messages':Array.isArray(original.input)?'input':null;
  if (!key) return {body:original,changed:false};
  if (original[key].some((item:any)=>!item || typeof item!=='object' || Array.isArray(item))) throw new HttpError(400,'messages must contain objects');
  const body=structuredClone(original), [prefix,groups]=turns(body[key]);
  const summaryItem=(output:string) => ({role:'user',content:key==='messages'?output:[{type:'input_text',text:output}]});
  let changed=false;
  // Group turns BEFORE either compression pass. Never detach live tool calls from their results.
  if (tokens(body)>S.tokens) {
    const completed=Math.max(0,groups.length-S.keepTurns);
    await Promise.all(groups.slice(0,completed).map(async (block,index)=>{
      if (rendered.get('block',hash(block))) {stats.hits++; return;}
      const output=await summarize(block,transcript(block),'block',output=>hash([summaryItem(output)]));
      // ponytail: an outage uses an uncached excerpt; a later request can still obtain a real summary.
      groups[index]=[summaryItem(output??clip(transcript(block),S.excerpt))]; changed=true;
    }));
  }
  await Promise.all(groups.flat().flatMap(item=>textRefs(item,true)).map(async ref=>{
    const value=ref.owner[ref.key];
    if (tokens(value)<=S.toolTokens) return;
    if (rendered.get('tool',hash(value))) {stats.hits++; return;}
    const output=await summarize(value,`tool_output:\n${value}\n---\n---------------------------`,'tool',hash);
    const replacement=output && Array.from(output).length<Array.from(value).length ? output : clip(value,Math.min(S.excerpt,Math.floor(Array.from(value).length/2)));
    if (Array.from(replacement).length<Array.from(value).length) {ref.owner[ref.key]=replacement; changed=true;}
  }));
  body[key]=[...prefix,...groups.flat()];
  while (tokens(body)>S.tokens && groups.length>S.keepTurns) {groups.shift(); body[key]=[...prefix,...groups.flat()]; changed=true;}
  while (tokens(body)>S.tokens) {
    const refs=groups.flat().flatMap(item=>textRefs(item)).sort((a,b)=>Array.from(b.owner[b.key]).length-Array.from(a.owner[a.key]).length);
    const largest=refs[0]; if (!largest || Array.from(largest.owner[largest.key]).length<=S.trimFloor) break;
    largest.owner[largest.key]=clip(largest.owner[largest.key],Math.max(S.trimFloor,Math.floor(Array.from(largest.owner[largest.key]).length/2))); stats.clipped++; changed=true;
  }
  if (tokens(body)>S.tokens) throw new HttpError(413,'fixed request fields exceed token budget');
  return {body:changed?body:original,changed};
}
const hop=new Set(['connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade']);
export function resolveUpstream(route:string): [string,string] | null {
  if (route==='/codex/responses') return [S.upstream,route];
  const match=/^\/r\/([a-z0-9_-]+)(\/.*)$/.exec(route);
  return match && Object.hasOwn(S.routes,match[1]) ? [S.routes[match[1]],match[2]] : null;
}
async function readBody(request:Request) {
  const declared=request.headers.get('content-length');
  if (declared!==null && !/^\d+$/.test(declared)) throw new HttpError(400,'invalid content length');
  if (Number(declared)>S.maxBody) throw new HttpError(413,'request body too large');
  const reader=request.body?.getReader(), parts:Uint8Array[]=[]; let size=0;
  if (reader) try {while (true) {const {done,value}=await reader.read(); if (done) break; size+=value.length; if (size>S.maxBody) {await reader.cancel(); throw new HttpError(413,'request body too large');} parts.push(value);}} finally {reader.releaseLock();}
  if (declared!==null && Number(declared)!==size) throw new HttpError(400,'invalid request body size');
  return Buffer.concat(parts);
}
function send(url:URL,request:Request,body?:Uint8Array,retry=true):Promise<IncomingMessage> {
  const excluded=new Set([...hop,'host','content-length','accept-encoding',...(request.headers.get('connection')?.toLowerCase().split(',').map(s=>s.trim())??[])]);
  const headers=Object.fromEntries([...request.headers].filter(([key])=>!excluded.has(key.toLowerCase())));
  if (body) headers['content-length']=String(body.length);
  return new Promise((resolve,reject)=>{
    const outgoing=(url.protocol==='https:'?https:http)(url,{method:request.method,headers},resolve);
    const abort=()=>outgoing.destroy(new Error('client disconnected'));
    request.signal.addEventListener('abort',abort,{once:true}); outgoing.once('close',()=>request.signal.removeEventListener('abort',abort));
    outgoing.setTimeout(600_000,()=>outgoing.destroy(new Error('upstream timeout')));
    outgoing.on('error',(error:NodeJS.ErrnoException)=>retry && ['EAI_AGAIN','ENOTFOUND'].includes(error.code!) ? send(url,request,body,false).then(resolve,reject) : reject(error));
    outgoing.end(body);
  });
}
export const app=new Elysia().all('*',async ({request})=>{
  try {
    const url=new URL(request.url), route=url.pathname.replace(/\/+$/,'');
    if (request.method==='GET' && route==='/health') return response({status:'ok'});
    if (request.method==='GET' && route==='/stats') return response({...stats,cache:db.query('SELECT count(*) AS n FROM compressed').get()});
    if (!['GET','POST'].includes(request.method)) throw new HttpError(405,'method not allowed');
    if (request.method==='POST' && route==='/compress') {
      let value:Item; try {value=JSON.parse((await readBody(request)).toString('utf8'));} catch(error) {if (error instanceof HttpError) throw error; throw new HttpError(400,'invalid JSON');}
      if (!value || typeof value!=='object' || Array.isArray(value)) throw new HttpError(400,'invalid JSON');
      const result=await compressBody(value); return response(result.body,200,{'X-Context-Router':result.changed?'compacted':'pass'});
    }
    const upstream=resolveUpstream(route); if (!upstream) throw new HttpError(404,'not found');
    const target=new URL(upstream[0]); target.pathname=target.pathname.replace(/\/+$/,'')+upstream[1]; target.search=url.search;
    let body:Uint8Array|undefined, changed=false;
    if (request.method==='POST') {
      const encoding=request.headers.get('content-encoding')?.toLowerCase()||'identity';
      if (!['identity','gzip','zstd'].includes(encoding)) throw new HttpError(415,'unsupported content encoding');
      const raw=await readBody(request); let decoded:Uint8Array;
      try {decoded=encoding==='gzip'?gunzipSync(raw,{maxOutputLength:S.maxBody}):encoding==='zstd'?zstdDecompressSync(raw,{maxOutputLength:S.maxBody}):raw;} catch {throw new HttpError(400,'invalid compressed request body');}
      if (decoded.length>S.maxBody) throw new HttpError(413,'request body too large');
      let value:Item|null=null; try {value=JSON.parse(Buffer.from(decoded).toString('utf8'));} catch {}
      body=raw;
      if (value && typeof value==='object' && !Array.isArray(value)) {
        const result=await compressBody(value); changed=result.changed;
        if (changed) {const wire=Buffer.from(json(result.body)); body=encoding==='gzip'?gzipSync(wire):encoding==='zstd'?zstdCompressSync(wire):wire;}
      }
      stats.requests++;
    }
    const upstreamResponse=await send(target,request,body), headers=new Headers();
    const excluded=new Set([...hop,...String(upstreamResponse.headers.connection??'').toLowerCase().split(',').map(s=>s.trim())]);
    for (let i=0;i<upstreamResponse.rawHeaders.length;i+=2) if (!excluded.has(upstreamResponse.rawHeaders[i].toLowerCase())) headers.append(upstreamResponse.rawHeaders[i],upstreamResponse.rawHeaders[i+1]);
    headers.set('X-Context-Router',changed?'compacted':'pass');
    const status=upstreamResponse.statusCode!;
    if ([204,304].includes(status)) {upstreamResponse.resume(); return new Response(null,{status,headers});}
    return new Response(Readable.toWeb(upstreamResponse) as unknown as ReadableStream<Uint8Array>,{status,headers});
  } catch(error:any) {
    if (error instanceof HttpError) return response({error:error.message},error.status);
    if (error.name==='SQLiteError' || error.code?.startsWith('SQLITE')) return response({error:'cache database unavailable'},503);
    return response({error:'context router upstream failure'},502);
  }
},{parse:'none'});
if (import.meta.main) {
  app.listen({hostname:S.host,port:S.port,maxRequestBodySize:S.maxBody,idleTimeout:0});
  console.log(`ctx-engine listening on ${S.host}:${app.server!.port}`);
  const stop=async()=>{await app.stop(true); db.close(); process.exit(0);};
  process.on('SIGTERM',stop); process.on('SIGINT',stop);
}
