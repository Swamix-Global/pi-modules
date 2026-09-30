import { test, expect } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("settings and complete history survive a new process without changing .env defaults", async () => {
  const directory = mkdtempSync(join(tmpdir(), "ctx-history-test-"));
  const file = join(directory, "router.db");
  const main = new URL("../main.ts", import.meta.url).href;
  const settings = new URL("../settings.ts", import.meta.url).href;
  const run = async (code: string) => {
    const child = Bun.spawn([process.execPath, "-e", code], {
      cwd: new URL("..", import.meta.url).pathname,
      env: {
        ...process.env,
        PI_CONTEXT_ROUTER_DB: file,
        PI_CONTEXT_ROUTER_SUMMARY_WORDS: "100",
      },
      stdout: "pipe",
      stderr: "pipe",
    });
    const [stdout, stderr, exit] = await Promise.all([
      new Response(child.stdout).text(),
      new Response(child.stderr).text(),
      child.exited,
    ]);
    if (exit !== 0) throw new Error(stderr);
    return JSON.parse(stdout);
  };
  try {
    await run(`import {Database} from 'bun:sqlite';
      const db=new Database(${JSON.stringify(file)});
      db.exec('CREATE TABLE dashboard_settings(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL)');
      db.query('INSERT INTO dashboard_settings VALUES(1,?)').run(JSON.stringify({words:111,temperature:0.1,workers:2,trimFloor:400,timeout:30000,excerpt:1200,toolTokens:100,toolPrompt:'retired'}));
      db.close();console.log('{}');`);
    await run(`import {db,app} from ${JSON.stringify(main)};
      const res = await app.handle(new Request('http://localhost/api/settings',{method:'PUT',body:JSON.stringify({words:222,model:'gpt-6-luna'})}));
      if(res.status!==200) throw new Error(await res.text());
      const saved=JSON.parse(db.query('SELECT value FROM dashboard_settings WHERE id=1').get().value);
      if(Object.keys(saved).sort().join(',')!=='blockPrompt,keepTurns,maxTokens,model,tokens,words') throw new Error('retired controls persisted');
      db.exec("CREATE TABLE summaries(digest TEXT,summary TEXT);INSERT INTO summaries VALUES('old','keep');");
      db.query("INSERT INTO history(created_at,route,model,status,changed,before_tokens,after_tokens,duration_ms,error,original,compressed,changes) VALUES(1,'POST /compress','persist',200,1,100,10,5,NULL,?,?,?)").run(JSON.stringify({text:'FULL ORIGINAL'}),JSON.stringify({text:'SUMMARY'}),JSON.stringify([{kind:'block',before:'FULL ORIGINAL',after:'SUMMARY'}]));
      db.query("INSERT INTO history(created_at,route,model,status,changed,before_tokens,after_tokens,duration_ms,error,original,compressed,changes) VALUES(1,'POST /compress','interrupted',0,0,100,0,0,NULL,?,NULL,'[]')").run(JSON.stringify({text:'INTERRUPTED FULL INPUT'}));
      db.close();console.log('{}');`);
    const result =
      await run(`import {db,app} from ${JSON.stringify(main)};import {S} from ${JSON.stringify(settings)};
      const config=await (await app.handle(new Request('http://localhost/api/settings'))).json();
      const row=await (await app.handle(new Request('http://localhost/api/history/1'))).json();
      const pending=await (await app.handle(new Request('http://localhost/api/history/2'))).json();
      console.log(JSON.stringify({words:S.words,model:S.model,defaults:config.defaults.words,defaultModel:config.defaults.model,row,pending,legacy:db.query('SELECT summary FROM summaries').get()}));db.close();`);
    expect(result.words).toBe(222);
    expect(result.model).toBe("gpt-6-luna");
    expect(result.defaultModel).toBe("deepseek-flash");
    expect(result.defaults).toBe(100);
    expect(result.row.original).toEqual({ text: "FULL ORIGINAL" });
    expect(result.row.compressed).toEqual({ text: "SUMMARY" });
    expect(result.row.changes).toEqual([
      { kind: "block", before: "FULL ORIGINAL", after: "SUMMARY" },
    ]);
    expect(result.pending.status).toBe(502);
    expect(result.pending.error).toBe("router stopped before completion");
    expect(result.pending.original).toEqual({ text: "INTERRUPTED FULL INPUT" });
    expect(result.legacy).toEqual({ summary: "keep" });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
