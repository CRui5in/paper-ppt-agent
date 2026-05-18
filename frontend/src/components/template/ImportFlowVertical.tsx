import type { ReactNode } from "react";
import { Check, Loader2, X as XIcon } from "lucide-react";

export type FlowStepState = "done" | "active" | "pending" | "error";

export interface FlowStep {
  id: string;
  label: string;
  state: FlowStepState;
  detail?: string;
}

export interface ImportFlowVerticalProps {
  steps: FlowStep[];
  /** Optional title shown above the list. */
  title?: ReactNode;
  /** Optional subtitle / hint below the title. */
  subtitle?: ReactNode;
  className?: string;
}

/**
 * Vertical 4-step flow checklist for the templates page right column.
 *
 * Each step renders a left-edge dot (with state-specific color/animation),
 * a label, and an optional sub-state line. Dots are connected by a thin
 * vertical line that fills in from above as steps complete — no progress
 * percentages or numbers, just colour + state.
 */
export function ImportFlowVertical({
  steps,
  title,
  subtitle,
  className,
}: ImportFlowVerticalProps) {
  return (
    <section className={`ti-flow-section ${className ?? ""}`}>
      {title ? <header className="ti-flow-title">{title}</header> : null}
      {subtitle ? <p className="ti-flow-subtitle">{subtitle}</p> : null}
      <ol className="ti-flow-list">
        {steps.map((step, i) => {
          const previousState = steps[i - 1]?.state;
          const lineState: FlowStepState =
            previousState === "done" || step.state === "done" || step.state === "active"
              ? "done"
              : "pending";
          const isLast = i === steps.length - 1;
          return (
            <li key={step.id} className="ti-flow-item" data-state={step.state}>
              <div className="ti-flow-rail">
                {i === 0 ? null : (
                  <span
                    className="ti-flow-line"
                    data-state={lineState}
                    aria-hidden="true"
                  />
                )}
                <span
                  className="ti-flow-dot"
                  data-state={step.state}
                  aria-hidden="true"
                >
                  {step.state === "done" ? (
                    <Check size={10} strokeWidth={3} />
                  ) : step.state === "error" ? (
                    <XIcon size={10} strokeWidth={3} />
                  ) : null}
                </span>
                {!isLast ? (
                  <span
                    className="ti-flow-line ti-flow-line-after"
                    data-state={
                      step.state === "done" ? "done" : "pending"
                    }
                    aria-hidden="true"
                  />
                ) : null}
              </div>
              <div className="ti-flow-body">
                <span className="ti-flow-label">{step.label}</span>
                {step.detail ? (
                  <span className="ti-flow-detail">{step.detail}</span>
                ) : null}
              </div>
              {step.state === "active" ? (
                <Loader2 size={12} className="ti-flow-spinner animate-spin" />
              ) : null}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
