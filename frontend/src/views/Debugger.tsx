import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type DebugBlock } from "../api";

function BlockReport({ block, index, project, sessionId }: { block: DebugBlock; index: number; project: string; sessionId: string }) {
  const client = useQueryClient();
  const preview = useMutation({
    mutationFn: () => api.debugPreview(project, sessionId, block.id),
    onSuccess: () => client.invalidateQueries({ queryKey: ["debug-session", project, sessionId] }),
  });
  return <article className="min-w-0 border border-line bg-panel">
    <header className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-line px-3 py-2 text-[12px]">
      <h3 className="font-semibold">Block {index + 1} <span className="font-normal text-muted">· {block.source}</span></h3>
      <code className="break-all text-muted">{block.id}</code>
    </header>
    <div className="grid min-w-0 md:grid-cols-2">
      <section className="min-w-0 border-b border-line md:border-b-0 md:border-r" aria-label={`Block ${index + 1} before`}>
        <h4 className="border-b border-line px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted">Before · full text</h4>
        <pre className="m-0 max-h-[70vh] overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-[12px] leading-relaxed text-body">{block.before}</pre>
      </section>
      <section className="min-w-0" aria-label={`Block ${index + 1} after`}>
        <h4 className="border-b border-line px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted">After · stored summary</h4>
        <pre className="m-0 max-h-[70vh] overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-[12px] leading-relaxed text-body">{block.after}</pre>
        <div className="border-t border-line p-2">
          <button type="button" disabled={preview.isPending} onClick={() => preview.mutate()} className="min-h-10 border border-accent px-3 font-semibold text-accent hover:bg-accentbg disabled:opacity-50">{preview.isPending ? "Generating…" : "Generate summary"}</button>
          <span className="ml-2 text-[12px] text-muted">Uses the selected condenser; may incur cost.</span>
          {preview.isError && <p role="alert" className="text-neg">Could not generate summary.</p>}
        </div>
      </section>
    </div>
  </article>;
}

function ErrorMessage({ children }: { children: string }) {
  return <p role="alert" className="border border-[#633333] bg-[#241414] px-3 py-2 text-neg">{children}</p>;
}

