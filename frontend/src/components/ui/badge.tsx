import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva(
  'inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-semibold shadow-sm',
  {
    variants: {
      variant: {
        default: 'border-[var(--accent-edge)] bg-[var(--accent-soft)] text-[var(--text-accent)]',
        success: 'border-emerald-500/25 bg-emerald-500/12 text-[var(--tone-success)]',
        warning: 'border-amber-500/30 bg-amber-500/12 text-[var(--tone-warning)]',
        danger: 'border-red-500/25 bg-red-500/12 text-[var(--tone-danger)]',
        secondary: 'border-[var(--border)] bg-[var(--chip-bg)] text-[var(--text-muted)]',
      },
    },
    defaultVariants: { variant: 'default' },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }
