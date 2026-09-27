import { useState } from "react";
import { ApiError } from "../api/client";
import { useCloseAll, useEmergencyStop, useResetLatch } from "../api/hooks";
import { Alert, Button, Modal } from "./ui";

/** EMERGENCY STOP (latches) and CLOSE ALL (no latch), each behind a typed confirmation. */
export function EmergencyControls() {
  const [which, setWhich] = useState<"emergency" | "closeall" | null>(null);
  const [typed, setTyped] = useState("");
  const [closePositions, setClosePositions] = useState(true);
  const [error, setError] = useState<string>();
  const emergency = useEmergencyStop();
  const closeAll = useCloseAll();
  const phrase = which === "emergency" ? "EMERGENCY STOP" : "CLOSE ALL";
  const reset = () => {
    setWhich(null);
    setTyped("");
    setError(undefined);
  };
  const onError = (e: unknown) => setError(e instanceof ApiError ? (e.detail ?? e.title) : "Request failed");
  return (
    <>
      <Button variant="secondary" onClick={() => setWhich("closeall")}>CLOSE ALL</Button>
      <Button variant="danger" onClick={() => setWhich("emergency")}>EMERGENCY STOP</Button>
      <Modal
        title={which === "emergency" ? "Emergency stop" : "Close all positions"}
        open={which !== null}
        onClose={reset}
        footer={
          <>
            <Button onClick={reset}>Cancel</Button>
            <Button
              variant="danger"
              disabled={typed !== phrase || emergency.isPending || closeAll.isPending}
              onClick={() =>
                which === "emergency"
                  ? emergency.mutate({ closePositions }, { onSuccess: reset, onError })
                  : closeAll.mutate(undefined, { onSuccess: reset, onError })
              }
            >
              {phrase}
            </Button>
          </>
        }
      >
        {which === "emergency" ? (
          <div className="space-y-2 text-sm">
            <p>
              Blocks all new entries (latched: a reset with your password is needed afterwards) and cancels
              pending entries.
            </p>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={closePositions} onChange={(e) => setClosePositions(e.target.checked)} />
              Also close every open position at market (otherwise protective stops stay in place)
            </label>
          </div>
        ) : (
          <p className="text-sm">
            Cancels all open orders and sells every position at market. Trading is paused until you press START.
          </p>
        )}
        <label className="mt-3 block text-sm">
          Type <strong>{phrase}</strong> to confirm
          <input className="mt-1 w-full rounded-md border border-border bg-surface-2 px-2 py-1" value={typed}
            onChange={(e) => setTyped(e.target.value)} aria-label="Confirmation phrase" />
        </label>
        {error && <Alert tone="danger">{error}</Alert>}
      </Modal>
    </>
  );
}

/** Reset a latch (daily loss, emergency stop, reconcile mismatch, risk error). Needs the
 * password; trading stays paused until START. */
export function LatchReset({ latched }: { latched: string[] }) {
  const [open, setOpen] = useState(false);
  const [latch, setLatch] = useState(latched[0] ?? "");
  const [typed, setTyped] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string>();
  const reset = useResetLatch();
  if (latched.length === 0) return null;
  const close = () => {
    setOpen(false);
    setTyped("");
    setPassword("");
    setError(undefined);
  };
  return (
    <>
      <Button onClick={() => { setLatch(latched[0] ?? ""); setOpen(true); }}>Reset…</Button>
      <Modal title="Reset latch" open={open} onClose={close} footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button variant="primary" disabled={typed !== "RESET" || !password || reset.isPending}
            onClick={() => reset.mutate({ latch, password }, {
              onSuccess: close,
              onError: (e) => setError(e instanceof ApiError ? (e.detail ?? e.title) : "Request failed"),
            })}>
            Reset
          </Button>
        </>}>
        <div className="space-y-3 text-sm">
          <label className="block">Latch
            <select className="mt-1 w-full rounded-md border border-border bg-surface-2 px-2 py-1" value={latch}
              onChange={(e) => setLatch(e.target.value)} aria-label="Latch">
              {latched.map((l) => <option key={l}>{l}</option>)}
            </select>
          </label>
          <p>Trading stays paused until you press START.</p>
          <label className="block">Type <strong>RESET</strong> to confirm
            <input className="mt-1 w-full rounded-md border border-border bg-surface-2 px-2 py-1" value={typed}
              onChange={(e) => setTyped(e.target.value)} aria-label="Reset confirmation" />
          </label>
          <label className="block">Password
            <input type="password" className="mt-1 w-full rounded-md border border-border bg-surface-2 px-2 py-1"
              value={password} onChange={(e) => setPassword(e.target.value)} aria-label="Password" />
          </label>
          {error && <Alert tone="danger">{error}</Alert>}
        </div>
      </Modal>
    </>
  );
}
