/**
 * UI primitives — shadcn/ui-style components, vendored.
 *
 * Written by hand rather than pulled in wholesale: the brief asks for shadcn/ui, whose model is
 * that you *own* the component source. Vendoring the handful this app actually uses (rather than
 * running the generator and inheriting forty files) keeps the dependency surface to
 * `class-variance-authority` + `tailwind-merge` and means every component here is one we
 * maintain and have a use for.
 */

import { cva, type VariantProps } from 'class-variance-authority'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Info,
  Loader2,
  X,
  type LucideIcon,
} from 'lucide-react'
import {
  createContext,
  forwardRef,
  useContext,
  useEffect,
  useId,
  useRef,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'

import { cn } from '@/lib/utils'

// ---- Button -----------------------------------------------------------------
const buttonVariants = cva(
  'inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded font-medium ' +
    'transition-colors duration-100 ease-snap disabled:pointer-events-none disabled:opacity-45 ' +
    '[&_svg]:shrink-0',
  {
    variants: {
      variant: {
        primary: 'bg-brand text-brand-ink hover:bg-brand/88 active:bg-brand/95',
        secondary:
          'border border-line-strong bg-surface text-ink hover:bg-surface-raised hover:border-ink-subtle',
        ghost: 'text-ink-muted hover:bg-surface-raised hover:text-ink',
        danger: 'bg-bad text-white hover:bg-bad/88',
        subtle: 'bg-surface-raised text-ink hover:bg-line',
      },
      size: {
        sm: 'h-7 px-2.5 text-xs [&_svg]:size-3.5',
        md: 'h-9 px-3.5 text-base [&_svg]:size-4',
        lg: 'h-11 px-5 text-lg [&_svg]:size-[18px]',
        icon: 'h-9 w-9 [&_svg]:size-4',
        'icon-sm': 'h-7 w-7 [&_svg]:size-3.5',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
)

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  loading?: boolean
  icon?: LucideIcon
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, loading, icon: Icon, children, disabled, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || loading}
      {...props}
    >
      {loading ? <Loader2 className="animate-spin" aria-hidden /> : Icon ? <Icon aria-hidden /> : null}
      {children}
    </button>
  )
})

// ---- Card / Panel -----------------------------------------------------------
export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('panel shadow-panel', className)} {...props} />
}

export function CardHeader({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex items-start justify-between gap-3 border-b border-line px-4 py-3',
        className,
      )}
    >
      <div className="min-w-0">
        <h2 className="truncate text-lg font-semibold text-ink">{title}</h2>
        {description ? (
          <p className="mt-0.5 text-xs leading-relaxed text-ink-muted">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  )
}

export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('p-4', className)} {...props} />
}

// ---- Badge ------------------------------------------------------------------
const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-2xs font-semibold ' +
    'uppercase tracking-wide [&_svg]:size-3',
  {
    variants: {
      tone: {
        neutral: 'bg-surface-raised text-ink-muted ring-1 ring-inset ring-line',
        brand: 'bg-brand-soft text-brand ring-1 ring-inset ring-brand/30',
        good: 'bg-good/12 text-good ring-1 ring-inset ring-good/30',
        warn: 'bg-warn/14 text-warn ring-1 ring-inset ring-warn/30',
        bad: 'bg-bad/14 text-bad ring-1 ring-inset ring-bad/30',
        info: 'bg-info/12 text-info ring-1 ring-inset ring-info/30',
        estimated: 'bg-estimated/14 text-estimated ring-1 ring-inset ring-estimated/30',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
)

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {
  icon?: LucideIcon
}

export function Badge({ className, tone, icon: Icon, children, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ tone }), className)} {...props}>
      {Icon ? <Icon aria-hidden /> : null}
      {children}
    </span>
  )
}

// ---- Form fields ------------------------------------------------------------
const FieldContext = createContext<{ id: string; errorId: string } | null>(null)

export function Field({
  label,
  error,
  hint,
  required,
  children,
  className,
}: {
  label: ReactNode
  error?: string | null
  hint?: ReactNode
  required?: boolean
  children: ReactNode
  className?: string
}) {
  const id = useId()
  const errorId = `${id}-error`
  return (
    <FieldContext.Provider value={{ id, errorId }}>
      <div className={cn('space-y-1', className)}>
        <label htmlFor={id} className="block text-xs font-medium text-ink-muted">
          {label}
          {required ? <span className="ml-0.5 text-bad">*</span> : null}
        </label>
        {children}
        {error ? (
          <p id={errorId} role="alert" className="flex items-center gap-1 text-xs text-bad">
            <AlertTriangle className="size-3 shrink-0" aria-hidden />
            {error}
          </p>
        ) : hint ? (
          <p className="text-xs text-ink-subtle">{hint}</p>
        ) : null}
      </div>
    </FieldContext.Provider>
  )
}

