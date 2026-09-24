import type { ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";
import { useId } from "react";

import { label } from "../lib/crm";
import { errorMessage } from "../lib/errors";
import { Alert } from "./ui";

const BADGE_COLORS: Record<string, string> = {
  NEW: "bg-slate-100 text-slate-700",
  CONTACTED: "bg-sky-100 text-sky-800",
  QUALIFIED: "bg-indigo-100 text-indigo-800",
  PROPOSAL: "bg-violet-100 text-violet-800",
  NEGOTIATION: "bg-amber-100 text-amber-800",
  WON: "bg-emerald-100 text-emerald-800",
  LOST: "bg-rose-100 text-rose-800",
  PLANNED: "bg-slate-100 text-slate-700",
  ACTIVE: "bg-emerald-100 text-emerald-800",
  COMPLETED: "bg-emerald-100 text-emerald-800",
  DONE: "bg-emerald-100 text-emerald-800",
  OPEN: "bg-sky-100 text-sky-800",
  IN_PROGRESS: "bg-amber-100 text-amber-800",
  CANCELLED: "bg-slate-100 text-slate-500",
  FAILED: "bg-rose-100 text-rose-800",
  NO_ANSWER: "bg-rose-100 text-rose-800",
};

export function Badge({ value }: { value: string }) {
  return (
    <span
      className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${
        BADGE_COLORS[value] ?? "bg-slate-100 text-slate-700"
      }`}
    >
      {label(value)}
    </span>
  );
}

export function PageHeader({ title, actions }: { title: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
      <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </div>
  );
}

export function Card({ title, children, actions }: { title?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 sm:p-5">
      {title || actions ? (
        <div className="mb-3 flex items-center justify-between gap-2">
          {title ? <h2 className="text-sm font-semibold text-slate-900">{title}</h2> : <span />}
          {actions}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function QueryState({
  isPending,
  error,
  empty,
  emptyText = "Nothing here yet.",
  children,
}: {
  isPending: boolean;
  error: unknown;
  empty?: boolean;
  emptyText?: string;
  children: ReactNode;
}) {
  if (isPending) return <p className="text-sm text-slate-500">Loading…</p>;
  if (error) return <Alert>{errorMessage(error)}</Alert>;
  if (empty) return <p className="text-sm text-slate-500">{emptyText}</p>;
  return <>{children}</>;
}

interface SelectFieldProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label: string;
  options: readonly { value: string; label: string }[];
}

export function SelectField({ label: text, options, ...props }: SelectFieldProps) {
  const id = useId();
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {text}
      </label>
      <select
        id={id}
        className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm"
        {...props}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

interface TextAreaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label: string;
  error?: string;
}

export function TextArea({ label: text, error, ...props }: TextAreaProps) {
  const id = useId();
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {text}
      </label>
      <textarea
        id={id}
        rows={3}
        aria-invalid={error ? true : undefined}
        className={`block w-full rounded-md border bg-white px-3 py-2 text-sm ${
          error ? "border-red-500" : "border-slate-300"
        }`}
        {...props}
      />
      {error ? <p className="text-xs text-red-600">{error}</p> : null}
    </div>
  );
}

export function Pagination({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
}) {
  if (total <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.ceil(total / limit);
  return (
    <div className="mt-4 flex items-center justify-between text-sm text-slate-600">
      <span>
        Page {page} of {pages} · {total} total
      </span>
      <div className="flex gap-2">
        <button
          className="rounded border border-slate-300 px-3 py-1 disabled:opacity-40"
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          Previous
        </button>
        <button
          className="rounded border border-slate-300 px-3 py-1 disabled:opacity-40"
          disabled={offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        >
          Next
        </button>
      </div>
    </div>
  );
}
