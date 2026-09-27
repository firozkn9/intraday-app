import { useEffect, useRef, type ButtonHTMLAttributes, type ReactNode } from "react";

type Variant = "primary" | "secondary" | "danger" | "ghost";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent text-accent-contrast hover:opacity-90",
  secondary: "bg-surface-2 text-text border border-border hover:bg-border/60",
  danger: "bg-danger text-white hover:opacity-90",
  ghost: "text-muted hover:text-text hover:bg-surface-2",
};

export function Button({
  variant = "secondary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      className={`inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${VARIANTS[variant]} ${className}`}
      {...props}
    />
  );
}

export function Card({ title, children, actions }: { title?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-surface">
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
          {title && <h2 className="text-sm font-semibold">{title}</h2>}
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

const TONES = {
  ok: "bg-ok-bg text-ok",
  warn: "bg-warn-bg text-warn",
  danger: "bg-danger-bg text-danger",
  neutral: "bg-neutral-bg text-muted",
  accent: "bg-accent/15 text-accent",
} as const;

export type Tone = keyof typeof TONES;

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${TONES[tone]}`}>
      {children}
    </span>
  );
}

export function Alert({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <div className={`rounded-md px-3 py-2 text-sm ${TONES[tone]}`}>{children}</div>;
}

export function Modal({
  title,
  open,
  onClose,
  children,
  footer,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dlg = ref.current;
    if (!dlg) return;
    if (open && !dlg.open) {
      if (typeof dlg.showModal === "function") dlg.showModal();
      else dlg.setAttribute("open", "");
    } else if (!open && dlg.open) {
      if (typeof dlg.close === "function") dlg.close();
      else dlg.removeAttribute("open");
    }
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-label={title}
      className="m-auto w-[min(42rem,calc(100vw-2rem))] rounded-lg border border-border bg-surface p-0 text-text shadow-xl backdrop:bg-black/50"
    >
      {open && (
        <>
          <header className="flex items-center justify-between border-b border-border px-4 py-3">
            <h2 className="font-semibold">{title}</h2>
            <Button variant="ghost" aria-label="Close" onClick={onClose}>
              ✕
            </Button>
          </header>
          <div className="max-h-[65vh] overflow-y-auto px-4 py-3">{children}</div>
          {footer && <footer className="flex justify-end gap-2 border-t border-border px-4 py-3">{footer}</footer>}
        </>
      )}
    </dialog>
  );
}
