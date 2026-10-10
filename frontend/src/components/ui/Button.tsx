import { forwardRef, type ButtonHTMLAttributes } from 'react'

type Props = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'quiet' }

export const Button = forwardRef<HTMLButtonElement, Props>(function Button({ variant = 'quiet', className = '', ...props }, ref) {
  return <button ref={ref} className={`button button--${variant} ${className}`.trim()} {...props} />
})
