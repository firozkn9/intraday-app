import { useMemo, useRef, useState } from "react";
import { API_BASE, ApiError } from "../api/client";
import {
  useImportSettings,
  useRollbackSettings,
  useSaveSettings,
  useSettings,
  useSettingsHistory,
  useValidateSettings,
} from "../api/hooks";
import type { Config, Evaluation, FieldSpec, Policy, SettingsResponse } from "../api/types";
import { Alert, Badge, Button, Card, Modal, type Tone } from "../components/ui";
import {
  boundsText,
  formatValue,
  fromInput,
  getPath,
  GROUP_LABELS,
  humanize,
  POLICY_TEXT,
  setPath,
  subgroup,
  toInput,
} from "../lib/settings";

const POLICY_TONE: Record<Policy, Tone> = {
  HOT: "neutral",
  PAUSED: "warn",
  PAUSED_ANY: "warn",
  RESTART: "accent",
  LOCKED: "neutral",
};
const POLICY_SHORT: Record<Policy, string> = {
  HOT: "live",
  PAUSED: "pause to loosen",
  PAUSED_ANY: "pause to change",
  RESTART: "restart",
  LOCKED: "locked",
};

function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.detail ? `${e.title}: ${e.detail}` : e.title;
  return "Request failed";
}

export function SettingsPage() {
  const settings = useSettings();
  if (settings.isPending) return <p className="text-muted">Loading settings…</p>;
  if (settings.error) return <Alert tone="danger">{errorMessage(settings.error)}</Alert>;
  // Keyed by version so the draft resets whenever a new version becomes active.
  return <SettingsEditor key={settings.data.version} data={settings.data} />;
}

export function SettingsEditor({ data }: { data: SettingsResponse }) {
  const [draft, setDraft] = useState<Record<string, string | boolean>>({});
  const [group, setGroup] = useState("risk");
  const [review, setReview] = useState<Evaluation | null>(null);
  const [comment, setComment] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);

  const validate = useValidateSettings();
  const save = useSaveSettings();

  const groups = useMemo(() => [...new Set(data.fields.map((f) => f.group))], [data.fields]);
  const specs = data.fields.filter((f) => f.group === group);
  const dirtyKeys = Object.keys(draft);

  const proposed = useMemo<Config>(() => {
    let cfg = data.config;
    for (const spec of data.fields) {
      if (spec.key in draft) cfg = setPath(cfg, spec.key, fromInput(spec, draft[spec.key]!));
    }
    return cfg;
  }, [data, draft]);

  const fieldErrors = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of review?.errors ?? []) m.set(e.key, e.message);
    return m;
  }, [review]);

  const onReview = () => {
    setNotice(null);
    validate.mutate(proposed, { onSuccess: setReview, onError: (e) => setNotice(errorMessage(e)) });
  };

  const onSave = () => {
    save.mutate(
      { base_version: data.version, config: proposed, comment },
      {
        onSuccess: (res) => {
          setReview(null);
          setNotice(`Saved as version ${res.version}.`);
        },
        onError: (e) => {
          if (e instanceof ApiError && e.errors.length) {
            setReview((r) => (r ? { ...r, valid: false, errors: e.errors } : r));
          }
          setNotice(errorMessage(e));
        },
      },
    );
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Settings</h1>
        <Badge tone="accent">v{data.version}</Badge>
        {data.running_version != null && data.running_version !== data.version && (
          <Badge tone="warn">engine running v{data.running_version}</Badge>
        )}
        <span className="text-xs text-muted">
          {data.created_by ?? "system"} · {new Date(data.created_at).toLocaleString()} · {data.comment}
        </span>
        <div className="ml-auto flex gap-2">
          <Button onClick={() => setShowHistory(true)}>History</Button>
          <a
            href={`${API_BASE}/settings/export`}
            className="inline-flex items-center rounded-md border border-border bg-surface-2 px-3 py-1.5 text-sm font-medium hover:bg-border/60"
          >
            Export
          </a>
          <ImportButton version={data.version} onDone={setNotice} />
        </div>
      </div>

      {!data.engine_paused && (
        <Alert tone="warn">
          Trading is enabled. Tightening applies immediately; loosening a risk limit requires pausing first.
        </Alert>
      )}
      {notice && <Alert tone={notice.startsWith("Saved") ? "ok" : "danger"}>{notice}</Alert>}

      <div className="flex flex-wrap gap-1 border-b border-border" role="tablist">
        {groups.map((g) => {
          const dirty = dirtyKeys.some((k) => k.startsWith(`${g}.`));
          return (
            <button
              key={g}
              type="button"
              role="tab"
              aria-selected={g === group}
              onClick={() => setGroup(g)}
              className={`-mb-px border-b-2 px-3 py-2 text-sm ${
                g === group ? "border-accent font-semibold text-text" : "border-transparent text-muted hover:text-text"
              }`}
            >
              {GROUP_LABELS[g] ?? humanize(g)}
              {dirty && <span className="ml-1 text-accent">•</span>}
            </button>
          );
        })}
      </div>

      <Card>
        <div className="divide-y divide-border">
          {specs.map((spec, i) => {
            const heading = subgroup(spec);
            const showHeading = heading && heading !== (i > 0 ? subgroup(specs[i - 1]!) : null);
            return (
              <div key={spec.key}>
                {showHeading && <h3 className="pt-4 pb-1 text-sm font-semibold">{heading}</h3>}
                <FieldRow
                  spec={spec}
                  value={spec.key in draft ? draft[spec.key]! : toInput(spec, getPath(data.config, spec.key))}
                  dirty={spec.key in draft}
                  error={fieldErrors.get(spec.key)}
                  onChange={(v) =>
                    setDraft((d) => {
                      const original = toInput(spec, getPath(data.config, spec.key));
                      const next = { ...d };
                      if (v === original) delete next[spec.key];
                      else next[spec.key] = v;
                      return next;
                    })
                  }
                />
              </div>
            );
          })}
        </div>
      </Card>

      <div className="sticky bottom-16 z-10 flex items-center gap-3 rounded-lg border border-border bg-surface p-3 md:bottom-2">
        <span className="text-sm text-muted">
          {dirtyKeys.length === 0 ? "No unsaved changes" : `${dirtyKeys.length} unsaved change(s)`}
        </span>
        <div className="ml-auto flex gap-2">
          <Button disabled={dirtyKeys.length === 0} onClick={() => { setDraft({}); setReview(null); }}>
            Discard
          </Button>
          <Button variant="primary" disabled={dirtyKeys.length === 0 || validate.isPending} onClick={onReview}>
            Review changes
          </Button>
        </div>
      </div>

      <Modal
        title="Review settings changes"
        open={review !== null}
        onClose={() => setReview(null)}
        footer={
          <>
            <Button onClick={() => setReview(null)}>Back</Button>
            <Button
              variant="primary"
              onClick={onSave}
              disabled={!review?.valid || review.blocked_by_running || review.changes.length === 0 || save.isPending}
            >
              {save.isPending ? "Saving…" : "Save new version"}
            </Button>
          </>
        }
      >
        {review && <ReviewBody review={review} comment={comment} setComment={setComment} />}
      </Modal>

      <HistoryModal open={showHistory} onClose={() => setShowHistory(false)} activeVersion={data.version} engineRunning={!data.engine_paused} />
    </div>
  );
}

