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
        PI_CONTEXT_ROUTER_TOOL_OUTPUT_TOKENS: "100",
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
    await run(`import {db,app} from ${JSON.stringify(main)};
      const res = await app.handle(new Request('http://localhost/api/settings',{method:'PUT',body:JSON.stringify({toolTokens:222})}));
      if(res.status!==200) throw new Error(await res.text());
      db.exec("CREATE TABLE summaries(digest TEXT,summary TEXT);INSERT INTO summaries VALUES('old','keep');");
      db.query("INSERT INTO history(created_at,route,model,status,changed,before_tokens,after_tokens,duration_ms,error,original,compressed,changes) VALUES(1,'POST /compress','persist',200,1,100,10,5,NULL,?,?,?)").run(JSON.stringify({text:'FULL ORIGINAL'}),JSON.stringify({text:'SUMMARY'}),JSON.stringify([{kind:'tool',before:'FULL ORIGINAL',after:'SUMMARY'}]));
      db.query("INSERT INTO history(created_at,route,model,status,changed,before_tokens,after_tokens,duration_ms,error,original,compressed,changes) VALUES(1,'POST /compress','interrupted',0,0,100,0,0,NULL,?,NULL,'[]')").run(JSON.stringify({text:'INTERRUPTED FULL INPUT'}));
      db.close();console.log('{}');`);
    const result =
      await run(`import {db,app} from ${JSON.stringify(main)};import {S} from ${JSON.stringify(settings)};
      const config=await (await app.handle(new Request('http://localhost/api/settings'))).json();
      const row=await (await app.handle(new Request('http://localhost/api/history/1'))).json();
      const pending=await (await app.handle(new Request('http://localhost/api/history/2'))).json();
      console.log(JSON.stringify({threshold:S.toolTokens,defaults:config.defaults.toolTokens,row,pending,legacy:db.query('SELECT summary FROM summaries').get()}));db.close();`);
    expect(result.threshold).toBe(222);
    expect(result.defaults).toBe(100);
    expect(result.row.original).toEqual({ text: "FULL ORIGINAL" });
    expect(result.row.compressed).toEqual({ text: "SUMMARY" });
    expect(result.row.changes).toEqual([
      { kind: "tool", before: "FULL ORIGINAL", after: "SUMMARY" },
    ]);
    expect(result.pending.status).toBe(502);
    expect(result.pending.error).toBe("router stopped before completion");
    expect(result.pending.original).toEqual({ text: "INTERRUPTED FULL INPUT" });
    expect(result.legacy).toEqual({ summary: "keep" });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
