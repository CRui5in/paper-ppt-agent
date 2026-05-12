import type { ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "../ui/tooltip";

/** Hover tooltip built on Radix UI — consistent with the ConfigHelp component
 *  used in OptionsPanel. Renders a floating bubble anchored to the trigger
 *  element with proper positioning, so it never appears at the wrong place.
 */
interface HoverTooltipProps {
  /** Full text to reveal on hover. */
  content: string;
  /** Wrapper class. */
  className?: string;
  /** When true, never show the tooltip (e.g. content is empty). */
  disabled?: boolean;
  children: ReactNode;
}

export function HoverTooltip({ content, className, disabled, children }: HoverTooltipProps) {
  if (disabled || !content) {
    return <span className={className}>{children}</span>;
  }

  return (
    <TooltipProvider delayDuration={120}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={className} style={{ pointerEvents: "auto" }}>
            {children}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" align="center" className="config-tooltip-content">
          {content}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
