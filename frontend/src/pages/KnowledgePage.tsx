import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { useAuth } from "../auth/context";
import { Badge, Card, PageHeader, QueryState } from "../components/common";
import { Alert, Button, TextField } from "../components/ui";
import { apiFetch } from "../lib/api";
import { formatDateTime } from "../lib/crm";
import { errorMessage } from "../lib/errors";

export interface KnowledgeDocument {
  id: string;
  title: string;
  filename: string;
  size_bytes: number;
  version: number;
  status: "UPLOADED" | "PROCESSING" | "READY" | "FAILED";
  error_code: string | null;
  chunk_count: number;
  mime_type?: string;
  embedding_model?: string | null;
  /** Embedded with a previous embedding model: not searched until re-processed. */
  needs_reprocess?: boolean;
  created_at: string;
}

interface Answer {
  answer: string;
  supported: boolean;
  label: string;
  sources: { document_id: string; title: string; snippet: string; score: number }[];
}

const knowledgeApi = {
  list: () => apiFetch<KnowledgeDocument[]>("/api/v1/knowledge/documents"),
  upload: (file: File, title: string) => {
    const form = new FormData();
    form.append("file", file);
    if (title.trim()) form.append("title", title.trim());
    return apiFetch<KnowledgeDocument>("/api/v1/knowledge/documents", { method: "POST", body: form });
  },
  remove: (id: string) => apiFetch<void>(`/api/v1/knowledge/documents/${id}`, { method: "DELETE" }),
  reprocess: (id: string) =>
    apiFetch<KnowledgeDocument>(`/api/v1/knowledge/documents/${id}/reprocess`, { method: "POST" }),
  ask: (question: string) => apiFetch<Answer>("/api/v1/knowledge/query", { method: "POST", body: { question } }),
};

const MAX_MB = 10;

export function KnowledgePage() {
  const { session } = useAuth();
  const isAdmin = session?.role === "OWNER" || session?.role === "ADMIN";
  const queryClient = useQueryClient();
  const docs = useQuery({
    queryKey: ["knowledge"],
    queryFn: knowledgeApi.list,
    refetchInterval: (q) =>
      q.state.data?.some((d) => d.status === "PROCESSING" || d.status === "UPLOADED") ? 3000 : false,
  });
  const remove = useMutation({
    mutationFn: knowledgeApi.remove,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["knowledge"] }),
  });
  const reprocess = useMutation({
    mutationFn: knowledgeApi.reprocess,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["knowledge"] }),
  });

  return (
    <div className="space-y-4">
      <PageHeader title="Knowledge base" />
      <p className="text-sm text-slate-600">
        Upload product guides, price lists or FAQs (PDF, DOCX, TXT, Markdown). During calls and in customer
        conversations the copilot uses matching excerpts, clearly labelled as company knowledge. Original files are not stored — only the extracted text.
      </p>
      {isAdmin ? <UploadCard /> : null}
      <AskCard />
      <Card title="Documents">
        <QueryState isPending={docs.isPending} error={docs.error} empty={docs.data?.length === 0} emptyText="No documents yet.">
          <ul className="divide-y divide-slate-100">
            {docs.data?.map((d) => (
              <li key={d.id} className="flex flex-col gap-1 py-2 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-900">
                    {d.title} <span className="text-xs text-slate-400">v{d.version}</span>
                  </p>
                  <p className="text-xs text-slate-500">
                    {d.filename} · {(d.size_bytes / 1024).toFixed(0)} KB · {d.chunk_count} sections ·{" "}
                    {formatDateTime(d.created_at)}
                    {d.error_code ? ` · ${d.error_code}` : ""}
                    {d.embedding_model ? ` · ${d.embedding_model}` : ""}
                  </p>
                  {d.needs_reprocess ? (
                    <p className="text-xs text-amber-700">
                      Processed with a previous embedding model — not searched until re-processed.
                    </p>
                  ) : null}
                </div>
                <div className="flex items-center gap-2">
                  <Badge value={d.status === "READY" ? "COMPLETED" : d.status === "FAILED" ? "FAILED" : "IN_PROGRESS"} />
                  {isAdmin && (d.status === "FAILED" || d.needs_reprocess) ? (
                    <button
                      className="text-xs text-sky-700 underline"
                      disabled={reprocess.isPending}
                      onClick={() => reprocess.mutate(d.id)}
                    >
                      Re-process
                    </button>
                  ) : null}
                  {isAdmin ? (
                    <button className="text-xs text-rose-700 underline" onClick={() => remove.mutate(d.id)}>
                      Delete
                    </button>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </QueryState>
      </Card>
    </div>
  );
}

function UploadCard() {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const upload = useMutation({
    mutationFn: () => knowledgeApi.upload(file!, title),
    onSuccess: () => {
      setFile(null);
      setTitle("");
      queryClient.invalidateQueries({ queryKey: ["knowledge"] });
    },
  });
  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!file) return setLocalError("Choose a file first.");
    if (!/\.(pdf|docx|txt|md|markdown)$/i.test(file.name))
      return setLocalError("Only PDF, DOCX, TXT and Markdown files are supported.");
    if (file.size > MAX_MB * 1024 * 1024) return setLocalError(`Files are limited to ${MAX_MB} MB.`);
    setLocalError(null);
    upload.mutate();
  }
  return (
    <Card title="Upload a document">
      <form onSubmit={onSubmit} className="grid gap-3 sm:grid-cols-3">
        <div className="space-y-1">
          <label htmlFor="kb-file" className="block text-sm font-medium text-slate-700">
            File
          </label>
          <input
            id="kb-file"
            type="file"
            accept=".pdf,.docx,.txt,.md,.markdown"
            className="block w-full text-sm"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>
        <TextField label="Title (optional)" value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
        <div className="flex items-end">
          <Button type="submit" disabled={upload.isPending}>
            {upload.isPending ? "Uploading…" : "Upload"}
          </Button>
        </div>
        {localError || upload.error ? (
          <div className="sm:col-span-3">
            <Alert>{localError ?? errorMessage(upload.error)}</Alert>
          </div>
        ) : null}
      </form>
    </Card>
  );
}

function AskCard() {
  const [question, setQuestion] = useState("");
  const ask = useMutation({ mutationFn: () => knowledgeApi.ask(question.trim()) });
  return (
    <Card title="Ask your knowledge base">
      <form
        className="flex flex-col gap-2 sm:flex-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (question.trim().length >= 3) ask.mutate();
        }}
      >
        <input
          aria-label="Question"
          className="flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm"
          placeholder="e.g. Do we support multiple branches?"
          value={question}
          maxLength={500}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <Button type="submit" variant="secondary" disabled={ask.isPending}>
          Ask
        </Button>
      </form>
      {ask.error ? <Alert>{errorMessage(ask.error)}</Alert> : null}
      {ask.data ? (
        <div className="mt-3 space-y-2" aria-label="Answer">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{ask.data.label}</p>
          <p className="text-sm text-slate-900">{ask.data.answer}</p>
          {ask.data.sources.map((s) => (
            <p key={s.document_id} className="text-xs text-slate-500">
              Source: {s.title}
            </p>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