export function DebuggerView() {
  const client = useQueryClient();
  const projectsQuery = useQuery({ queryKey: ["debug-projects"], queryFn: api.debugProjects, retry: false });
  const [project, setProject] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [promptText, setPromptText] = useState("");
  const projects = projectsQuery.data?.projects ?? [];

  useEffect(() => {
    if (!project && projects.length) setProject(projects[0].path);
    if (project && !projects.some((item) => item.path === project)) {
      setProject(projects[0]?.path ?? "");
      setSessionId("");
    }
  }, [project, projects]);

  const sessionsQuery = useQuery({
    queryKey: ["debug-sessions", project],
    queryFn: () => api.debugSessions(project),
    enabled: Boolean(project),
    retry: false,
  });
  const sessions = sessionsQuery.data?.sessions ?? [];
  useEffect(() => {
    if (sessionId && !sessions.some((item) => item.id === sessionId)) setSessionId("");
  }, [sessionId, sessions]);

  const selectedSession = sessions.find((item) => item.id === sessionId);
  const detailQuery = useQuery({
    queryKey: ["debug-session", project, sessionId],
    queryFn: () => api.debugSession(sessionId, project),
    enabled: Boolean(project && sessionId),
    retry: false,
  });
  const promptQuery = useQuery({ queryKey: ["debug-prompt"], queryFn: api.debugPrompt, retry: false });
  useEffect(() => {
    if (promptQuery.data) setPromptText(promptQuery.data.prompt);
  }, [promptQuery.data]);

  const savePrompt = useMutation({
    mutationFn: () => api.saveDebugPrompt(promptText),
    onSuccess: (result) => {
      setPromptText(result.prompt);
      client.setQueryData(["debug-prompt"], (current: typeof promptQuery.data) =>
        current ? { ...current, prompt: result.prompt } : current,
      );
    },
  });

  return (
    <div className="space-y-4 py-2">
      <section aria-labelledby="debug-sessions-heading" className="space-y-3">
        <div>
          <h2 id="debug-sessions-heading" className="text-base font-semibold">Condenser debugger</h2>
          <p className="mt-1 text-muted">Inspect completed session blocks. Before and after text is shown in full; summaries are only shown when stored.</p>
        </div>
        {projectsQuery.isLoading && <p role="status" className="text-muted">Loading projects…</p>}
        {projectsQuery.isError && <ErrorMessage>Could not load projects from /debug/projects.</ErrorMessage>}
        {projectsQuery.data && projects.length === 0 && <p className="border border-line bg-panel px-3 py-4 text-muted">No projects with debugger sessions are available.</p>}
        {projects.length > 0 && <div className="grid gap-3 sm:grid-cols-2">
          <label className="block min-w-0 text-[12px] font-medium text-muted">
            Project
            <select value={project} onChange={(event) => { setProject(event.target.value); setSessionId(""); }} className="mt-1 block min-h-10 w-full border border-line bg-panel px-3 text-body">
              {projects.map((item) => <option key={item.path} value={item.path}>{item.path} · {item.sessions} sessions</option>)}
            </select>
          </label>
          <label className="block min-w-0 text-[12px] font-medium text-muted">
            Session
            <select value={sessionId} onChange={(event) => setSessionId(event.target.value)} disabled={!sessions.length} className="mt-1 block min-h-10 w-full border border-line bg-panel px-3 text-body disabled:opacity-60">
              <option value="">{sessionsQuery.isLoading ? "Loading sessions…" : sessions.length ? "Choose a session" : "No sessions"}</option>
              {sessions.map((item) => <option key={item.id} value={item.id}>{item.title || item.id} · {new Date(item.at * 1000).toLocaleString()}</option>)}
            </select>
          </label>
        </div>}
        {sessionsQuery.isLoading && project && <p role="status" className="text-muted">Loading sessions…</p>}
        {sessionsQuery.isError && <ErrorMessage>Could not load sessions for this project.</ErrorMessage>}
        {sessionsQuery.data && sessions.length === 0 && <p className="text-muted">No sessions are stored for this project.</p>}
        {selectedSession && <dl className="grid gap-x-4 gap-y-2 border border-line bg-panel p-3 text-[12px] sm:grid-cols-2 lg:grid-cols-4">
          <div className="min-w-0"><dt className="text-muted">Title</dt><dd className="break-words">{selectedSession.title || "Untitled session"}</dd></div>
          <div className="min-w-0"><dt className="text-muted">Session ID</dt><dd className="break-all font-mono">{selectedSession.id}</dd></div>
          <div className="min-w-0"><dt className="text-muted">Source</dt><dd className="break-words">{selectedSession.source}</dd></div>
          <div><dt className="text-muted">Recorded · blocks</dt><dd>{new Date(selectedSession.at * 1000).toLocaleString()} · {selectedSession.blocks}</dd></div>
        </dl>}
        {detailQuery.isLoading && <p role="status" className="text-muted">Loading completed blocks…</p>}
        {detailQuery.isError && <ErrorMessage>Could not load the selected session’s blocks.</ErrorMessage>}
        {detailQuery.data && detailQuery.data.blocks.length === 0 && <p className="border border-line bg-panel px-3 py-4 text-muted">This session has no completed blocks stored for inspection.</p>}
        {detailQuery.data?.blocks.map((block, index) => <BlockReport key={`${block.id}-${index}`} block={block} index={index} project={project} sessionId={sessionId} />)}
      </section>

      <section aria-labelledby="debug-prompt-heading" className="space-y-2 border-t border-line pt-4">
        <div>
          <h2 id="debug-prompt-heading" className="text-base font-semibold">Condenser prompt</h2>
          <p className="mt-1 text-muted">Edit the prompt stored by the backend. Saving updates the database value.</p>
        </div>
        {promptQuery.isLoading && <p role="status" className="text-muted">Loading condenser prompt…</p>}
        {promptQuery.isError && <ErrorMessage>Could not load the condenser prompt from /debug/prompt.</ErrorMessage>}
        {promptQuery.data && <>
          <p className="text-[12px] text-muted">{promptText === promptQuery.data.default_prompt ? "Using the default prompt." : "Using an edited prompt."}</p>
          <textarea aria-label="Condenser prompt" value={promptText} onChange={(event) => setPromptText(event.target.value)} rows={12} spellCheck={false} className="block min-h-56 w-full resize-y border border-line bg-panel px-3 py-2 font-mono text-[12px] leading-relaxed text-body" />
          {savePrompt.isError && <ErrorMessage>Could not save the prompt. Your edits remain in the editor; retry when the backend is available.</ErrorMessage>}
          {savePrompt.isSuccess && <p role="status" className="text-pos">Prompt saved.</p>}
          <button type="button" disabled={savePrompt.isPending || promptText === promptQuery.data.prompt} onClick={() => savePrompt.mutate()} className="min-h-10 border border-accent px-4 font-semibold text-accent hover:bg-accentbg disabled:cursor-not-allowed disabled:opacity-50">
            {savePrompt.isPending ? "Saving…" : "Save prompt"}
          </button>
        </>}
      </section>
    </div>
  );
}
