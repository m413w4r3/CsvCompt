import type { ButtonHTMLAttributes } from 'react'

type Props = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'quiet' }

export function Button({ variant = 'quiet', className = '', ...props }: Props) {
  return <button className={`button button--${variant} ${className}`.trim()} {...props} />
}
