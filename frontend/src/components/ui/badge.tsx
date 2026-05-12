import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";

const badgeVariants = cva("inline-flex items-center rounded-md px-2 py-0.5 text-xs font-semibold", {
  variants: {
    variant: {
      default: "bg-primary/10 text-primary",
      success: "border border-emerald-200 bg-emerald-50 text-emerald-700",
      warning: "border border-amber-200 bg-amber-50 text-amber-700",
      muted: "bg-muted text-muted-foreground",
      destructive: "border border-red-200 bg-red-50 text-red-700",
    },
  },
  defaultVariants: { variant: "default" },
});

export function Badge({ className, variant, ...props }: React.HTMLAttributes<HTMLDivElement> & VariantProps<typeof badgeVariants>) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}