const controlClass =
  'w-full rounded border bg-surface px-2.5 py-1.5 text-base text-ink transition-colors ' +
  'placeholder:text-ink-subtle focus:border-brand disabled:cursor-not-allowed disabled:opacity-60'

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean }>(
  function Input({ className, invalid, ...props }, ref) {
    const field = useContext(FieldContext)
    return (
      <input
        ref={ref}
        id={props.id ?? field?.id}
        aria-invalid={invalid || undefined}
        aria-describedby={invalid && field ? field.errorId : undefined}
        className={cn(controlClass, invalid ? 'border-bad' : 'border-line-strong', className)}
        {...props}
      />
    )
  },
)

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean }
>(function Textarea({ className, invalid, ...props }, ref) {
  const field = useContext(FieldContext)
  return (
    <textarea
      ref={ref}
      id={props.id ?? field?.id}
      aria-invalid={invalid || undefined}
      className={cn(
        controlClass,
        'min-h-[72px] resize-y',
        invalid ? 'border-bad' : 'border-line-strong',
        className,
      )}
      {...props}
    />
  )
})

export const Select = forwardRef<
  HTMLSelectElement,
  SelectHTMLAttributes<HTMLSelectElement> & { invalid?: boolean }
>(function Select({ className, invalid, children, ...props }, ref) {
  const field = useContext(FieldContext)
  return (
    <div className="relative">
      <select
        ref={ref}
        id={props.id ?? field?.id}
        aria-invalid={invalid || undefined}
        className={cn(
          controlClass,
          'appearance-none pr-8',
          invalid ? 'border-bad' : 'border-line-strong',
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute right-2 top-1/2 size-4 -translate-y-1/2 text-ink-subtle"
        aria-hidden
      />
    </div>
  )
})

// ---- Progress ---------------------------------------------------------------
export function Progress({
  value,
  tone = 'brand',
  className,
  showLabel,
  estimated,
}: {
  /** 0–1. */
  value: number
  tone?: 'brand' | 'good' | 'warn' | 'bad'
  className?: string
  showLabel?: boolean
  /** Hatches the bar to mark a figure that is estimated rather than measured (§46). */
  estimated?: boolean
}) {
  const pct = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0)) * 100
  const toneClass = {
    brand: 'bg-brand',
    good: 'bg-good',
    warn: 'bg-warn',
    bad: 'bg-bad',
  }[tone]

  return (
    <div className={cn('flex items-center gap-2', className)}>
      <div
        className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-raised ring-1 ring-inset ring-line"
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={cn('h-full rounded-full transition-[width] duration-300 ease-snap', toneClass, estimated && 'estimated-hatch')}
          style={{ width: `${pct}%` }}
        />
      </div>
      {showLabel ? (
        <span className="tnum w-9 shrink-0 text-right text-xs text-ink-muted">
          {Math.round(pct)}%
        </span>
      ) : null}
    </div>
  )
}

// ---- Empty / error / loading states ----------------------------------------
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon?: LucideIcon
  title: string
  description?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center gap-2 px-6 py-12 text-center', className)}>
      {Icon ? (
        <div className="mb-1 flex size-10 items-center justify-center rounded-full bg-surface-raised text-ink-subtle">
          <Icon className="size-5" aria-hidden />
        </div>
      ) : null}
      <p className="text-lg font-medium text-ink">{title}</p>
      {description ? (
        <p className="max-w-md text-sm leading-relaxed text-ink-muted">{description}</p>
      ) : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  )
}

export function ErrorState({
  title = 'Something went wrong',
  message,
  onRetry,
}: {
  title?: string
  message?: string
  onRetry?: () => void
}) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-10 text-center">
      <AlertTriangle className="size-5 text-bad" aria-hidden />
      <p className="text-lg font-medium text-ink">{title}</p>
      {message ? <p className="max-w-md text-sm text-ink-muted">{message}</p> : null}
      {onRetry ? (
        <Button size="sm" onClick={onRetry} className="mt-2">
          Try again
        </Button>
      ) : null}
    </div>
  )
}