function FieldRow({
  spec,
  value,
  dirty,
  error,
  onChange,
}: {
  spec: FieldSpec;
  value: string | boolean;
  dirty: boolean;
  error?: string;
  onChange: (v: string | boolean) => void;
}) {
  const id = `f-${spec.key}`;
  const locked = spec.policy === "LOCKED";
  const bounds = boundsText(spec);
  const inputCls = `w-full rounded-md border bg-bg px-2 py-1.5 text-sm num ${
    error ? "border-danger" : dirty ? "border-accent" : "border-border"
  } disabled:opacity-60`;

  let input;
  if (spec.type === "bool") {
    input = (
      <input
        id={id}
        type="checkbox"
        className="h-4 w-4 accent-[var(--color-accent)]"
        checked={Boolean(value)}
        disabled={locked}
        onChange={(e) => onChange(e.target.checked)}
      />
    );
  } else if (spec.type === "enum") {
    input = (
      <select id={id} className={inputCls} value={String(value)} disabled={locked} onChange={(e) => onChange(e.target.value)}>
        {(spec.options ?? []).map((o) => (
          <option key={String(o)} value={String(o)}>
            {String(o)}
          </option>
        ))}
      </select>
    );
  } else {
    input = (
      <input
        id={id}
        type={spec.type === "time" ? "time" : "text"}
        inputMode={["int", "float", "decimal"].includes(spec.type) ? "decimal" : undefined}
        className={inputCls}
        value={String(value)}
        disabled={locked}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <div className="grid gap-2 py-3 md:grid-cols-[minmax(0,1fr)_14rem] md:items-center">
      <div className="min-w-0">
        <label htmlFor={id} className="flex flex-wrap items-center gap-2 text-sm font-medium">
          {humanize(spec.key)}
          <Badge tone={POLICY_TONE[spec.policy]} title={POLICY_TEXT[spec.policy]}>
            {locked ? "🔒 " : ""}
            {POLICY_SHORT[spec.policy]}
          </Badge>
        </label>
        <p className="text-xs text-muted">
          {spec.description}
          {bounds && <span className="num"> Range {bounds}{spec.unit ? ` ${spec.unit}` : ""}.</span>}
          {!bounds && spec.unit && <span> ({spec.unit})</span>}
        </p>
        {error && <p className="text-xs text-danger">{error}</p>}
      </div>
      <div>{input}</div>
    </div>
  );
}

function ReviewBody({
  review,
  comment,
  setComment,
}: {
  review: Evaluation;
  comment: string;
  setComment: (s: string) => void;
}) {
  return (
    <div className="space-y-3 text-sm">
      {!review.valid && (
        <Alert tone="danger">
          <strong>Not valid:</strong>
          <ul className="mt-1 list-disc pl-5">
            {review.errors.map((e) => (
              <li key={e.key + e.message}>
                <code>{e.key}</code>: {e.message}
              </li>
            ))}
          </ul>
        </Alert>
      )}
      {review.blocked_by_running && (
        <Alert tone="warn">
          Some changes loosen risk or require a pause. Pause trading from the status bar, then save.
        </Alert>
      )}
      {review.pending_restart.length > 0 && (
        <Alert tone="accent">Takes effect after an engine restart: {review.pending_restart.join(", ")}</Alert>
      )}
      {review.changes.length > 0 && (
        <table className="w-full text-left">
          <thead className="text-xs text-muted">
            <tr>
              <th className="py-1 font-medium">Setting</th>
              <th className="py-1 font-medium">From</th>
              <th className="py-1 font-medium">To</th>
              <th className="py-1 font-medium" />
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {review.changes.map((c) => (
              <tr key={c.key}>
                <td className="py-1.5 pr-2">{c.key}</td>
                <td className="py-1.5 pr-2 num text-muted">{formatValue(c.old)}</td>
                <td className="py-1.5 pr-2 num">{formatValue(c.new)}</td>
                <td className="py-1.5">
                  {c.loosening ? <Badge tone="warn">loosens</Badge> : null}{" "}
                  {c.requires_pause ? <Badge tone="danger">needs pause</Badge> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <label className="block">
        <span className="mb-1 block font-medium">Comment (saved in the audit log)</span>
        <input
          className="w-full rounded-md border border-border bg-bg px-2 py-1.5"
          maxLength={500}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="Why this change?"
        />
      </label>
    </div>
  );
}

function HistoryModal({
  open,
  onClose,
  activeVersion,
  engineRunning,
}: {
  open: boolean;
  onClose: () => void;
  activeVersion: number;
  engineRunning: boolean;
}) {
  const history = useSettingsHistory(open);
  const rollback = useRollbackSettings();
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal title="Settings history" open={open} onClose={onClose}>
      {error && <Alert tone="danger">{error}</Alert>}
      {history.isPending && <p className="text-sm text-muted">Loading…</p>}
      <ol className="space-y-3 text-sm">
        {history.data?.map((h) => (
          <li key={h.version} className="rounded-md border border-border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <strong>v{h.version}</strong>
              {h.is_active && <Badge tone="ok">active</Badge>}
              <span className="text-xs text-muted">
                {h.created_by ?? "system"} · {new Date(h.created_at).toLocaleString()}
              </span>
              {!h.is_active && (
                <Button
                  className="ml-auto"
                  disabled={rollback.isPending}
                  title={engineRunning ? "Rolling back a loosening change requires pausing" : undefined}
                  onClick={() => {
                    setError(null);
                    rollback.mutate(
                      { version: h.version, base_version: activeVersion },
                      { onSuccess: onClose, onError: (e) => setError(errorMessage(e)) },
                    );
                  }}
                >
                  Restore
                </Button>
              )}
            </div>
            {h.comment && <p className="mt-1">{h.comment}</p>}
            {h.changes.length > 0 && (
              <ul className="mt-1 text-xs text-muted">
                {h.changes.map((c) => (
                  <li key={c.key} className="num">
                    {c.key}: {formatValue(c.old)} → {formatValue(c.new)}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ol>
    </Modal>
  );
}

function ImportButton({ version, onDone }: { version: number; onDone: (msg: string) => void }) {
  const ref = useRef<HTMLInputElement>(null);
  const importer = useImportSettings();
  return (
    <>
      <Button onClick={() => ref.current?.click()} disabled={importer.isPending}>
        Import
      </Button>
      <input
        ref={ref}
        type="file"
        accept=".yaml,.yml,application/yaml,text/yaml"
        className="hidden"
        aria-label="Import settings YAML"
        onChange={async (e) => {
          const file = e.target.files?.[0];
          e.target.value = "";
          if (!file) return;
          const yaml = await file.text();
          importer.mutate(
            { yaml, base_version: version, comment: `Imported ${file.name}` },
            {
              onSuccess: (r) => onDone(`Saved as version ${r.version}.`),
              onError: (err) => onDone(errorMessage(err)),
            },
          );
        }}
      />
    </>
  );
}
