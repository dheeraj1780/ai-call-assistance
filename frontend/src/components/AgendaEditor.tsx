import { draftKey, type AgendaDraftItem } from "../lib/prep";
import { Button } from "./ui";

const MAX_ITEMS = 15;

export function AgendaEditor({
  items,
  onChange,
}: {
  items: AgendaDraftItem[];
  onChange: (items: AgendaDraftItem[]) => void;
}) {
  const update = (index: number, patch: Partial<AgendaDraftItem>) =>
    onChange(
      items.map((item, i) =>
        i === index
          ? { ...item, ...patch, source: item.source === "MANUAL" ? "MANUAL" : "AI_EDITED" }
          : item,
      ),
    );
  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    const next = [...items];
    [next[index], next[target]] = [next[target]!, next[index]!];
    onChange(next);
  };

  return (
    <div className="space-y-2">
      {items.length === 0 ? <p className="text-sm text-slate-500">No agenda items yet.</p> : null}
      <ol className="space-y-2">
        {items.map((item, index) => (
          <li key={item.key} className="rounded-md border border-slate-200 bg-white p-3">
            <div className="flex items-start gap-2">
              <span className="mt-2 w-5 text-xs text-slate-400">{index + 1}.</span>
              <div className="flex-1 space-y-2">
                <input
                  aria-label={`Agenda item ${index + 1} title`}
                  className="w-full rounded border border-slate-300 px-2 py-1 text-sm font-medium"
                  value={item.title}
                  maxLength={200}
                  placeholder="Topic"
                  onChange={(e) => update(index, { title: e.target.value })}
                />
                <input
                  aria-label={`Agenda item ${index + 1} question`}
                  className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
                  value={item.question}
                  maxLength={500}
                  placeholder="Question to ask (optional)"
                  onChange={(e) => update(index, { question: e.target.value })}
                />
                {item.source !== "MANUAL" ? (
                  <span className="text-xs text-amber-700">
                    {item.source === "AI" ? "AI suggestion" : "AI suggestion, edited"}
                  </span>
                ) : null}
              </div>
              <div className="flex flex-col gap-1 text-xs">
                <button aria-label={`Move item ${index + 1} up`} onClick={() => move(index, -1)} className="rounded border px-1">
                  ↑
                </button>
                <button aria-label={`Move item ${index + 1} down`} onClick={() => move(index, 1)} className="rounded border px-1">
                  ↓
                </button>
                <button
                  aria-label={`Remove item ${index + 1}`}
                  onClick={() => onChange(items.filter((_, i) => i !== index))}
                  className="rounded border px-1 text-rose-700"
                >
                  ✕
                </button>
              </div>
            </div>
          </li>
        ))}
      </ol>
      <Button
        variant="secondary"
        disabled={items.length >= MAX_ITEMS}
        onClick={() => onChange([...items, { key: draftKey(), title: "", question: "", source: "MANUAL" }])}
      >
        Add agenda item
      </Button>
    </div>
  );
}