export function Spinner({ className }: { className?: string }) {
  return (
    <Loader2
      className={cn('size-4 animate-spin text-ink-subtle', className)}
      aria-label="Loading"
    />
  )
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('skeleton h-4 w-full', className)} aria-hidden />
}

// ---- Table ------------------------------------------------------------------
export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  // Wide tables scroll inside their own container so the page body never scrolls sideways.
  return (
    <div className="w-full overflow-x-auto">
      <table className={cn('w-full border-collapse text-base', className)} {...props} />
    </div>
  )
}

export function Th({ className, ...props }: HTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={cn(
        'border-b border-line px-3 py-2 text-left text-2xs font-semibold uppercase tracking-wider text-ink-subtle',
        className,
      )}
      {...props}
    />
  )
}

export function Td({ className, ...props }: HTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn('border-b border-line/60 px-3 py-2 align-middle', className)} {...props} />
}

export function Tr({ className, ...props }: HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={cn('transition-colors hover:bg-surface-raised/70', className)} {...props} />
}

// ---- Dialog -----------------------------------------------------------------
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  wide,
}: {
  open: boolean
  onClose: () => void
  title: ReactNode
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
  wide?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    // Focus the panel so the dialog is reachable by keyboard immediately.
    ref.current?.focus()
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previousOverflow
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/55 p-4 pt-[8vh] animate-fade-in">
      <button
        type="button"
        aria-label="Close dialog"
        className="absolute inset-0 cursor-default"
        onClick={onClose}
        tabIndex={-1}
      />
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === 'string' ? title : undefined}
        tabIndex={-1}
        className={cn(
          'relative w-full rounded-lg border border-line bg-surface shadow-pop animate-slide-up',
          wide ? 'max-w-3xl' : 'max-w-lg',
        )}
      >
        <div className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
          <div>
            <h2 className="text-lg font-semibold text-ink">{title}</h2>
            {description ? <p className="mt-0.5 text-xs text-ink-muted">{description}</p> : null}
          </div>
          <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close">
            <X aria-hidden />
          </Button>
        </div>
        <div className="max-h-[65vh] overflow-y-auto p-4">{children}</div>
        {footer ? (
          <div className="flex items-center justify-end gap-2 border-t border-line px-4 py-3">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  )
}

// ---- Tabs -------------------------------------------------------------------
export function Tabs<T extends string>({
  value,
  onChange,
  options,
  className,
}: {
  value: T
  onChange: (value: T) => void
  options: Array<{ value: T; label: ReactNode; count?: number }>
  className?: string
}) {
  return (
    <div className={cn('flex items-center gap-0.5 rounded bg-surface-raised p-0.5', className)} role="tablist">
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(option.value)}
            className={cn(
              'flex items-center gap-1.5 rounded-sm px-2.5 py-1 text-sm font-medium transition-colors',
              active ? 'bg-surface text-ink shadow-sm' : 'text-ink-muted hover:text-ink',
            )}
          >
            {option.label}
            {option.count !== undefined ? (
              <span className="tnum text-2xs text-ink-subtle">{option.count}</span>
            ) : null}
          </button>
        )
      })}
    </div>
  )
}

// ---- Callout ----------------------------------------------------------------
/**
 * Used wherever the platform must state a limitation rather than imply confidence (§46) — low
 * mapping confidence, unavailable analysis, manual-only coverage.
 */
export function Callout({
  tone = 'info',
  title,
  children,
  className,
}: {
  tone?: 'info' | 'warn' | 'bad' | 'estimated'
  title?: ReactNode
  children?: ReactNode
  className?: string
}) {
  const config = {
    info: { icon: Info, cls: 'border-info/35 bg-info/8 text-info' },
    warn: { icon: AlertTriangle, cls: 'border-warn/35 bg-warn/8 text-warn' },
    bad: { icon: AlertTriangle, cls: 'border-bad/35 bg-bad/8 text-bad' },
    estimated: { icon: Info, cls: 'border-estimated/35 bg-estimated/8 text-estimated' },
  }[tone]
  const Icon = config.icon

  return (
    <div className={cn('flex gap-2 rounded border px-3 py-2', config.cls, className)}>
      <Icon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      <div className="min-w-0 text-xs leading-relaxed">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children ? <div className={cn(title && 'mt-0.5', 'text-ink-muted')}>{children}</div> : null}
      </div>
    </div>
  )
}

export { Check, buttonVariants }
